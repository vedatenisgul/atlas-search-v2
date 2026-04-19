"""
AtlasTrie — prefix-tree inverted index singleton.

PRD refs: §1 Executive Summary, §2.2 search(query), §3 System Architecture.

Node structure:
    children : dict[char -> TrieNode]
    metadata : dict[url -> {"term_frequency": int,
                             "depth": int,
                             "origin_url": str}]
    is_word  : bool

API:
    insert(word, url, depth, origin)  — O(len(word)); thread-safe write
    search(word, prefix=False)        — exact or prefix lookup; thread-safe read
    walk()                            — yields (word, postings) for ETL export
    word_count / size properties for telemetry

Concurrency:
    Guarded by a single ``threading.RLock``. The RLock lets a writer call back
    into the same instance (e.g. ``search()`` from within an ``insert()`` path)
    without self-deadlock.

Owner agent: Indexer Agent.
"""

from __future__ import annotations

import threading
from typing import Dict, Iterator, List, Optional, Tuple

from core.normalize import turkish_fold


# --------------------------------------------------------------- node


class TrieNode:
    """A single node in the AtlasTrie prefix tree."""

    __slots__ = ("children", "metadata", "is_word")

    def __init__(self) -> None:
        self.children: Dict[str, "TrieNode"] = {}
        # Postings keyed by URL; value carries ranking features.
        self.metadata: Dict[str, Dict[str, object]] = {}
        self.is_word: bool = False


# --------------------------------------------------------------- trie


class AtlasTrie:
    """Thread-safe singleton prefix-tree inverted index."""

    _instance: Optional["AtlasTrie"] = None
    _singleton_lock = threading.Lock()

    # ----------------------------------------------------- construction
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self.lock = threading.RLock()
        self.root = TrieNode()
        self._word_count = 0
        self._node_count = 1  # root
        # ``_dirty`` flips True on any mutating call (insert / bulk_insert /
        # purge_by_origin / clear). The ETL exporter reads + clears it under
        # the RLock so an idle flush tick does not pay for a full trie walk.
        # Hydration-time bulk_inserts (import_legacy_data_to_trie) do NOT
        # set the flag — the trie was just loaded from disk, re-exporting
        # it immediately would be wasted I/O.
        self._dirty: bool = False

    # ------------------------------------------------------- singleton
    @classmethod
    def get_instance(cls) -> "AtlasTrie":
        return cls()

    @classmethod
    def _reset_for_tests(cls) -> None:
        """Discard the singleton — tests only."""
        with cls._singleton_lock:
            cls._instance = None

    # ----------------------------------------------------------- write
    def insert(
        self,
        word: str,
        url: str,
        depth: int,
        origin: str,
    ) -> bool:
        """Insert ``word`` and update postings for ``url``.

        Returns True if the insert produced any state change. Empty or
        whitespace-only words are ignored (returns False).
        """
        if not word:
            return False
        # Defensive: apply Turkish-aware fold even on direct callers that
        # bypassed core.normalize.tokenize(). Keeps indexing symmetric with
        # query lookups regardless of entry point.
        token = turkish_fold(word.strip())
        if not token:
            return False
        if not url:
            return False

        try:
            depth_int = int(depth)
        except (TypeError, ValueError):
            depth_int = 0
        origin_str = origin or ""

        with self.lock:
            node = self.root
            for ch in token:
                next_node = node.children.get(ch)
                if next_node is None:
                    next_node = TrieNode()
                    node.children[ch] = next_node
                    self._node_count += 1
                node = next_node

            if not node.is_word:
                node.is_word = True
                self._word_count += 1

            posting = node.metadata.get(url)
            if posting is None:
                node.metadata[url] = {
                    "term_frequency": 1,
                    "depth": depth_int,
                    "origin_url": origin_str,
                }
            else:
                # Incremental update: bump frequency, keep shallowest depth so
                # ranking favors the closest hop from origin.
                posting["term_frequency"] = int(
                    posting.get("term_frequency", 0)
                ) + 1
                current_depth = int(posting.get("depth", depth_int))
                posting["depth"] = min(current_depth, depth_int)
                if not posting.get("origin_url"):
                    posting["origin_url"] = origin_str
            self._dirty = True
            return True

    def bulk_insert(self, word: str, postings: Dict[str, Dict[str, object]]) -> None:
        """Seed ``word`` with a full postings dict. Used by the ETL importer.

        Skips ``insert()``'s frequency increment so round-tripped data is
        reconstructed byte-identically.
        """
        if not word or not postings:
            return
        token = turkish_fold(word.strip())
        if not token:
            return

        with self.lock:
            node = self.root
            for ch in token:
                next_node = node.children.get(ch)
                if next_node is None:
                    next_node = TrieNode()
                    node.children[ch] = next_node
                    self._node_count += 1
                node = next_node
            if not node.is_word:
                node.is_word = True
                self._word_count += 1
            for url, entry in postings.items():
                if not url or not isinstance(entry, dict):
                    continue
                node.metadata[url] = {
                    "term_frequency": int(entry.get("term_frequency", 1) or 1),
                    "depth": int(entry.get("depth", 0) or 0),
                    "origin_url": str(entry.get("origin_url", "") or ""),
                }

    # ------------------------------------------------------------ read
    def search(
        self,
        word: str,
        prefix: bool = False,
        *,
        exact: Optional[bool] = None,
    ) -> Dict[str, Dict[str, object]]:
        """Lookup postings for ``word``.

        * Exact mode (default): returns the postings dict for the exact word,
          or an empty dict if the term is not indexed. "ist" will NOT match
          "istanbul" — exact mode is ``WHERE word = 'ist'``, never
          ``WHERE word LIKE 'ist%'``.
        * Prefix mode: returns a merged postings dict across every word sharing
          the given prefix. Frequencies are summed, minimum depth is taken per
          URL, and the first-seen ``origin_url`` wins.

        The primary kwarg is ``exact`` (matching the product spec:
        ``trie_db.search(word, exact=True)``). The legacy ``prefix`` flag is
        kept for backward compatibility — if both are supplied ``exact`` wins.
        """
        if exact is not None:
            prefix = not bool(exact)

        if not word:
            return {}
        token = turkish_fold(word.strip())
        if not token:
            return {}

        with self.lock:
            node = self._descend(token)
            if node is None:
                return {}

            if not prefix:
                if not node.is_word:
                    return {}
                return self._copy_postings(node.metadata)

            aggregate: Dict[str, Dict[str, object]] = {}
            for sub_node in self._iter_subtree(node):
                if not sub_node.is_word:
                    continue
                self._merge_postings(aggregate, sub_node.metadata)
            return aggregate

    def contains(self, word: str) -> bool:
        """True when ``word`` is a complete indexed term."""
        if not word:
            return False
        token = turkish_fold(word.strip())
        if not token:
            return False
        with self.lock:
            node = self._descend(token)
            return bool(node and node.is_word)

    def starts_with(self, prefix: str) -> bool:
        """True when any indexed word starts with ``prefix``."""
        if not prefix:
            return False
        token = turkish_fold(prefix.strip())
        if not token:
            return False
        with self.lock:
            return self._descend(token) is not None

    # ------------------------------------------------------- traversal
    def walk(self) -> Iterator[Tuple[str, Dict[str, Dict[str, object]]]]:
        """Yield ``(word, postings_copy)`` across every terminal node.

        Snapshots the entire ``(word, postings_copy)`` list under the RLock,
        then releases the lock and yields from the snapshot. This keeps
        concurrent ``insert()`` calls from stalling for the duration of a
        full ETL export (the exporter walks every terminal node). The
        memory cost is one shallow copy of each posting dict — bounded by
        the index size, and already paid whenever an export runs.
        """
        with self.lock:
            snapshot: List[Tuple[str, Dict[str, Dict[str, object]]]] = []
            stack: List[Tuple[TrieNode, List[str]]] = [(self.root, [])]
            while stack:
                node, path = stack.pop()
                if node.is_word and node.metadata:
                    snapshot.append(
                        ("".join(path), self._copy_postings(node.metadata))
                    )
                # Stable deterministic order helps diff/debug ETL output.
                for ch in sorted(node.children.keys()):
                    child = node.children[ch]
                    stack.append((child, path + [ch]))

        # Yield outside the lock — callers (notably the exporter) can do
        # per-record I/O without holding the RLock against live writers.
        for item in snapshot:
            yield item

    # ------------------------------------------------------- telemetry
    @property
    def word_count(self) -> int:
        with self.lock:
            return self._word_count

    @property
    def node_count(self) -> int:
        with self.lock:
            return self._node_count

    def size(self) -> int:
        """Alias for ``word_count``."""
        return self.word_count

    @property
    def is_dirty(self) -> bool:
        """True when an indexing mutation has happened since the last export.

        Cheap advisory read; callers use this to skip a full ``walk()`` on
        otherwise-idle flush ticks. Hydration (``bulk_insert`` from the ETL
        importer) intentionally does not flip this flag — the in-memory
        trie was just rebuilt from the files we'd be re-exporting to.
        """
        with self.lock:
            return bool(self._dirty)

    def mark_exported(self) -> None:
        """Signal a successful ETL export — clears the dirty flag."""
        with self.lock:
            self._dirty = False

    # ---------------------------------------------------------- utility
    def clear(self) -> None:
        """Drop the entire index. Used by the ``/api/system/reset`` endpoint."""
        with self.lock:
            self.root = TrieNode()
            self._word_count = 0
            self._node_count = 1
            # Nothing left to export. Force ``_dirty=False`` so the next
            # exporter tick no-ops instead of writing a (correct but
            # wasteful) empty shard set.
            self._dirty = False

    def purge_by_origin(self, origin_url: str) -> Dict[str, int]:
        """Remove every posting that originated from ``origin_url``.

        Cascading-delete support: when a crawl job is deleted the orchestrator
        calls this so words/URLs sourced from that seed no longer surface in
        search results. Walks the entire trie under the RLock so concurrent
        readers/writers observe a consistent view.

        Returns a telemetry dict:
            {"postings_removed": int,  # URL entries dropped
             "words_unindexed":  int}  # terminal words that now have no
                                       # postings left (is_word cleared)
        """
        if not origin_url:
            return {"postings_removed": 0, "words_unindexed": 0}
        target = str(origin_url)

        postings_removed = 0
        words_unindexed = 0

        with self.lock:
            stack: List[TrieNode] = [self.root]
            while stack:
                node = stack.pop()
                if node.metadata:
                    # Drop every posting tagged with this origin.
                    to_drop = [
                        url
                        for url, entry in node.metadata.items()
                        if isinstance(entry, dict)
                        and entry.get("origin_url") == target
                    ]
                    for url in to_drop:
                        node.metadata.pop(url, None)
                        postings_removed += 1

                    # A terminal word with no surviving postings is dead; clear
                    # its is_word flag so search() and contains() stop matching.
                    if node.is_word and not node.metadata:
                        node.is_word = False
                        if self._word_count > 0:
                            self._word_count -= 1
                        words_unindexed += 1

                stack.extend(node.children.values())

            if postings_removed or words_unindexed:
                self._dirty = True

        return {
            "postings_removed": postings_removed,
            "words_unindexed": words_unindexed,
        }

    # ------------------------------------------------------- internals
    def _descend(self, token: str) -> Optional[TrieNode]:
        node = self.root
        for ch in token:
            node = node.children.get(ch)
            if node is None:
                return None
        return node

    @staticmethod
    def _iter_subtree(node: TrieNode) -> Iterator[TrieNode]:
        stack: List[TrieNode] = [node]
        while stack:
            current = stack.pop()
            yield current
            stack.extend(current.children.values())

    @staticmethod
    def _copy_postings(
        postings: Dict[str, Dict[str, object]],
    ) -> Dict[str, Dict[str, object]]:
        return {url: dict(entry) for url, entry in postings.items()}

    @staticmethod
    def _merge_postings(
        target: Dict[str, Dict[str, object]],
        source: Dict[str, Dict[str, object]],
    ) -> None:
        for url, entry in source.items():
            existing = target.get(url)
            if existing is None:
                target[url] = dict(entry)
                continue
            existing["term_frequency"] = int(
                existing.get("term_frequency", 0)
            ) + int(entry.get("term_frequency", 0))
            existing["depth"] = min(
                int(existing.get("depth", 0)),
                int(entry.get("depth", 0)),
            )
            if not existing.get("origin_url"):
                existing["origin_url"] = entry.get("origin_url", "")


__all__ = ["AtlasTrie", "TrieNode"]

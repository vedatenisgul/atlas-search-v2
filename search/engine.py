"""
SearchEngine — static query pipeline over AtlasTrie + NoSQLStore.

PRD refs: §2.2 search(query), §6 Search Ranking.

Pipeline stages (mirrors the product spec):

    1. **Query normalization** — Turkish-aware case fold + punctuation-free
       tokenization via :func:`core.normalize.tokenize`.  This is the *same*
       helper the crawler uses during indexing, so indexed text and query
       text always hash to the same token set.

           "İstanbul Üniversitesi!"  ->  ["istanbul", "üniversitesi"]

    2. **Trie lookup (exact match)** — for each token, call
       ``trie_db.search(token, exact=True)``.  The trie walks character by
       character; any missing edge => empty postings for that token.
       ``exact=True`` returns metadata *only* at the terminal node, so
       "ist" never matches "istanbul".

    3. **Result aggregation** — merge postings across query tokens keyed by
       URL.  ``term_frequency`` is summed, the *shallowest* ``depth`` wins,
       and the first non-empty ``origin_url`` is preserved.

    4. **Ranking** — delegate to :func:`search.ranking.rank_results`, which
       applies ``score = (frequency × 10) + 1000 − (depth × 5)`` and sorts
       descending.

    5. **Pagination** — slice ``results[offset : offset + limit]`` *after*
       ranking so the top-N contract is honored regardless of page.

    6. **Hydration** — enrich each surviving record with ``title`` and
       ``snippet`` pulled from ``NoSQLStore.data['metadata']``.

Concurrency:
    Reads run concurrently with active crawl/index writers because the Trie
    uses an RLock and the NoSQLStore exposes a Lock over ``data``. Each
    external call is wrapped in the corresponding guard.

Owner agent: Search Agent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.normalize import tokenize_list
from search.ranking import rank_results


# --------------------------------------------------------------- engine


class SearchEngine:
    """Static query façade over the Trie + NoSQLStore singletons.

    Intentionally stateless: instantiation is never required, and every entry
    point is a ``@staticmethod``. This keeps the search layer trivially
    concurrent — all shared state lives behind the storage-layer locks.
    """

    # ------------------------------------------------------------ public
    @staticmethod
    def query(
        query: str,
        limit: int = 10,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Execute the full query pipeline and return hydrated results.

        Parameters
        ----------
        query   : free-text user query; Turkish characters are folded.
        limit   : max number of results to return after ranking.
        offset  : number of top-ranked results to skip for pagination.

        Returns
        -------
        List of result records. Each record is a dict with keys:
            url, origin_url, depth, frequency, relevance_score,
            title, snippet.

        An empty list is returned for empty queries, queries that tokenize
        to nothing, or queries where no token has any trie hit.
        """
        return SearchEngine.query_with_total(query, limit, offset)["results"]

    @staticmethod
    def query_with_total(
        query: str,
        limit: int = 10,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Same pipeline as ``query()``, but also reports the full match count.

        Useful for the UI which needs to render "N results" regardless of
        which page of the result set the user is viewing.

        Returns
        -------
        dict with keys:
            results : list[dict]  # paginated, hydrated records
            total   : int         # total matches across *all* pages
        """
        # Step 1: Query normalization — shared tokenizer guarantees symmetry
        # with how crawler/worker.py indexed the same text.
        tokens = tokenize_list(query)
        if not tokens:
            return {"results": [], "total": 0}

        safe_limit, safe_offset = SearchEngine._normalize_pagination(limit, offset)

        # Steps 2 + 3: Exact-match trie lookup per token, then aggregate.
        aggregated = SearchEngine._lookup_and_aggregate(tokens)
        total = len(aggregated)
        if total == 0 or safe_limit == 0:
            return {"results": [], "total": total}

        # Step 4: Rank.
        ranked = rank_results(aggregated)
        # Step 5: Paginate after ranking so page N is deterministic.
        window = ranked[safe_offset : safe_offset + safe_limit]
        if not window:
            return {"results": [], "total": total}

        # Step 6: Hydrate with title + snippet from the metadata store.
        return {"results": SearchEngine._hydrate(window), "total": total}

    # ------------------------------------------------------- pagination
    @staticmethod
    def _normalize_pagination(limit: Any, offset: Any) -> tuple:
        """Clamp user-supplied pagination into safe non-negative ints."""
        try:
            safe_limit = int(limit)
        except (TypeError, ValueError):
            safe_limit = 10
        try:
            safe_offset = int(offset)
        except (TypeError, ValueError):
            safe_offset = 0
        if safe_limit < 0:
            safe_limit = 0
        if safe_offset < 0:
            safe_offset = 0
        return safe_limit, safe_offset

    # ----------------------------------------------------- aggregation
    @staticmethod
    def _lookup_and_aggregate(tokens: List[str]) -> Dict[str, Dict[str, Any]]:
        """Run one Trie lookup per token and merge postings by URL.

        Aggregation rules (PRD §6):
            * ``term_frequency``: summed across every token the URL matched.
            * ``depth``:          minimum depth observed across token matches.
            * ``origin_url``:     first non-empty value wins — origin does not
                                  change between postings for the same URL.
        """
        trie = SearchEngine._get_trie()
        if trie is None:
            return {}

        aggregated: Dict[str, Dict[str, Any]] = {}
        for token in tokens:
            # ``exact=True`` matches the product spec exactly. The lookup is
            # strict: searching "ist" never matches "istanbul".
            postings = trie.search(token, exact=True) or {}
            if not postings:
                continue
            for url, entry in postings.items():
                if not url or not isinstance(entry, dict):
                    continue
                freq = int(entry.get("term_frequency", 0) or 0)
                depth = int(entry.get("depth", 0) or 0)
                origin = str(entry.get("origin_url", "") or "")

                bucket = aggregated.get(url)
                if bucket is None:
                    aggregated[url] = {
                        "term_frequency": freq,
                        "depth": depth,
                        "origin_url": origin,
                    }
                else:
                    bucket["term_frequency"] = int(bucket["term_frequency"]) + freq
                    bucket["depth"] = min(int(bucket["depth"]), depth)
                    if not bucket["origin_url"] and origin:
                        bucket["origin_url"] = origin
        return aggregated

    # --------------------------------------------------------- hydration
    @staticmethod
    def _hydrate(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Attach ``title`` and ``snippet`` to each result from NoSQLStore.

        Missing metadata entries degrade to empty strings rather than
        dropping the row — a ranked hit without stored metadata is still a
        legitimate search result (the crawler may not have flushed yet).
        """
        metadata_map = SearchEngine._snapshot_metadata()
        hydrated: List[Dict[str, Any]] = []
        for record in results:
            meta = metadata_map.get(record["url"]) or {}
            record["title"] = str(meta.get("title", "") or "")
            record["snippet"] = str(meta.get("snippet", "") or "")
            hydrated.append(record)
        return hydrated

    @staticmethod
    def _snapshot_metadata() -> Dict[str, Dict[str, Any]]:
        """Return a shallow copy of ``db.data['metadata']`` under the lock.

        Copying under the lock lets ``_hydrate`` iterate without blocking
        concurrent crawler writes.
        """
        db = SearchEngine._get_store()
        if db is None:
            return {}
        try:
            with db.lock:
                raw = db.data.get("metadata") or {}
                if not isinstance(raw, dict):
                    return {}
                return dict(raw)
        except Exception:
            return {}

    # --------------------------------------------------------- lookups
    @staticmethod
    def _get_trie() -> Optional[Any]:
        """Fetch the AtlasTrie singleton; import lazily to avoid cycles."""
        try:
            from storage.trie import AtlasTrie
            return AtlasTrie.get_instance()
        except Exception:
            return None

    @staticmethod
    def _get_store() -> Optional[Any]:
        """Fetch the NoSQLStore singleton; import lazily to avoid cycles."""
        try:
            from storage.nosql import NoSQLStore
            return NoSQLStore.get_instance()
        except Exception:
            return None


__all__ = ["SearchEngine"]

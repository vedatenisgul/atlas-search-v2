"""
ETL exporter / importer for the AtlasTrie.

PRD refs: §7 Persistence.

Export:
    ``export_all_to_legacy_format()`` walks the Trie, shards terminal nodes by
    the word's first character into ``data/storage/{a-z}.data`` files, and
    writes one record per ``(word, url)`` pair. Words whose first character
    falls outside ``a-z`` are routed to ``_misc.data``.

Import:
    ``import_legacy_data_to_trie()`` scans ``data/storage/*.data`` at boot and
    rebuilds the in-memory Trie via ``AtlasTrie.bulk_insert()``. Malformed
    lines are skipped — partial corruption never blocks startup.

Line format (tab-separated, one record per line):

    word \\t url \\t origin_url \\t depth \\t term_frequency \\n

Tabs, newlines, and backslashes inside any field are backslash-escaped so the
format is a strict round-trip.

Owner agent: Indexer Agent.
"""

from __future__ import annotations

import glob
import os
import string
import tempfile
from typing import Dict, Iterable, Optional, Tuple

from core import config
from storage.trie import AtlasTrie


# --------------------------------------------------------------- helpers

_FIELD_DELIMITER = "\t"
_MISC_SHARD = "_misc"
_VALID_SHARDS = set(string.ascii_lowercase) | {_MISC_SHARD}


def _shard_for(word: str) -> str:
    """Return the shard name (a-z or ``_misc``) for ``word``."""
    if not word:
        return _MISC_SHARD
    first = word[0].lower()
    if "a" <= first <= "z":
        return first
    return _MISC_SHARD


def _encode_field(value: str) -> str:
    """Backslash-escape characters that would break the line format."""
    if value is None:
        return ""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _decode_field(value: str) -> str:
    """Invert ``_encode_field`` — safe against unterminated escape sequences."""
    out: list = []
    i = 0
    length = len(value)
    while i < length:
        ch = value[i]
        if ch == "\\" and i + 1 < length:
            nxt = value[i + 1]
            if nxt == "\\":
                out.append("\\")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "n":
                out.append("\n")
            else:
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _ensure_data_dir(data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)


def _shard_path(data_dir: str, shard: str) -> str:
    return os.path.join(data_dir, f"{shard}.data")


def _atomic_write_shard(path: str, lines: Iterable[str]) -> None:
    """Write ``lines`` atomically to ``path`` — tmp in same dir + os.replace."""
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".shard-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except (OSError, AttributeError):
                pass
        os.replace(tmp_path, path)
    except Exception:
        # On failure: remove the tmp so we don't leave turds behind.
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------- export


def export_all_to_legacy_format(
    trie: Optional[AtlasTrie] = None,
    data_dir: Optional[str] = None,
) -> Dict[str, int]:
    """Flush the Trie to ``data/storage/{a-z}.data``.

    Returns a dict ``{shard_name: record_count}`` for telemetry / tests.
    Each shard is written atomically so a mid-flush crash can't leave a
    partially-rewritten file that the importer would choke on.
    """
    target_trie = trie or AtlasTrie.get_instance()
    target_dir = data_dir or config.DATA_DIR
    _ensure_data_dir(target_dir)

    buckets: Dict[str, list] = {}
    for word, postings in target_trie.walk():
        if not word or not postings:
            continue
        shard = _shard_for(word)
        encoded_word = _encode_field(word)
        bucket = buckets.setdefault(shard, [])
        for url, entry in postings.items():
            if not url:
                continue
            line = _FIELD_DELIMITER.join(
                (
                    encoded_word,
                    _encode_field(url),
                    _encode_field(str(entry.get("origin_url", "") or "")),
                    str(int(entry.get("depth", 0) or 0)),
                    str(int(entry.get("term_frequency", 0) or 0)),
                )
            ) + "\n"
            bucket.append(line)

    counts: Dict[str, int] = {}
    # Write every shard we have data for.
    for shard, lines in buckets.items():
        path = _shard_path(target_dir, shard)
        _atomic_write_shard(path, lines)
        counts[shard] = len(lines)

    # Remove shards that existed on disk but no longer carry any records.
    # Deleting (rather than truncating to 0 bytes) keeps the data/storage/
    # dir tidy after a reset + partial re-crawl and avoids leaving empty
    # .data files the importer would still open and skip on next boot.
    for path in glob.glob(os.path.join(target_dir, "*.data")):
        shard_name = os.path.splitext(os.path.basename(path))[0]
        if shard_name not in buckets and shard_name in _VALID_SHARDS:
            try:
                os.remove(path)
                counts.setdefault(shard_name, 0)
            except FileNotFoundError:
                counts.setdefault(shard_name, 0)
            except OSError:
                continue

    return counts


# --------------------------------------------------------------- import


def import_legacy_data_to_trie(
    trie: Optional[AtlasTrie] = None,
    data_dir: Optional[str] = None,
) -> Dict[str, int]:
    """Rebuild the Trie from ``data/storage/*.data`` files.

    Returns ``{shard_name: records_loaded}``. Missing directory or empty
    shards are tolerated — the result is simply an empty dict.
    """
    target_trie = trie or AtlasTrie.get_instance()
    target_dir = data_dir or config.DATA_DIR
    if not os.path.isdir(target_dir):
        return {}

    counts: Dict[str, int] = {}
    # Accumulate per-word postings before handing to bulk_insert so repeated
    # (word, url) records collapse cleanly even if a shard was rewritten mid-
    # export in a previous run.
    for path in sorted(glob.glob(os.path.join(target_dir, "*.data"))):
        shard_name = os.path.splitext(os.path.basename(path))[0]
        pending: Dict[str, Dict[str, Dict[str, object]]] = {}
        loaded = 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for raw_line in fh:
                    record = _parse_line(raw_line)
                    if record is None:
                        continue
                    word, url, origin, depth, freq = record
                    postings = pending.setdefault(word, {})
                    postings[url] = {
                        "term_frequency": freq,
                        "depth": depth,
                        "origin_url": origin,
                    }
                    loaded += 1
        except OSError:
            continue

        for word, postings in pending.items():
            target_trie.bulk_insert(word, postings)
        if loaded:
            counts[shard_name] = loaded
    return counts


def _parse_line(raw: str) -> Optional[Tuple[str, str, str, int, int]]:
    """Parse one ``word\\turl\\torigin\\tdepth\\tfreq`` line."""
    line = raw.rstrip("\n").rstrip("\r")
    if not line:
        return None
    parts = line.split(_FIELD_DELIMITER)
    if len(parts) != 5:
        return None
    word = _decode_field(parts[0])
    url = _decode_field(parts[1])
    origin = _decode_field(parts[2])
    if not word or not url:
        return None
    try:
        depth = int(parts[3])
        freq = int(parts[4])
    except ValueError:
        return None
    if freq <= 0:
        return None
    return word, url, origin, depth, freq


# --------------------------------------------------------------- facade


class ETLExporter:
    """Thin class facade around the module-level ETL functions.

    The crawler resolves either the free functions or this class's methods,
    so we expose both shapes without duplicating logic.
    """

    @staticmethod
    def export_all_to_legacy_format(
        trie: Optional[AtlasTrie] = None,
        data_dir: Optional[str] = None,
    ) -> Dict[str, int]:
        return export_all_to_legacy_format(trie=trie, data_dir=data_dir)

    @staticmethod
    def import_legacy_data_to_trie(
        trie: Optional[AtlasTrie] = None,
        data_dir: Optional[str] = None,
    ) -> Dict[str, int]:
        return import_legacy_data_to_trie(trie=trie, data_dir=data_dir)


__all__ = [
    "export_all_to_legacy_format",
    "import_legacy_data_to_trie",
    "ETLExporter",
]

"""
NoSQLStore — in-memory singleton KV with threading.Lock.

PRD refs: §1 Executive Summary, §7 Persistence.

Schema (8 keys, per Indexer Agent spec):
    seen_urls         : dict[job_id -> list[sha256(url)]]     # per-job dedup
    visited_urls      : dict[sha256(url) -> expiry_ts]        # global TTL dedup
    crawler_queue     : dict[job_id -> list[(url, depth)]]    # persisted frontier
    job_queue_counts  : dict[job_id -> int]                   # pending per job
    crawler_logs      : dict[job_id -> list[log_entry]]       # bounded log ring
    jobs              : dict[job_id -> job_state]             # active jobs
    job_history       : list[archived_job]                    # completed jobs
    metadata          : dict[url -> {title, snippet, depth, origin, ts}]

Persistence:
    * Atomic JSON flush to ``atlas_store.json`` — write to ``.tmp`` then
      ``os.replace()`` so readers never observe a partial file.
    * Background daemon thread flushes every ``SYNC_INTERVAL`` seconds.
    * Explicit ``save()`` triggered by workers on exit.

Concurrency:
    One ``threading.Lock`` guards every mutation. The public ``lock``
    attribute is exposed so callers (crawler, search) can wrap compound
    ``data`` mutations in a single critical section, matching the pattern
    already established in ``crawler/worker.py``.

Owner agent: Indexer Agent.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Any, Dict, Optional

from core import config


# --------------------------------------------------------------- schema


def _default_schema() -> Dict[str, Any]:
    """Return a fresh copy of the store schema.

    ``job_logs`` is the ring-buffer key the worker + routes actually write
    to; it replaced the older ``crawler_logs`` name so the schema declaration
    and the runtime readers/writers now agree. The other four keys below
    are reserved for future use (per-job queue persistence across reboots)
    but are intentionally left empty today — nothing writes to them yet.
    """
    return {
        "seen_urls": {},
        "visited_urls": {},
        "crawler_queue": {},
        "job_queue_counts": {},
        "job_logs": {},
        "jobs": {},
        "job_history": [],
        "metadata": {},
    }


# --------------------------------------------------------------- helpers


def _json_default(obj: Any) -> Any:
    """JSON fallback encoder for the non-native types we keep in ``data``."""
    if isinstance(obj, deque):
        return list(obj)
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode("utf-8", errors="replace")
        except Exception:
            return list(obj)
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


def _rehydrate_log_rings(data: Dict[str, Any]) -> None:
    """Convert persisted log lists back into bounded deques in place.

    The ring is keyed ``job_logs`` (matching ``crawler/worker.py::_log`` and
    ``api/routes.py::_read_job_logs``). A JSON load always returns list[dict]
    entries; we wrap them into bounded deques so the ``LOG_RING_SIZE`` cap
    is enforced from the first post-restart append, not only once the list
    grows past the bound.
    """
    logs = data.get("job_logs")
    if isinstance(logs, dict):
        for job_id, entries in list(logs.items()):
            if isinstance(entries, deque):
                continue
            try:
                seq = list(entries) if entries is not None else []
            except TypeError:
                seq = []
            logs[job_id] = deque(seq, maxlen=config.LOG_RING_SIZE)


# --------------------------------------------------------------- store


class NoSQLStore:
    """Thread-safe singleton key-value store with atomic JSON persistence."""

    _instance: Optional["NoSQLStore"] = None
    _singleton_lock = threading.Lock()

    # ----------------------------------------------------- construction
    def __new__(cls, *args, **kwargs):
        # Enforce singleton semantics across every construction path.
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, store_path: Optional[str] = None) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self.store_path = store_path or config.STORE_PATH
        self.lock = threading.Lock()
        self.data: Dict[str, Any] = _default_schema()

        self._sync_interval = max(1, int(config.SYNC_INTERVAL))
        self._stop_flag = threading.Event()
        self._sync_thread: Optional[threading.Thread] = None
        self._dirty = False
        # Monotonic counter bumped on every write. ``save()`` captures the
        # counter under the lock with the snapshot, then only clears
        # ``_dirty`` after the disk write if the counter has not advanced —
        # preventing the "clear dirty after concurrent write" race.
        self._write_seq: int = 0
        self._last_flush_ts: float = 0.0

        self._load_from_disk()
        self._start_sync_daemon()

    # ------------------------------------------------------- singleton
    @classmethod
    def get_instance(cls) -> "NoSQLStore":
        """Return the process-wide singleton instance."""
        return cls()

    @classmethod
    def _reset_for_tests(cls) -> None:
        """Tear down the singleton. Tests only — never call from app code."""
        inst = cls._instance
        if inst is not None:
            try:
                inst.shutdown(save=False)
            except Exception:
                pass
        with cls._singleton_lock:
            cls._instance = None

    # -------------------------------------------------------- mutation
    def mark_dirty(self) -> None:
        """Signal the sync daemon that state changed since last flush.

        Callers that hold ``self.lock`` around a compound mutation should
        invoke this from *inside* the critical section so the write-seq
        bump is observed atomically with the data change. For the common
        one-shot mutations below (``put`` / ``update``) the method is
        already called under the lock.
        """
        self._dirty = True
        self._write_seq += 1

    def put(self, key: str, value: Any) -> None:
        with self.lock:
            self.data[key] = value
            self._dirty = True
            self._write_seq += 1

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            return self.data.get(key, default)

    def update(self, key: str, mutator) -> Any:
        """Apply ``mutator(current_value)`` under the lock and persist dirty."""
        with self.lock:
            new_value = mutator(self.data.get(key))
            self.data[key] = new_value
            self._dirty = True
            self._write_seq += 1
            return new_value

    @property
    def is_dirty(self) -> bool:
        """True when writes have happened since the last successful flush.

        Cheap advisory read — callers use this to skip a ``save()`` on idle
        flush ticks and avoid the full in-memory walk ``save()`` performs.
        """
        return bool(self._dirty)

    # ------------------------------------------------------ persistence
    def save(self) -> bool:
        """Atomically flush ``data`` to ``store_path``. Returns success.

        Captures a snapshot + the current ``_write_seq`` under the lock,
        releases the lock for the (potentially slow) disk write, and only
        clears ``_dirty`` if no concurrent writer bumped the sequence while
        the flush was in flight. Otherwise the dirty flag stays armed and
        the next sync-daemon tick will re-flush the outstanding work.
        """
        with self.lock:
            snapshot = self._prepare_snapshot()
            snapshot_seq = self._write_seq
        ok = self._atomic_write(snapshot)
        if ok:
            with self.lock:
                self._last_flush_ts = time.time()
                # Only clear dirty when nothing changed during the flush.
                if self._write_seq == snapshot_seq:
                    self._dirty = False
        return ok

    def _prepare_snapshot(self) -> Dict[str, Any]:
        """Produce a JSON-safe shallow copy of the in-memory state."""
        snapshot: Dict[str, Any] = {}
        for key, value in self.data.items():
            if isinstance(value, deque):
                snapshot[key] = list(value)
            elif key == "job_logs" and isinstance(value, dict):
                # Convert any nested deques (keyed per job) to plain lists so
                # the JSON encoder can emit them. The deques are re-formed on
                # load via :func:`_rehydrate_log_rings`.
                snapshot[key] = {
                    job_id: list(entries) if isinstance(entries, deque) else entries
                    for job_id, entries in value.items()
                }
            else:
                snapshot[key] = value
        snapshot["_last_flush_ts"] = time.time()
        return snapshot

    def _atomic_write(self, snapshot: Dict[str, Any]) -> bool:
        """Write ``snapshot`` atomically: .tmp + os.replace().

        Does **not** touch ``self._dirty`` or ``self._last_flush_ts`` — those
        are updated by :meth:`save` under the lock after the write returns
        so they cannot race with a concurrent writer bumping ``_write_seq``.
        """
        path = self.store_path
        tmp_path = f"{path}.tmp"
        parent = os.path.dirname(os.path.abspath(path))
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(
                    snapshot,
                    fh,
                    ensure_ascii=False,
                    default=_json_default,
                )
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except (OSError, AttributeError):
                    # fsync may be unavailable on some platforms; tolerable.
                    pass
            os.replace(tmp_path, path)
            return True
        except Exception:
            # Best-effort cleanup — never raise from the background flusher.
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False

    def _load_from_disk(self) -> None:
        """Hydrate ``self.data`` from disk, keeping missing schema keys."""
        path = self.store_path
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, json.JSONDecodeError):
            # Corrupt store: start fresh rather than crashing the process.
            return
        if not isinstance(loaded, dict):
            return

        for key, default_value in _default_schema().items():
            if key in loaded:
                self.data[key] = loaded[key]
            else:
                self.data[key] = default_value
        self._last_flush_ts = float(loaded.get("_last_flush_ts") or 0.0)
        _rehydrate_log_rings(self.data)

    # ------------------------------------------------------ sync daemon
    def _start_sync_daemon(self) -> None:
        if self._sync_thread is not None and self._sync_thread.is_alive():
            return
        thread = threading.Thread(
            target=self._sync_loop,
            name="NoSQLStore-sync",
            daemon=True,
        )
        self._sync_thread = thread
        thread.start()

    def _sync_loop(self) -> None:
        # Loop exits only when shutdown() is called. Errors are swallowed so
        # an unstable FS (e.g. tmpfs at capacity) can't take the app down.
        while not self._stop_flag.wait(self._sync_interval):
            if self._dirty:
                try:
                    self.save()
                except Exception:
                    pass

    def shutdown(self, save: bool = True) -> None:
        """Stop the sync daemon and optionally flush one last time."""
        self._stop_flag.set()
        thread = self._sync_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._sync_interval + 1)
        if save:
            try:
                self.save()
            except Exception:
                pass

    # --------------------------------------------------------- helpers
    def reset(self) -> None:
        """Clear all data back to the default schema. Tests / admin only.

        Memory-only: leaves ``atlas_store.json`` on disk and marks the store
        dirty so the sync daemon will overwrite the file on its next tick.
        For the full memory + disk wipe used by ``/api/system/reset`` see
        :meth:`clear_all`.
        """
        with self.lock:
            self.data = _default_schema()
            self._dirty = True
            self._write_seq += 1

    def clear_all(self, delete_file: bool = True) -> Dict[str, int]:
        """Deep purge: reset in-memory state *and* remove the JSON file.

        Unlike :meth:`reset`, this method clears the ``_dirty`` flag so the
        background sync daemon does not immediately rewrite a fresh (but
        empty) ``atlas_store.json`` moments after we just deleted it. The
        reset orchestrator in ``api/routes.py`` is responsible for calling
        :func:`crawler.worker.abort_pending_flushes` *before* this so any
        in-flight worker finalize pass also skips its store.save().

        Args:
            delete_file: when True, physically remove ``self.store_path``.
                Set to False for tests that only want the memory wipe.

        Returns:
            Telemetry dict: ``{"store_removed": 0|1}``.
        """
        store_removed = 0
        with self.lock:
            self.data = _default_schema()
            self._dirty = False
            self._write_seq += 1
            self._last_flush_ts = 0.0
            if delete_file:
                path = self.store_path
                try:
                    os.remove(path)
                    store_removed = 1
                except FileNotFoundError:
                    pass
                except OSError:
                    # Never raise from a purge — the memory reset still
                    # took effect and the sync daemon is now quiescent.
                    pass
                # Also sweep a stale .tmp left behind by a crashed flush so
                # the next save() starts from a clean slate.
                tmp_path = f"{path}.tmp"
                try:
                    os.remove(tmp_path)
                except (FileNotFoundError, OSError):
                    pass
        return {"store_removed": store_removed}

    def purge_origin(self, origin_url: str) -> Dict[str, int]:
        """Drop every row sourced from ``origin_url``.

        Cascading-delete counterpart to ``AtlasTrie.purge_by_origin()``. The
        orchestrator calls this when a job is deleted so page metadata and
        the global visited-URL TTL set no longer reference that crawl.

        Steps (all under ``self.lock`` — no concurrent writers can observe a
        half-deleted state):
            1. Walk ``metadata`` and collect every URL whose ``origin`` matches.
            2. Compute the sha256 of each (matching the hash the crawler used
               when it wrote ``visited_urls``) and drop the TTL entry.
            3. Remove the metadata rows themselves.
            4. Mark the store dirty so the sync daemon flushes.

        Returns a telemetry dict: metadata_removed / visited_removed.
        """
        if not origin_url:
            return {"metadata_removed": 0, "visited_removed": 0}

        # Lazy import — security module isn't always available at module import
        # time during test bootstraps.
        try:
            from core.security import sha256_url  # local to avoid import cycle
        except Exception:
            sha256_url = None  # type: ignore[assignment]

        metadata_removed = 0
        visited_removed = 0

        with self.lock:
            metadata = self.data.get("metadata")
            visited = self.data.get("visited_urls")

            victim_urls: list[str] = []
            if isinstance(metadata, dict):
                for url, entry in list(metadata.items()):
                    if isinstance(entry, dict) and entry.get("origin") == origin_url:
                        victim_urls.append(url)

            if isinstance(visited, dict) and sha256_url is not None:
                for url in victim_urls:
                    try:
                        url_hash = sha256_url(url)
                    except Exception:
                        continue
                    if visited.pop(url_hash, None) is not None:
                        visited_removed += 1

            if isinstance(metadata, dict):
                for url in victim_urls:
                    if metadata.pop(url, None) is not None:
                        metadata_removed += 1

            self._dirty = True
            self._write_seq += 1

        return {
            "metadata_removed": metadata_removed,
            "visited_removed": visited_removed,
        }


__all__ = ["NoSQLStore"]

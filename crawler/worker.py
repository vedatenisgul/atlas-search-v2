"""
CrawlerWorker — one threading.Thread per crawl job.

PRD refs: §2.1 index(origin, k), §3 System Architecture, §5 Backpressure.

Lifecycle:
    fetch -> parse -> index -> enqueue children (depth+1)
    honor pause/resume/stop flags, rate limit via time.sleep(1/hit_rate),
    finally block persists NoSQLStore and exports Trie via storage.exporter.

Concurrency: writes to AtlasTrie under its RLock; writes to NoSQLStore
under its Lock. Reads by SearchEngine are concurrent-safe.

Owner agent: Crawler Agent.
"""

from __future__ import annotations

import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Iterable, Optional, Tuple

from core import config
from core.normalize import tokenize as normalize_tokenize
from core.parser import AtlasHTMLParser
from core.security import (
    normalize_url,
    sanitize_html_input,
    sha256_url,
    validate_url,
)
from crawler.queue import CrawlerQueue


# ---------------------------------------------------------------- constants


def _cfg(name: str, fallback):
    try:
        value = getattr(config, name, fallback)
        return value if value is not None else fallback
    except Exception:
        return fallback


DEFAULT_MAX_DEPTH = int(_cfg("DEFAULT_MAX_DEPTH", 3))
DEFAULT_MAX_URLS = int(_cfg("DEFAULT_MAX_URLS", 1000))
DEFAULT_HIT_RATE = float(_cfg("DEFAULT_HIT_RATE", 2.0))
DEFAULT_MAX_CAPACITY = int(_cfg("DEFAULT_MAX_CAPACITY", 10_000))
VISITED_TTL_SECONDS = int(_cfg("VISITED_TTL_SECONDS", 3600))
EMPTY_QUEUE_BACKOFF_SECONDS = int(_cfg("EMPTY_QUEUE_BACKOFF_SECONDS", 2))
LOG_RING_SIZE = int(_cfg("LOG_RING_SIZE", 50))

USER_AGENT = "AtlasSearchBot/1.0 (+https://atlas.search)"
HTTP_TIMEOUT_SECONDS = 10
MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB per response

# Periodic flush of the Trie to ``data/storage/{a-z}.data`` so the on-disk
# shards visibly grow *during* a crawl instead of only appearing at the
# finalize pass. Tuned to amortise the exporter cost (full trie walk) over
# a reasonable number of pages. Override via ``config.FLUSH_EVERY_N``.
FLUSH_EVERY_N = int(_cfg("FLUSH_EVERY_N", 25))


# --------------------------------------------------------------- reset gate
#
# Set by the ``/api/system/reset`` endpoint before it purges the on-disk
# state. Any worker still winding down (finalize pass, periodic flush) sees
# the flag and skips its disk writes, so a late ``_flush_to_disk()`` cannot
# resurrect shards or ``atlas_store.json`` we just deleted.
#
# The event is module-global so every worker thread (past, present, future)
# shares it without having to be re-wired. It is cleared again by
# ``allow_flushes()`` once the reset completes, letting subsequent crawls
# flush normally.
GLOBAL_RESET_EVENT = threading.Event()


def abort_pending_flushes() -> None:
    """Block every worker's disk writes until ``allow_flushes()`` is called."""
    GLOBAL_RESET_EVENT.set()


def allow_flushes() -> None:
    """Re-enable worker disk writes after a reset completes."""
    GLOBAL_RESET_EVENT.clear()


def _flushes_aborted() -> bool:
    return GLOBAL_RESET_EVENT.is_set()


def _make_ssl_context() -> ssl.SSLContext:
    """Development-scope SSL context: certificate validation bypassed.

    The bypass is intentional and tracked in recommendation.md. Production
    deployments must restore ``ssl.create_default_context()`` semantics.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ------------------------------------------------------------------- worker


class CrawlerWorker(threading.Thread):
    """BFS web crawler thread. One instance per crawl job."""

    def __init__(
        self,
        job_id: str,
        seed_url: str,
        max_depth: int = DEFAULT_MAX_DEPTH,
        hit_rate: float = DEFAULT_HIT_RATE,
        max_capacity: int = DEFAULT_MAX_CAPACITY,
        max_urls: int = DEFAULT_MAX_URLS,
    ):
        super().__init__(name=f"CrawlerWorker-{job_id}", daemon=True)
        if not job_id:
            raise ValueError("job_id is required")
        if not seed_url:
            raise ValueError("seed_url is required")

        self.job_id = job_id
        self.seed_url = seed_url
        self.max_depth = max(0, int(max_depth))
        self.hit_rate = max(0.1, float(hit_rate))
        self.max_urls = max(1, int(max_urls))

        # Queue capacity is now honored as-is — independent of ``max_urls``.
        #   * max_capacity=1000, max_urls=100   -> 1000 slots, crawler stops
        #                                          after fetching 100 URLs.
        #   * max_capacity=1000, max_urls=10000 -> 1000 slots, newcomers past
        #                                          capacity evict the head
        #                                          (ring buffer) so newly
        #                                          discovered URLs are still
        #                                          admitted at the tail.
        requested_capacity = max(1, int(max_capacity))
        self._requested_queue_capacity = requested_capacity

        self.queue = CrawlerQueue(
            job_id=job_id,
            max_capacity=requested_capacity,
            logger=self._log,
        )

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # unset() means paused

        self.crawled_count = 0
        self.fetch_errors = 0
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.status_label = "idle"

        self._ssl_context = _make_ssl_context()

    # ---------------------------------------------------------- control API
    def pause(self) -> None:
        if not self._stop_event.is_set():
            self._pause_event.clear()
            self.status_label = "paused"
            self._log("info", f"[{self.job_id}] paused")

    def resume(self) -> None:
        if not self._stop_event.is_set():
            self._pause_event.set()
            self.status_label = "running"
            self._log("info", f"[{self.job_id}] resumed")

    def stop(self) -> None:
        self._stop_event.set()
        # Release the pause gate so the loop can observe the stop flag.
        self._pause_event.set()
        self._log("info", f"[{self.job_id}] stop requested")

    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def is_stopping(self) -> bool:
        return self._stop_event.is_set()

    # ---------------------------------------------------------------- run()
    def run(self) -> None:
        self.started_at = time.time()
        self.status_label = "running"
        self._log(
            "info",
            f"[{self.job_id}] started seed={self.seed_url} "
            f"depth={self.max_depth} hit_rate={self.hit_rate} "
            f"max_urls={self.max_urls}",
        )
        try:
            self._seed_frontier()
            self._crawl_loop()
        except Exception as exc:  # defensive — never crash the thread
            self._log("error", f"[{self.job_id}] worker crashed: {exc!r}")
        finally:
            self._finalize()

    def _seed_frontier(self) -> None:
        try:
            seed = validate_url(self.seed_url)
        except ValueError as exc:
            self._log("error", f"[{self.job_id}] invalid seed URL: {exc}")
            self._stop_event.set()
            return
        self.queue.push(seed, 0)

    def _crawl_loop(self) -> None:
        while not self._stop_event.is_set():
            # Honor pause — wake periodically to re-check the stop flag.
            if self.is_paused():
                self._pause_event.wait(timeout=0.5)
                continue

            if self.crawled_count >= self.max_urls:
                # Drain the frontier so the dashboard's "pending" gauge
                # collapses to zero and we stop holding memory for URLs the
                # worker will never fetch.
                dropped = self.queue.clear()
                self._log(
                    "info",
                    f"[{self.job_id}] max_urls={self.max_urls} reached — "
                    f"discarded {dropped} pending URL(s)",
                )
                break

            item = self.queue.pop()
            if item is None:
                # Empty queue: short backoff, then try once more before exit.
                time.sleep(EMPTY_QUEUE_BACKOFF_SECONDS)
                if len(self.queue) == 0:
                    break
                continue

            url = item.get("url")
            depth = int(item.get("depth", 0))
            if not url:
                continue

            # Strict global dedup: before any fetch, consult the persisted
            # visited_urls TTL set. This spans jobs — even a brand-new crawl
            # will skip a URL that was successfully indexed in the last hour.
            if self._is_visited_recently(url):
                self._log(
                    "info",
                    f"[{self.job_id}] dedup skip (<= {VISITED_TTL_SECONDS}s TTL): {url}",
                )
                continue

            fetched = self._fetch(url)
            # Rate-limit after every fetch attempt — successful or not.
            time.sleep(1.0 / self.hit_rate)

            if fetched is None:
                continue

            body, final_url = fetched
            parser = self._parse(body, final_url)
            self.crawled_count += 1

            self._store_page_metadata(
                url=url,
                title=parser.title,
                snippet=parser.snippet,
                depth=depth,
                origin=self.seed_url,
            )
            self._index_tokens(
                text=parser.text,
                url=url,
                depth=depth,
                origin=self.seed_url,
            )

            # Mark visited only after a successful fetch+index so transient
            # fetch errors don't lock a URL out of the queue for an hour.
            self._mark_visited(url)

            # Traceability: emit an explicit per-URL indexed log line so the
            # Status dashboard's log panel shows every successfully indexed
            # URL (e.g. "[INFO] Indexed: https://example.com/page1").
            self._log("info", f"Indexed: {url}")
            self._log(
                "info",
                f"[{self.job_id}] crawled depth={depth} "
                f"url={url} links={len(parser.links)}",
            )

            # Incremental on-disk flush so ``data/storage/{a-z}.data`` shards
            # visibly grow while the crawl is still running instead of only
            # appearing at finalize. Cheap enough at 1-per-FLUSH_EVERY_N since
            # the exporter writes shards atomically and hit_rate sleep
            # dominates the per-page cost anyway.
            if (
                FLUSH_EVERY_N > 0
                and self.crawled_count % FLUSH_EVERY_N == 0
            ):
                self._flush_to_disk()

            if depth < self.max_depth:
                # Early-exit shortcut: if we've already lined up enough URLs
                # to satisfy ``max_urls``, skip the enqueue step entirely.
                # The BFS frontier alone (pending) plus already-fetched pages
                # (crawled_count) covers everything the loop will ever touch.
                if (self.crawled_count + len(self.queue)) < self.max_urls:
                    self._enqueue_children(parser.links, depth + 1)

    # ---------------------------------------------------------------- fetch
    def _fetch(self, url: str) -> Optional[Tuple[str, str]]:
        """Fetch a single URL. Returns (body_text, final_url) or None."""
        try:
            safe_url = validate_url(url)
        except ValueError as exc:
            self._log("warn", f"[{self.job_id}] SSRF/validation block: {exc}")
            return None

        request = urllib.request.Request(
            safe_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en,*;q=0.5",
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=HTTP_TIMEOUT_SECONDS,
                context=self._ssl_context,
            ) as response:
                ctype = (response.headers.get("Content-Type") or "").lower()
                if "html" not in ctype and "xml" not in ctype:
                    # Skip binary payloads — image/pdf/zip/etc.
                    return None

                raw = response.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raw = raw[:MAX_BODY_BYTES]

                charset = response.headers.get_content_charset() or "utf-8"
                body = raw.decode(charset, errors="replace")
                return body, response.geturl()
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            self.fetch_errors += 1
            self._log("error", f"[{self.job_id}] fetch failed {url}: {exc}")
        except (TimeoutError, ValueError, ssl.SSLError, OSError) as exc:
            self.fetch_errors += 1
            self._log("error", f"[{self.job_id}] fetch error {url}: {exc}")
        except Exception as exc:
            self.fetch_errors += 1
            self._log(
                "error", f"[{self.job_id}] fetch unexpected {url}: {exc!r}"
            )
        return None

    # ---------------------------------------------------------------- parse
    @staticmethod
    def _parse(body: str, final_url: str) -> AtlasHTMLParser:
        parser = AtlasHTMLParser(base_url=final_url)
        try:
            parser.feed(body)
        except Exception:
            # Malformed HTML — accept whatever the parser managed to gather.
            pass
        finally:
            try:
                parser.close()
            except Exception:
                pass
        return parser

    # ---------------------------------------------------------- enqueueing
    def _enqueue_children(self, links: Iterable[str], next_depth: int) -> None:
        """Normalize, SSRF-check, and push every discovered link.

        Two narrow exception filters — a bare ``except Exception`` here
        would swallow real bugs (e.g. ``AttributeError`` from a malformed
        parser result) and make them look like legitimate SSRF rejections.
        """
        for link in links:
            try:
                candidate = normalize_url(link)
            except (ValueError, TypeError):
                continue
            # Pre-validate so we skip private/loopback before it hits the
            # queue. validate_url() may do DNS — that is acceptable at this
            # cadence since hit_rate bounds us anyway.
            try:
                validate_url(candidate)
            except ValueError:
                continue
            self.queue.push(candidate, next_depth)

    # ---------------------------------------------------------------- dedup
    def _is_visited_recently(self, url: str) -> bool:
        """Read-only check against the global visited-URL TTL set.

        Source of truth: ``NoSQLStore.data['visited_urls']`` — a dict of
        ``sha256(url) -> expiry_ts``. The check is non-destructive so a URL
        that fails to fetch isn't locked out of the queue; the companion
        ``_mark_visited()`` sets the TTL only after a successful fetch+index.
        """
        url_hash = sha256_url(url)
        now = time.time()
        try:
            from storage.nosql import NoSQLStore  # lazy — store owned by Indexer
            db = _get_store(NoSQLStore)
        except Exception:
            return False

        try:
            with _store_lock(db):
                visited = db.data.setdefault("visited_urls", {})
                # Opportunistic GC of expired entries — bounded at 64 per pass.
                swept = _sweep_expired(visited, now, budget=64)
                expiry = visited.get(url_hash)
                # GC is an actual mutation — mark the store dirty so the
                # 5-second sync daemon will flush it instead of waiting for
                # the worker's next explicit _flush_to_disk tick.
                if swept:
                    _mark_store_dirty(db)
                return isinstance(expiry, (int, float)) and expiry > now
        except Exception:
            return False

    def _mark_visited(self, url: str) -> None:
        """Record a successful fetch+index in the global visited-URL set."""
        url_hash = sha256_url(url)
        expiry = time.time() + VISITED_TTL_SECONDS
        try:
            from storage.nosql import NoSQLStore  # lazy — store owned by Indexer
            db = _get_store(NoSQLStore)
        except Exception:
            return
        try:
            with _store_lock(db):
                visited = db.data.setdefault("visited_urls", {})
                visited[url_hash] = expiry
                # Mark dirty so the sync daemon persists the TTL extension
                # without waiting for the next _flush_to_disk tick.
                _mark_store_dirty(db)
        except Exception:
            # Dedup is advisory — never take the worker down because of it.
            pass

    # --------------------------------------------------------- index + store
    def _index_tokens(
        self, text: str, url: str, depth: int, origin: str
    ) -> None:
        """Tokenize ``text`` and insert each token into the AtlasTrie.

        Uses :func:`core.normalize.tokenize` so indexing is symmetric with
        the query-side tokenization in ``search/engine.py``. Any drift
        between these two call sites silently breaks exact-match search for
        Turkish text (capital I / İ disambiguation), so we intentionally
        share one helper.
        """
        if not text:
            return
        try:
            from storage.trie import AtlasTrie  # lazy — trie owned by Indexer
            trie = _get_store(AtlasTrie)
        except Exception:
            return

        for token in normalize_tokenize(text):
            try:
                trie.insert(token, url=url, depth=depth, origin=origin)
            except TypeError:
                # Accept alternate trie.insert() signatures without crashing.
                try:
                    trie.insert(token, url, depth, origin)
                except Exception:
                    continue
            except Exception:
                continue

    def _store_page_metadata(
        self,
        url: str,
        title: str,
        snippet: str,
        depth: int,
        origin: str,
    ) -> None:
        try:
            from storage.nosql import NoSQLStore
            db = _get_store(NoSQLStore)
        except Exception:
            return
        try:
            with _store_lock(db):
                metadata = db.data.setdefault("metadata", {})
                metadata[url] = {
                    "title": sanitize_html_input(title),
                    "snippet": sanitize_html_input(snippet),
                    "depth": int(depth),
                    "origin": origin,
                    "ts": time.time(),
                }
                _mark_store_dirty(db)
        except Exception:
            pass

    # ---------------------------------------------------------- disk flush
    def _flush_to_disk(self) -> None:
        """Persist the NoSQLStore + export the Trie shards.

        Called both periodically from :meth:`_crawl_loop` (every
        ``FLUSH_EVERY_N`` URLs) and once more from :meth:`_finalize`. Any
        failure here is logged and swallowed — the crawler must never die
        because of a disk hiccup, and readers tolerate a partially-written
        shard because the exporter uses atomic ``tmp + os.replace`` writes.

        Honors :data:`GLOBAL_RESET_EVENT`: if a reset is in progress we
        deliberately skip both the store save and the trie export so this
        worker cannot resurrect files the reset handler just deleted.

        The store and trie each expose their own ``is_dirty`` flag. We
        consult both here so an idle flush tick (every ``FLUSH_EVERY_N``
        URLs across N concurrent workers) doesn't pay for a full JSON
        snapshot + trie walk when nothing has changed since the last flush.
        """
        if _flushes_aborted():
            self._log(
                "info",
                f"[{self.job_id}] flush skipped: global reset in progress",
            )
            return

        # Persist NoSQLStore state — only if writes have happened since the
        # last successful save(). ``save()`` itself walks the entire data
        # dict, so gating on is_dirty saves real work at steady state.
        try:
            from storage.nosql import NoSQLStore
            db = _get_store(NoSQLStore)
            saver = getattr(db, "save", None)
            dirty_attr = getattr(db, "is_dirty", True)
            should_save = bool(dirty_attr) if not callable(saver) else bool(dirty_attr)
            if callable(saver) and should_save:
                saver()
        except Exception as exc:
            self._log("warn", f"[{self.job_id}] store.save failed: {exc!r}")

        # Export the Trie to flat-files via ETLExporter (Indexer-owned).
        # Same gating — ``walk()`` holds the trie RLock across a full
        # traversal, which would stall every other live worker's inserts
        # for the duration of an export that had nothing new to write.
        try:
            from storage import exporter as _exporter
            from storage.trie import AtlasTrie as _Trie
            trie_inst = _get_store(_Trie)
            trie_dirty = getattr(trie_inst, "is_dirty", True)
            if callable(trie_dirty):
                trie_dirty = trie_dirty()  # type: ignore[assignment]
            export_fn = (
                getattr(_exporter, "export_all_to_legacy_format", None)
                or getattr(getattr(_exporter, "ETLExporter", None),
                           "export_all_to_legacy_format", None)
            )
            if callable(export_fn) and bool(trie_dirty):
                export_fn()
                mark = getattr(trie_inst, "mark_exported", None)
                if callable(mark):
                    mark()
        except Exception as exc:
            self._log("warn", f"[{self.job_id}] trie export failed: {exc!r}")

    # ----------------------------------------------------------- finalize
    def _finalize(self) -> None:
        self.ended_at = time.time()
        self.status_label = (
            "stopped" if self._stop_event.is_set() else "completed"
        )
        self._log(
            "info",
            f"[{self.job_id}] finalize: status={self.status_label} "
            f"crawled={self.crawled_count} errors={self.fetch_errors} "
            f"pending={len(self.queue)} dropped={self.queue.dropped}",
        )
        # Skip every disk-touching step when a reset is in flight — otherwise
        # we race the orchestrator and re-create shards / history rows it
        # just purged.
        if _flushes_aborted():
            self._log(
                "info",
                f"[{self.job_id}] finalize flush skipped: reset in progress",
            )
            return

        # Archive the final snapshot into ``NoSQLStore.data['job_history']``
        # *before* the flush so the on-disk ``atlas_store.json`` carries the
        # terminal state. Without this step, completed/stopped jobs vanish
        # on the next restart because ``_JOBS`` is in-memory only and no
        # other code path writes them to history.
        self._archive_final_state()

        self._flush_to_disk()

    def _archive_final_state(self) -> None:
        """Upsert a final snapshot into ``NoSQLStore.data['job_history']``.

        Called from :meth:`_finalize` for both natural completion and an
        explicit stop. If ``api/routes.py::_archive_worker`` later runs for
        the same ``job_id`` (user clicked Delete), it overwrites this row
        with ``status="deleted"`` — whichever terminal state is most
        recent wins.
        """
        try:
            from storage.nosql import NoSQLStore
            db = _get_store(NoSQLStore)
        except Exception:
            return
        try:
            record = self.snapshot()
            record["archived_at"] = time.time()
            record["archive_reason"] = self.status_label
            with _store_lock(db):
                history = db.data.setdefault("job_history", [])
                if not isinstance(history, list):
                    history = []
                    db.data["job_history"] = history
                # Replace the most recent entry for this job_id so repeat
                # finalize calls (shouldn't happen, but defensive) don't
                # accumulate duplicates.
                replaced = False
                for idx in range(len(history) - 1, -1, -1):
                    existing = history[idx]
                    if (
                        isinstance(existing, dict)
                        and existing.get("job_id") == self.job_id
                    ):
                        history[idx] = record
                        replaced = True
                        break
                if not replaced:
                    history.append(record)
                # Bound history ring — matches the cap used in routes.py.
                overflow = len(history) - 500
                if overflow > 0:
                    del history[:overflow]
                _mark_store_dirty(db)
        except Exception as exc:
            self._log(
                "warn", f"[{self.job_id}] archive final state failed: {exc!r}"
            )

    # -------------------------------------------------------------- logging
    def _log(self, level: str, message: str) -> None:
        """Append a log line to NoSQLStore.job_logs[job_id] (bounded ring)."""
        try:
            from storage.nosql import NoSQLStore
            db = _get_store(NoSQLStore)
        except Exception:
            return
        try:
            with _store_lock(db):
                logs = db.data.setdefault("job_logs", {})
                ring = logs.get(self.job_id)
                if not isinstance(ring, deque):
                    ring = deque(ring or (), maxlen=LOG_RING_SIZE)
                    logs[self.job_id] = ring
                ring.append(
                    {"ts": time.time(), "level": level, "msg": message}
                )
                _mark_store_dirty(db)
        except Exception:
            # Logging must never take the worker down.
            pass

    # --------------------------------------------------------- telemetry
    def snapshot(self) -> dict:
        """Per-job telemetry snapshot for the status API."""
        now = time.time()
        uptime = (
            (self.ended_at or now) - self.started_at
            if self.started_at
            else 0.0
        )
        effective_speed = (
            self.crawled_count / uptime if uptime > 0 else 0.0
        )
        return {
            "job_id": self.job_id,
            "seed_url": self.seed_url,
            "status": self.status_label,
            "paused": self.is_paused(),
            "stopping": self.is_stopping(),
            "crawled": self.crawled_count,
            # Target URL budget requested at job creation. Surfaced to the
            # status dashboard so "Total crawled" can render as "N / max_urls"
            # instead of a raw count with no progress reference.
            "max_urls": self.max_urls,
            "max_depth": self.max_depth,
            "hit_rate": self.hit_rate,
            "errors": self.fetch_errors,
            "uptime_seconds": uptime,
            "effective_speed": effective_speed,
            "queue": self.queue.snapshot(),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


# ----------------------------------------------------------------- helpers


def _sweep_expired(visited: dict, now: float, budget: int) -> int:
    """Remove up to ``budget`` expired entries from ``visited`` in place.

    Returns the number of entries removed so callers can decide whether the
    sweep was a real mutation (and therefore warrants a ``mark_dirty()``).
    """
    removed = 0
    for key in list(visited.keys()):
        if removed >= budget:
            break
        expiry = visited.get(key)
        if isinstance(expiry, (int, float)) and expiry <= now:
            visited.pop(key, None)
            removed += 1
    return removed


def _mark_store_dirty(instance) -> None:
    """Call ``instance.mark_dirty()`` if available, else fall back to ``_dirty``.

    The worker holds ``db.lock`` across its compound writes, so this helper
    must be invoked from *inside* that critical section — the underlying
    ``NoSQLStore.mark_dirty()`` bumps a write-sequence counter that the
    ``save()`` path uses to detect concurrent writes and defer clearing the
    dirty flag. Doing it outside the lock would re-introduce the race.
    """
    mark = getattr(instance, "mark_dirty", None)
    if callable(mark):
        try:
            mark()
            return
        except Exception:
            pass
    # Legacy fallback — never crash the worker on a missing hook.
    try:
        instance._dirty = True  # type: ignore[attr-defined]
    except Exception:
        pass


def _get_store(cls):
    """Resolve the NoSQLStore / AtlasTrie singleton across common patterns."""
    getter = getattr(cls, "get_instance", None)
    if callable(getter):
        return getter()
    # Fallback to a plain constructor — expected to be idempotent singleton.
    return cls()


def _store_lock(instance):
    """Return a context manager that serializes writes on ``instance.lock``."""
    lock = getattr(instance, "lock", None)
    if lock is not None:
        return lock
    # Null-context fallback so callers never need to branch.
    return _NullLock()


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

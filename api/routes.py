"""
All REST and UI page routes.

PRD refs: §8 API Summary, §2.3 UI / Monitoring.

REST endpoints:
    POST   /api/crawler/create
    POST   /api/crawler/pause/{id}
    POST   /api/crawler/resume/{id}
    POST   /api/crawler/stop/{id}
    DELETE /api/crawler/delete/{id}
    GET    /api/metrics
    GET    /api/crawler/status/{id}
    GET    /api/crawler/list
    GET    /api/crawler/history
    GET    /api/search
    POST   /api/crawler/export
    POST   /api/system/reset

UI (Jinja2 SSR) pages:
    /, /crawler, /status, /search

The route layer owns a small in-memory registry of live ``CrawlerWorker``
threads keyed by ``job_id``. Persistent per-job state (logs, metadata, queue
counters, history) lives in ``NoSQLStore`` so it survives reboots even though
the worker threads themselves do not.

Owner agent: UI Agent (pages) + Crawler/Search agents (REST).
"""

from __future__ import annotations

import glob
import inspect
import logging
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from core import config
from core.security import validate_url
from crawler.worker import (
    CrawlerWorker,
    abort_pending_flushes,
    allow_flushes,
)
from search.engine import SearchEngine
from storage.exporter import export_all_to_legacy_format
from storage.nosql import NoSQLStore
from storage.trie import AtlasTrie


logger = logging.getLogger("atlas.routes")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

router = APIRouter()
templates = Jinja2Templates(directory=str(_PROJECT_ROOT / "templates"))


def _template_response_accepts_request_first() -> bool:
    """Detect Starlette's ``TemplateResponse`` signature at import time.

    Starlette 0.29+ introduced ``TemplateResponse(request, name, context)``
    and deprecated the old ``TemplateResponse(name, context)`` form; in
    Starlette 1.0+ the new form is the *only* accepted shape. We probe the
    installed version once here so :func:`_render_page` can dispatch to
    the right call and render correctly on both legacy (0.27-era) and
    modern deployments without forcing a pin in ``requirements.txt``.
    """
    try:
        sig = inspect.signature(templates.TemplateResponse)
        params = list(sig.parameters.values())
    except (TypeError, ValueError):
        return False
    # The new API's first positional parameter is named "request"; the
    # old API's first parameter is named "name".
    for param in params:
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        return param.name == "request"
    return False


_TR_REQUEST_FIRST = _template_response_accepts_request_first()


def _render_page(request: Request, name: str, extra: Optional[Dict[str, Any]] = None):
    """Render a Jinja2 page in a way that works on Starlette 0.27 and 1.0.

    Both call shapes get the same context dict (with ``request`` included —
    modern Starlette tolerates an extra ``request`` key in the context).
    The ``atlas_config`` dict injected here is what ``templates/base.html``
    emits as ``window.ATLAS_CONFIG`` for ``static/js/app.js`` to consume.
    """
    context: Dict[str, Any] = {
        "request": request,
        "atlas_config": _atlas_config_for_templates(),
    }
    if extra:
        context.update(extra)
    if _TR_REQUEST_FIRST:
        return templates.TemplateResponse(request, name, context)
    return templates.TemplateResponse(name, context)


def _atlas_config_for_templates() -> Dict[str, Any]:
    """Subset of ``core.config`` exposed to server-rendered pages.

    Emitted as ``window.ATLAS_CONFIG`` by ``base.html`` so ``static/js/app.js``
    can read polling cadences and form defaults from the single source of
    truth in ``core/config.py`` instead of hardcoding its own copies.

    Only values the frontend actually consumes are included — ``STORE_PATH``
    and the like intentionally stay server-side.
    """
    return {
        "POLL_INTERVAL_MS": int(getattr(config, "POLL_INTERVAL_MS", 2000)),
        "UI_TICK_INTERVAL_MS": int(getattr(config, "UI_TICK_INTERVAL_MS", 1000)),
        "DEFAULT_MAX_DEPTH": int(getattr(config, "DEFAULT_MAX_DEPTH", 3)),
        "DEFAULT_HIT_RATE": float(getattr(config, "DEFAULT_HIT_RATE", 2.0)),
        "DEFAULT_MAX_CAPACITY": int(getattr(config, "DEFAULT_MAX_CAPACITY", 10_000)),
        "DEFAULT_MAX_URLS": int(getattr(config, "DEFAULT_MAX_URLS", 1000)),
        "LOG_RING_SIZE": int(getattr(config, "LOG_RING_SIZE", 50)),
    }




# ---------------------------------------------------------------- worker registry

# Active ``CrawlerWorker`` threads keyed by job_id. Never serialized — this
# mirrors the live thread map, while NoSQLStore holds the durable state.
_JOBS: Dict[str, CrawlerWorker] = {}
_JOBS_LOCK = threading.Lock()


def _register_worker(worker: CrawlerWorker) -> None:
    with _JOBS_LOCK:
        _JOBS[worker.job_id] = worker


def _get_worker(job_id: str) -> Optional[CrawlerWorker]:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def _drop_worker(job_id: str) -> Optional[CrawlerWorker]:
    with _JOBS_LOCK:
        return _JOBS.pop(job_id, None)


def _all_workers() -> List[CrawlerWorker]:
    with _JOBS_LOCK:
        return list(_JOBS.values())


def shutdown_all_workers(timeout_per_worker: float = 2.0) -> None:
    """Stop every tracked worker. Called from the app lifespan shutdown."""
    for worker in _all_workers():
        try:
            worker.stop()
        except Exception:
            pass
    for worker in _all_workers():
        try:
            worker.join(timeout=timeout_per_worker)
        except Exception:
            pass
    with _JOBS_LOCK:
        _JOBS.clear()


# ---------------------------------------------------------------- payloads


class CrawlerCreateRequest(BaseModel):
    """Body schema for ``POST /api/crawler/create``."""

    seed_url: str = Field(..., min_length=1)
    max_depth: int = Field(default=config.DEFAULT_MAX_DEPTH, ge=0, le=32)
    hit_rate: float = Field(default=config.DEFAULT_HIT_RATE, gt=0.0, le=100.0)
    max_capacity: int = Field(
        default=config.DEFAULT_MAX_CAPACITY, ge=1, le=10_000_000
    )
    max_urls: int = Field(default=config.DEFAULT_MAX_URLS, ge=1, le=10_000_000)


# ---------------------------------------------------------------- helpers


def _db() -> NoSQLStore:
    return NoSQLStore.get_instance()


def _trie() -> AtlasTrie:
    return AtlasTrie.get_instance()


def _read_job_logs(job_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Return up to ``limit`` log entries for ``job_id`` (oldest first)."""
    db = _db()
    try:
        with db.lock:
            # The worker writes its ring under ``job_logs`` (see worker._log).
            logs = db.data.get("job_logs") or {}
            ring = logs.get(job_id)
            if ring is None:
                return []
            snapshot = list(ring)
    except Exception:
        return []
    if limit > 0:
        snapshot = snapshot[-limit:]
    return snapshot


def _job_descriptor(worker: CrawlerWorker) -> Dict[str, Any]:
    """Compact descriptor used by the job-list endpoint."""
    snap = worker.snapshot()
    return {
        "job_id": snap.get("job_id"),
        "seed_url": snap.get("seed_url"),
        "status": snap.get("status"),
        "paused": snap.get("paused"),
        "stopping": snap.get("stopping"),
        "crawled": snap.get("crawled"),
        "max_urls": snap.get("max_urls"),
        "max_depth": snap.get("max_depth"),
        "hit_rate": snap.get("hit_rate"),
        "errors": snap.get("errors"),
        "effective_speed": snap.get("effective_speed"),
        "uptime_seconds": snap.get("uptime_seconds"),
        "started_at": snap.get("started_at"),
        "ended_at": snap.get("ended_at"),
        "queue": snap.get("queue"),
    }


def _persist_job_record(worker: CrawlerWorker, payload: Dict[str, Any]) -> None:
    """Write job metadata into ``NoSQLStore.data['jobs']`` for durability."""
    db = _db()
    try:
        with db.lock:
            jobs = db.data.setdefault("jobs", {})
            jobs[worker.job_id] = {
                "job_id": worker.job_id,
                "seed_url": worker.seed_url,
                "max_depth": worker.max_depth,
                "hit_rate": worker.hit_rate,
                "max_capacity": worker.queue.max_capacity,
                "max_urls": worker.max_urls,
                "created_at": time.time(),
                "request": payload,
            }
            db.mark_dirty()
    except Exception as exc:
        logger.warning("persist job record failed: %r", exc)


def _purge_job_state(job_id: str, origin_url: Optional[str] = None) -> Dict[str, int]:
    """Cascading delete: remove per-job residue + every trace of ``origin_url``.

    Order of operations matters. The trie and metadata purges both serialize
    on their own locks, never on ``_JOBS_LOCK``, so live search requests keep
    working while the delete is in flight:

        1. Drop per-job buckets from NoSQLStore (jobs/logs/queues).
        2. Purge AtlasTrie postings whose ``origin_url`` matches — this makes
           /api/search stop returning hits from this crawl.
        3. Purge NoSQLStore metadata + visited_urls TTL entries for the same
           origin so a re-crawl starts cold and the dashboard counters shrink.

    Returns a telemetry dict suitable for surfacing in the HTTP response.
    """
    db = _db()
    telemetry: Dict[str, int] = {}
    try:
        with db.lock:
            # Iterate the per-job bucket keys actually present in the schema
            # (see ``storage.nosql._default_schema``). ``crawler_logs`` was
            # renamed to ``job_logs`` during the H1/M1 sweep; the dead key
            # previously listed here was a no-op left behind from the rename.
            for key in (
                "jobs",
                "job_logs",
                "seen_urls",
                "crawler_queue",
                "job_queue_counts",
            ):
                bucket = db.data.get(key)
                if isinstance(bucket, dict):
                    bucket.pop(job_id, None)
            db.mark_dirty()
    except Exception as exc:
        logger.warning("purge job state failed: %r", exc)

    if not origin_url:
        return telemetry

    try:
        trie_stats = _trie().purge_by_origin(origin_url)
        telemetry["trie_postings_removed"] = int(
            trie_stats.get("postings_removed", 0)
        )
        telemetry["trie_words_unindexed"] = int(
            trie_stats.get("words_unindexed", 0)
        )
    except Exception as exc:
        logger.warning("trie cascade purge failed for %s: %r", origin_url, exc)

    try:
        store_stats = db.purge_origin(origin_url)
        telemetry["metadata_removed"] = int(store_stats.get("metadata_removed", 0))
        telemetry["visited_removed"] = int(store_stats.get("visited_removed", 0))
    except Exception as exc:
        logger.warning("store cascade purge failed for %s: %r", origin_url, exc)

    return telemetry


def _archive_worker(worker: CrawlerWorker, reason: str) -> None:
    """Append a final snapshot to ``job_history`` before purging live state.

    When ``reason == "deleted"`` the archived ``status`` is forced to
    ``"deleted"`` (overriding whatever ``worker.status_label`` held — usually
    "stopped" because delete always calls ``worker.stop()`` first). This lets
    the UI distinguish a user-initiated delete from an organic stop / crash:
    deleting a job that was *already* stopped still ends up tagged "deleted".
    """
    db = _db()
    try:
        record = worker.snapshot()
        record["archived_at"] = time.time()
        record["archive_reason"] = reason
        if reason == "deleted":
            record["status"] = "deleted"
            record["deleted"] = True
        with db.lock:
            history = db.data.setdefault("job_history", [])
            if isinstance(history, list):
                # Upsert: if the worker's own ``_archive_final_state`` (or a
                # prior archive call) already wrote a row for this job_id,
                # replace it so the latest terminal state wins. Delete
                # overrides stopped/completed, and a repeat delete is a
                # no-op instead of a duplicate entry.
                replaced = False
                for idx in range(len(history) - 1, -1, -1):
                    existing = history[idx]
                    if (
                        isinstance(existing, dict)
                        and existing.get("job_id") == worker.job_id
                    ):
                        history[idx] = record
                        replaced = True
                        break
                if not replaced:
                    history.append(record)
                # Bound the history ring so the JSON store doesn't grow
                # unbounded across a long-running process.
                overflow = len(history) - 500
                if overflow > 0:
                    del history[:overflow]
            db.mark_dirty()
    except Exception as exc:
        logger.warning("archive job failed: %r", exc)


# =========================================================================
# HTML (Jinja2 SSR) page routes
# =========================================================================


@router.get("/", response_class=None, include_in_schema=False)
def page_index(request: Request):
    return _render_page(request, "index.html")


@router.get("/crawler", response_class=None, include_in_schema=False)
def page_crawler(request: Request):
    return _render_page(request, "crawler.html")


@router.get("/status", response_class=None, include_in_schema=False)
def page_status(request: Request):
    return _render_page(request, "status.html")


@router.get("/search", response_class=None, include_in_schema=False)
def page_search(request: Request):
    return _render_page(request, "search.html")


# =========================================================================
# JSON REST endpoints
# =========================================================================


# ---------------------------------------------------------------- crawler


@router.post("/api/crawler/create")
def api_crawler_create(body: CrawlerCreateRequest) -> Dict[str, Any]:
    """Create and start a crawl job."""
    try:
        safe_seed = validate_url(body.seed_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid seed_url: {exc}")

    job_id = uuid.uuid4().hex[:12]

    try:
        worker = CrawlerWorker(
            job_id=job_id,
            seed_url=safe_seed,
            max_depth=body.max_depth,
            hit_rate=body.hit_rate,
            max_capacity=body.max_capacity,
            max_urls=body.max_urls,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _register_worker(worker)
    _persist_job_record(worker, body.model_dump())
    worker.start()

    return {
        "job_id": job_id,
        "status": "running",
        "seed_url": safe_seed,
        "max_depth": body.max_depth,
        "hit_rate": body.hit_rate,
        "max_capacity": body.max_capacity,
        "max_urls": body.max_urls,
    }


@router.post("/api/crawler/pause/{job_id}")
def api_crawler_pause(job_id: str) -> Dict[str, Any]:
    worker = _get_worker(job_id)
    if worker is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    worker.pause()
    return {"job_id": job_id, "status": worker.status_label, "paused": worker.is_paused()}


@router.post("/api/crawler/resume/{job_id}")
def api_crawler_resume(job_id: str) -> Dict[str, Any]:
    worker = _get_worker(job_id)
    if worker is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    worker.resume()
    return {"job_id": job_id, "status": worker.status_label, "paused": worker.is_paused()}


@router.post("/api/crawler/stop/{job_id}")
def api_crawler_stop(job_id: str) -> Dict[str, Any]:
    worker = _get_worker(job_id)
    if worker is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    worker.stop()
    return {"job_id": job_id, "status": worker.status_label, "stopping": True}


@router.delete("/api/crawler/delete/{job_id}")
def api_crawler_delete(job_id: str) -> Dict[str, Any]:
    worker = _drop_worker(job_id)
    if worker is None:
        # Recover the origin from the durable jobs record so an orphaned
        # persistent entry (e.g. after a server restart) can still cascade.
        origin = _lookup_origin_url(job_id)
        telemetry = _purge_job_state(job_id, origin_url=origin)
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"unknown job_id: {job_id}",
                "cascade": telemetry,
            },
        )

    try:
        worker.stop()
        worker.join(timeout=2.0)
    except Exception as exc:
        logger.warning("delete: worker stop/join failed: %r", exc)

    _archive_worker(worker, reason="deleted")
    # Ensure the trie and metadata buckets lose every trace of this crawl so
    # subsequent /api/search calls cannot surface wikipedia.org (etc.) hits.
    telemetry = _purge_job_state(job_id, origin_url=worker.seed_url)
    return {"job_id": job_id, "deleted": True, "cascade": telemetry}


def _lookup_origin_url(job_id: str) -> Optional[str]:
    """Resolve a job's seed_url from NoSQLStore.jobs (pre-purge read)."""
    db = _db()
    try:
        with db.lock:
            jobs = db.data.get("jobs") or {}
            record = jobs.get(job_id) if isinstance(jobs, dict) else None
            if isinstance(record, dict):
                seed = record.get("seed_url") or (
                    (record.get("request") or {}).get("seed_url")
                    if isinstance(record.get("request"), dict) else None
                )
                if isinstance(seed, str) and seed:
                    return seed
    except Exception:
        return None
    return None


@router.get("/api/crawler/status/{job_id}")
def api_crawler_status(job_id: str) -> Dict[str, Any]:
    """Per-job telemetry snapshot + most recent log ring."""
    worker = _get_worker(job_id)
    if worker is not None:
        snap = worker.snapshot()
    else:
        # Fall back to the archived record when the thread has been reaped.
        snap = _lookup_historical_snapshot(job_id)
        if snap is None:
            raise HTTPException(
                status_code=404, detail=f"unknown job_id: {job_id}"
            )
    snap["logs"] = _read_job_logs(job_id, limit=config.LOG_RING_SIZE)
    return snap


def _lookup_historical_snapshot(job_id: str) -> Optional[Dict[str, Any]]:
    db = _db()
    try:
        with db.lock:
            history = db.data.get("job_history") or []
            for record in reversed(history):
                if isinstance(record, dict) and record.get("job_id") == job_id:
                    return dict(record)
    except Exception:
        return None
    return None


@router.get("/api/crawler/list")
def api_crawler_list() -> Dict[str, Any]:
    """All live crawl jobs with their current telemetry."""
    workers = _all_workers()
    jobs = [_job_descriptor(w) for w in workers]
    # Surface the most recently started first.
    jobs.sort(key=lambda j: j.get("started_at") or 0, reverse=True)
    return {"jobs": jobs, "count": len(jobs)}


@router.get("/api/crawler/history")
def api_crawler_history(limit: int = Query(default=100, ge=1, le=1000)) -> Dict[str, Any]:
    """Archived jobs in most-recent-first order."""
    db = _db()
    try:
        with db.lock:
            history = list(db.data.get("job_history") or [])
    except Exception:
        history = []
    history.reverse()
    return {"history": history[:limit], "count": len(history)}


@router.post("/api/crawler/export")
def api_crawler_export() -> Dict[str, Any]:
    """Manually flush the AtlasTrie to ``data/storage/*.data``."""
    try:
        counts = export_all_to_legacy_format()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"export failed: {exc!r}")
    return {"exported": True, "shards": counts}


# ---------------------------------------------------------------- metrics


@router.get("/api/metrics")
def api_metrics() -> Dict[str, Any]:
    """Global telemetry aggregated across every live worker."""
    workers = _all_workers()
    snapshots = [w.snapshot() for w in workers]

    total_crawled = sum(int(s.get("crawled") or 0) for s in snapshots)
    total_errors = sum(int(s.get("errors") or 0) for s in snapshots)
    pending_total = sum(
        int((s.get("queue") or {}).get("pending") or 0) for s in snapshots
    )
    dropped_total = sum(
        int((s.get("queue") or {}).get("dropped_total") or 0) for s in snapshots
    )
    active_jobs = sum(1 for s in snapshots if not s.get("ended_at"))
    paused_jobs = sum(1 for s in snapshots if s.get("paused"))

    trie = _trie()
    try:
        word_count = trie.word_count
        node_count = trie.node_count
    except Exception:
        word_count = 0
        node_count = 0

    db = _db()
    try:
        with db.lock:
            history_len = len(db.data.get("job_history") or [])
            visited_len = len(db.data.get("visited_urls") or {})
            metadata_len = len(db.data.get("metadata") or {})
    except Exception:
        history_len = visited_len = metadata_len = 0

    return {
        "total_crawled": total_crawled,
        "total_errors": total_errors,
        "pending_total": pending_total,
        "dropped_total": dropped_total,
        "active_jobs": active_jobs,
        "paused_jobs": paused_jobs,
        "total_jobs": len(snapshots),
        "history_count": history_len,
        "visited_urls": visited_len,
        "indexed_pages": metadata_len,
        "trie_words": word_count,
        "trie_nodes": node_count,
        "ts": time.time(),
    }


# ---------------------------------------------------------------- search


@router.get("/api/search")
def api_search(
    q: str = Query(default="", description="Free-text query"),
    limit: int = Query(default=10, ge=0, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> Dict[str, Any]:
    """Run a search against the AtlasTrie + NoSQLStore metadata."""
    query = (q or "").strip()
    if not query:
        return {
            "query": "",
            "results": [],
            "limit": limit,
            "offset": offset,
            "count": 0,
            "total": 0,
        }

    t0 = time.time()
    try:
        payload = SearchEngine.query_with_total(query, limit=limit, offset=offset)
    except Exception as exc:
        logger.exception("search failed")
        raise HTTPException(status_code=500, detail=f"search failed: {exc!r}")
    elapsed_ms = int((time.time() - t0) * 1000)

    results = payload.get("results") or []
    total = int(payload.get("total") or 0)

    return {
        "query": query,
        "results": results,
        "limit": limit,
        "offset": offset,
        # ``count`` is the size of the current page; ``total`` is the full
        # number of hits across every page — the UI displays the latter.
        "count": len(results),
        "total": total,
        "elapsed_ms": elapsed_ms,
    }


# ---------------------------------------------------------------- admin


def _purge_shard_files() -> Dict[str, int]:
    """Delete every ``data/storage/*.data`` trie shard from disk.

    The NoSQLStore JSON file is *not* touched here — ``NoSQLStore.clear_all``
    owns its own file lifecycle (under the store's lock, with ``_dirty``
    cleared so the sync daemon can't immediately rewrite it). This helper
    is strictly for the shards the exporter produces.

    Returns ``{"shards_removed": N}`` for telemetry.
    """
    removed = 0
    data_dir = getattr(config, "DATA_DIR", os.path.join("data", "storage"))
    try:
        for path in glob.glob(os.path.join(data_dir, "*.data")):
            try:
                os.remove(path)
                removed += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("reset: failed to remove shard %s: %r", path, exc)
    except OSError as exc:
        logger.warning("reset: glob of data_dir failed: %r", exc)
    return {"shards_removed": removed}


@router.post("/api/system/reset")
def api_system_reset() -> Dict[str, Any]:
    """Clean Reset Protocol — stop workers, wipe memory, purge disk.

    Ordering is load-bearing:

      1. ``abort_pending_flushes()`` trips the module-level reset event so
         any worker currently in ``_flush_to_disk()`` or ``_finalize()``
         skips its disk writes. Set *before* shutdown so a worker woken by
         ``stop()`` and racing into finalize cannot beat us to the disk.
      2. ``shutdown_all_workers(timeout_per_worker=2.0)`` signals every
         worker and waits up to 2s per thread. The raised timeout (vs the
         previous 0.25s) gives an in-flight ``urlopen()`` time to return
         cleanly — we still don't need a *successful* flush from it, the
         reset event handles that, but we do want the thread to exit.
      3. Clear the trie (memory only — ``AtlasTrie`` has no filesystem
         coupling by design).
      4. ``NoSQLStore.clear_all(delete_file=True)`` resets the in-memory
         schema *and* removes ``atlas_store.json``, with ``_dirty`` cleared
         so the background sync daemon does not immediately rewrite it.
      5. Purge the ``data/storage/*.data`` shards.
      6. ``allow_flushes()`` clears the reset event so any crawls started
         after this point flush to disk normally again.

    There is no ``time.sleep`` grace period — thread join + the reset
    event cover the "is it safe to delete the file?" question without
    guessing at a timing window.
    """
    abort_pending_flushes()
    telemetry: Dict[str, Any] = {}
    try:
        try:
            shutdown_all_workers(timeout_per_worker=2.0)
        except Exception as exc:
            logger.warning("reset: worker shutdown failed: %r", exc)

        try:
            _trie().clear()
        except Exception as exc:
            logger.warning("reset: trie clear failed: %r", exc)

        try:
            store_stats = _db().clear_all(delete_file=True)
            telemetry["store_removed"] = int(store_stats.get("store_removed", 0))
        except Exception as exc:
            logger.warning("reset: store clear_all failed: %r", exc)
            telemetry["store_removed"] = 0

        shard_stats = _purge_shard_files()
        telemetry["shards_removed"] = int(shard_stats.get("shards_removed", 0))
    finally:
        # Always re-open the flush gate, even if a step above raised —
        # otherwise a transient error would permanently disable persistence
        # for every subsequent crawl in this process.
        allow_flushes()

    return {"reset": True, "ts": time.time(), **telemetry}


__all__ = ["router", "shutdown_all_workers"]

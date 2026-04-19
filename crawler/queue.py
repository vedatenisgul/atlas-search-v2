"""
CrawlerQueue — per-job FIFO frontier with ring-buffer capacity.

PRD refs: §5 Backpressure Design.

Semantics:
    push(url, depth) -> bool         # False only when url_hash seen before
    pop() -> dict | None             # {"url": ..., "depth": ...}
    status() -> "Healthy" | "Back-pressure Active" | "Critical (Queue Full)"

    Maintains a per-job seen_urls set of SHA-256 hashes for O(1) dedup.
    When len(queue) >= max_capacity the push evicts the oldest head entry
    (popleft) and appends the newcomer at the tail — so newly-discovered
    URLs are never silently lost, they just displace older frontier items.
    The ``dropped`` counter still tracks evictions for telemetry, and the
    Back-pressure / Critical status labels still fire as the queue fills.

Owner agent: Crawler Agent.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Callable, Optional

from core import config
from core.security import sha256_url


# Backpressure kicks in at 80% capacity — same threshold the UI uses.
_BACKPRESSURE_RATIO = 0.8


def _config_int(name: str, fallback: int) -> int:
    try:
        value = getattr(config, name, fallback)
        return int(value) if value is not None else fallback
    except (TypeError, ValueError):
        return fallback


class CrawlerQueue:
    """Thread-safe FIFO frontier for a single crawl job."""

    HEALTHY = "Healthy"
    BACKPRESSURE = "Back-pressure Active"
    CRITICAL = "Critical (Queue Full)"

    def __init__(
        self,
        job_id: str,
        max_capacity: Optional[int] = None,
        logger: Optional[Callable[[str, str], None]] = None,
    ):
        if max_capacity is None:
            max_capacity = _config_int("DEFAULT_MAX_CAPACITY", 10_000)
        if max_capacity <= 0:
            raise ValueError("max_capacity must be positive")

        self.job_id = job_id
        self.max_capacity = int(max_capacity)

        self._lock = threading.Lock()
        self._deque: deque = deque()
        self._seen_hashes: set = set()

        self._enqueued_total = 0
        self._dropped_total = 0
        self._popped_total = 0

        self._logger = logger or (lambda level, msg: None)

    # --------------------------------------------------------------- mutate
    def push(self, url: str, depth: int) -> bool:
        """Enqueue ``url`` at ``depth``.

        Always appends at the tail. If the queue is at capacity the oldest
        head entry is evicted first (ring-buffer) so newly-discovered URLs
        are never silently dropped. Returns False only when ``url`` has
        already been seen by this queue instance (per-job dedup).
        """
        if not url or depth < 0:
            return False

        url_hash = sha256_url(url)
        evicted_url: Optional[str] = None
        dropped_total = 0
        with self._lock:
            if url_hash in self._seen_hashes:
                return False

            if len(self._deque) >= self.max_capacity:
                # Ring buffer: pop the head to make room for the newcomer.
                # We intentionally do NOT remove the evicted URL's hash from
                # seen_hashes — once seen, it stays deduped for this job so
                # we don't cycle the same URLs through the frontier forever.
                head = self._deque.popleft()
                evicted_url = head.get("url") if isinstance(head, dict) else None
                self._dropped_total += 1
                dropped_total = self._dropped_total

            self._deque.append({"url": url, "depth": int(depth)})
            self._seen_hashes.add(url_hash)
            self._enqueued_total += 1

        if evicted_url is not None:
            # Log outside the lock — logger callbacks may take NoSQLStore.lock.
            self._safe_log(
                "warn",
                f"[{self.job_id}] queue full, evicted head url={evicted_url} "
                f"to admit {url} (dropped_total={dropped_total})",
            )
        return True

    def pop(self) -> Optional[dict]:
        """Dequeue the oldest frontier entry, or ``None`` if empty."""
        with self._lock:
            if not self._deque:
                return None
            item = self._deque.popleft()
            self._popped_total += 1
            return item

    def clear(self) -> int:
        """Drop all pending entries. Returns the count removed."""
        with self._lock:
            removed = len(self._deque)
            self._deque.clear()
            return removed

    # ------------------------------------------------------------- observe
    def __len__(self) -> int:
        with self._lock:
            return len(self._deque)

    def size(self) -> int:
        return len(self)

    def status(self) -> str:
        with self._lock:
            pending = len(self._deque)
            if pending >= self.max_capacity:
                return self.CRITICAL
            ratio = pending / self.max_capacity
            if ratio >= _BACKPRESSURE_RATIO:
                return self.BACKPRESSURE
            return self.HEALTHY

    def snapshot(self) -> dict:
        """Point-in-time telemetry snapshot for the status API."""
        with self._lock:
            pending = len(self._deque)
            return {
                "job_id": self.job_id,
                "pending": pending,
                "capacity": self.max_capacity,
                "enqueued_total": self._enqueued_total,
                "dropped_total": self._dropped_total,
                "popped_total": self._popped_total,
                "status": (
                    self.CRITICAL
                    if pending >= self.max_capacity
                    else self.BACKPRESSURE
                    if pending / self.max_capacity >= _BACKPRESSURE_RATIO
                    else self.HEALTHY
                ),
                "seen_count": len(self._seen_hashes),
            }

    @property
    def dropped(self) -> int:
        return self._dropped_total

    @property
    def enqueued(self) -> int:
        return self._enqueued_total

    @property
    def popped(self) -> int:
        return self._popped_total

    # --------------------------------------------------------------- logs
    def _safe_log(self, level: str, msg: str) -> None:
        try:
            self._logger(level, msg)
        except Exception:
            # Logger must never take the queue down.
            pass

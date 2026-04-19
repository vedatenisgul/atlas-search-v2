"""
Configuration constants.

PRD refs: §2.1, §5 Backpressure Design, §7 Persistence.

All tunables for the Atlas Search runtime live here. Two naming tiers are
exposed side-by-side:

    * Canonical names from the Indexer Agent spec
      (DEFAULT_MAX_DEPTH, DEFAULT_HIT_RATE, DEFAULT_QUEUE_CAPACITY,
       DEFAULT_MAX_URLS, TTL_SECONDS, SYNC_INTERVAL).
    * Legacy aliases already used by crawler/search/UI
      (DEFAULT_MAX_CAPACITY, VISITED_TTL_SECONDS, JSON_FLUSH_INTERVAL_SECONDS).

Both always point at the same underlying value — never drift them apart.

Owner agent: Indexer Agent.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------- crawl loop
DEFAULT_MAX_DEPTH: int = 3
DEFAULT_HIT_RATE: float = 2.0
DEFAULT_MAX_URLS: int = 1000

# ---------------------------------------------------------------- queue caps
DEFAULT_QUEUE_CAPACITY: int = 10_000
DEFAULT_MAX_CAPACITY: int = DEFAULT_QUEUE_CAPACITY

# ---------------------------------------------------------------- dedup TTL
TTL_SECONDS: int = 3600
VISITED_TTL_SECONDS: int = TTL_SECONDS

# ---------------------------------------------------------------- persistence
SYNC_INTERVAL: int = 5
JSON_FLUSH_INTERVAL_SECONDS: int = SYNC_INTERVAL

DATA_DIR: str = os.path.join("data", "storage")
STORE_PATH: str = "atlas_store.json"

# ---------------------------------------------------------------- worker knobs
EMPTY_QUEUE_BACKOFF_SECONDS: int = 2
LOG_RING_SIZE: int = 50

# Trie export cadence — crawler flushes every N successfully-indexed URLs.
# Kept high enough that the per-page cost stays dominated by the hit_rate
# sleep; low enough that data/storage/{a-z}.data shards visibly grow while
# a long crawl is still running. Overridable here and via the env.
FLUSH_EVERY_N: int = 25

# ---------------------------------------------------------------- security
# Per-process DNS resolution cache size used by ``core.security``. Small
# enough to stay in-memory for any realistic crawl while still amortising
# getaddrinfo() across the frontier of links for a given host.
DNS_CACHE_MAX: int = 256

# ---------------------------------------------------------------- UI
POLL_INTERVAL_MS: int = 2000
# Frontend uptime-interpolation cadence between polls. Finer-grained than
# POLL_INTERVAL_MS so the "Uptime" card ticks smoothly in the browser.
UI_TICK_INTERVAL_MS: int = 1000


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_HIT_RATE",
    "DEFAULT_MAX_URLS",
    "DEFAULT_QUEUE_CAPACITY",
    "DEFAULT_MAX_CAPACITY",
    "TTL_SECONDS",
    "VISITED_TTL_SECONDS",
    "SYNC_INTERVAL",
    "JSON_FLUSH_INTERVAL_SECONDS",
    "DATA_DIR",
    "STORE_PATH",
    "EMPTY_QUEUE_BACKOFF_SECONDS",
    "LOG_RING_SIZE",
    "FLUSH_EVERY_N",
    "DNS_CACHE_MAX",
    "POLL_INTERVAL_MS",
    "UI_TICK_INTERVAL_MS",
]

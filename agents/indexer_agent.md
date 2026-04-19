# Indexer Agent

**Role:** Storage and Persistence Engineer  
**Tool:** Cursor Composer  
**Phase:** Phase 3

---

## Responsibility

Owns the storage subsystem and persistence layer. Does not touch crawling logic, search query logic, or UI.

## Prompt

> "You are the Indexer Agent. Build the storage subsystem for Atlas Search. Files to create: storage/nosql.py, storage/trie.py, storage/exporter.py, core/config.py.
>
> Requirements:
> - NoSQLStore: singleton, thread-safe with threading.Lock(), persists to atlas_store.json via atomic write (write .tmp then os.replace()), background daemon flushes every 5 seconds. Schema keys: seen_urls, visited_urls, crawler_queue, job_queue_counts, crawler_logs, jobs, job_history, metadata
> - AtlasTrie: singleton, thread-safe with threading.RLock(). Each TrieNode has children dict and metadata dict keyed by URL storing term_frequency/depth/origin_url. Supports insert() and search() with exact and prefix match
> - ETLExporter: export_all_to_legacy_format() writes word/url/origin/depth/freq lines to data/storage/a-z.data files. import_legacy_data_to_trie() reconstructs Trie on boot
> - config.py: DEFAULT_MAX_DEPTH=3, DEFAULT_HIT_RATE=2.0, DEFAULT_QUEUE_CAPACITY=10000, DEFAULT_MAX_URLS=1000, TTL_SECONDS=3600, SYNC_INTERVAL=5"

## Inputs

- Architect Agent schema documentation
- Crawler Agent storage usage patterns

## Outputs

- `storage/nosql.py` — thread-safe singleton with atomic writes and daemon sync
- `storage/trie.py` — prefix tree with RLock, insert/search, word_list tracking
- `storage/exporter.py` — ETL export and import with round-trip fidelity
- `core/config.py` — all system constants

## Issues Raised

- Trie grows unbounded in long crawls — no eviction policy
- visited_urls grows indefinitely in atlas_store.json

## Orchestrator Response

Both noted as known limitations, added to recommendation.md. Not in scope.

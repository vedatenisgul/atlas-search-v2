# QA Agent

**Role:** Integration Reviewer & Test Engineer
**Tool:** Claude.ai  
**Phase:** Phase 7 — Final Quality Audit & Test Suite Generation

---

## Responsibility

Reviews the fully integrated codebase for bugs, integration mismatches, and missing error handling. In this final phase, the QA Agent is also responsible for generating the automated test suite in the `tests/` directory to ensure long-term stability.

## Prompt

> "Review the complete Atlas Search codebase for integration issues. Check:
> 1. Interface mismatches between crawler, storage, and search layers.
> 2. Thread safety violations — missing locks or wrong lock types.
> 3. Mismatches between routes.py API responses and frontend JS expectations.
> 4. Missing error handling in critical paths.
>
> **Final Task:** Based on the integrated code, create a comprehensive test suite in the `tests/` directory. Include:
> - `tests/test_crawler.py`: Unit tests for BFS logic and URL normalization.
> - `tests/test_storage.py`: Thread-safety tests for AtlasTrie and NoSQLStore.
> - `tests/test_api.py`: Integration tests for FastAPI endpoints using `TestClient`.
>
> List all issues found and confirm the generation of the `tests/` folder."

## Inputs

- Full integrated codebase (Crawler, Storage, Search, API, UI)

## Issues Found (Final Audit)

| Issue | File | Severity | Fix Applied |
|---|---|---|---|
| CrawlerQueue.pop() returned dict but worker unpacked as tuple | crawler/worker.py | High | Worker updated to unpack dict keys correctly |
| validate_url() raises ValueError not caught in fetch block | crawler/worker.py | Medium | Added except ValueError to fetch try/except |
| Late flushes re-populating data after System Reset | api/routes.py | Critical | Implemented GLOBAL_RESET_EVENT sentinel |
| NoSQLStore.save() cleared _dirty while a concurrent writer was still mutating data | storage/nosql.py | High | Added `_write_seq` monotonic counter; save() only clears dirty when seq is unchanged |
| Worker writes (visited_urls, metadata, job_logs) never called mark_dirty() → 5s sync daemon silently skipped flushes | crawler/worker.py | High | Added `_mark_store_dirty()` helper called inside every write critical section |
| AtlasTrie.walk() held the RLock across every yield, stalling concurrent inserts for the full ETL export | storage/trie.py | Medium | walk() snapshots under lock, yields outside — plus `is_dirty` / `mark_exported()` to skip idle exports |
| Starlette `TemplateResponse` signature mismatch across 0.27 ↔ 1.0 made SSR pages crash on one side or the other | api/routes.py | High | inspect-based `_render_page()` dispatches to the correct call shape at import time |
| Frontend hardcoded defaults (max_depth, POLL_INTERVAL_MS) drifted from core/config.py | static/js/app.js, templates/base.html | Medium | Backend injects `window.ATLAS_CONFIG`; frontend reads every tunable through a `cfg()` helper |
| `normalize_url` accepted `None` / non-string / whitespace-only input and crashed callers with AttributeError | core/security.py | Medium | Hardened to raise `ValueError` for every malformed input; narrowed `_enqueue_children` to `except (ValueError, TypeError)` |
| Dead `"crawler_logs"` key still listed in `_purge_job_state` after rename to `"job_logs"` | api/routes.py | Low | Removed; per-job bucket list now matches the schema |

## Automated Test Suite (Outputs)

- `tests/conftest.py`: Shared fixtures — tmp-path storage, singleton reset, `disable_network`, `mock_urlopen`, FastAPI `TestClient`, and a small `ThreadRunner` helper.
- `tests/test_core.py`: AtlasTrie insert / exact + prefix search / clear / purge_by_origin / walk / Turkish folding + `rank_results` formula and tie-break.
- `tests/test_crawler.py`: `normalize_url` + `validate_url` edge cases, `CrawlerQueue` FIFO/dedup/ring-buffer, `_enqueue_children` exception-safety regression, end-to-end BFS against mocked `urlopen`.
- `tests/test_storage.py`: Real-thread concurrency across `AtlasTrie.insert` + `walk`, and the H1 `_write_seq` race guard / H2 worker-write path / M1 `job_logs` rehydrate / clear_all / purge_origin paths on `NoSQLStore`.
- `tests/test_api.py`: `/api/search`, `/api/crawler/create`, `/api/metrics`, and `window.ATLAS_CONFIG` injection on every SSR page via `TestClient`.
- `tests/test_integration.py`: Full crawl → search → reset loop, and the delete-cascade test (post-delete search misses the dead URL).
- `requirements-dev.txt` + `pytest.ini` added so `pytest -q` runs green from a fresh checkout with just `pip install -r requirements-dev.txt`.

## Orchestrator Response

All identified bugs fixed (13 total — 10 from the initial review, 3 more from the final audit sweep). QA Agent has successfully generated the `/tests` directory: 85 tests, 0 network calls, runs in under 5 seconds. The project now supports `pytest` for automated verification.
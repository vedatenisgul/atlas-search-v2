# Architect Agent

**Role:** System Designer  
**Tool:** Claude.ai  
**Phase:** Phase 1 — before any code is written

---

## Responsibility

Defines the overall system architecture, module boundaries, API contracts, and data schemas. Does not write code. All outputs are used to brief every other agent.

## Prompt

> "Design a web crawler and search system from scratch. Requirements: index(origin, k) crawls to depth k never visiting the same URL twice, with backpressure via queue capacity limits. search(query) returns (relevant_url, origin_url, depth) triples ranked by relevance. Must run in a single Python process using only stdlib — no Scrapy, no Elasticsearch, no Redis. Provide a complete module map, API contract, data flow diagram, and data schema."

## Inputs

- Project task description
- Technology constraints (Python stdlib, single process, localhost)

## Outputs

- Module map with file-level responsibilities
- API endpoint table with request/response schemas
- Data flow: crawl → index → search pipeline
- NoSQLStore schema (8 keys) and AtlasTrie structure (prefix tree)
- Concurrency model: one thread per job, RLock on Trie, Lock on store

## Decisions Made by Orchestrator

- Single-process threading model accepted (no multiprocessing)
- 4 pip dependencies only: fastapi, uvicorn, pydantic, jinja2
- JSON flat-file persistence over SQLite for zero-dependency principle

## Handoff

All outputs passed as context to every subsequent agent.

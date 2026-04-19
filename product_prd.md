# Atlas Search — Product Requirements Document

**Version:** 1.0  
**Date:** 2026-04-18  
**Stack:** Python 3.9+ · FastAPI · Vanilla JS/CSS/HTML · Custom NoSQL KV Store

---

## 1. Executive Summary

Atlas Search is a zero-dependency web crawling platform and real-time search engine built entirely from Python's standard library. It replaces traditional infrastructure (Scrapy, Elasticsearch, Redis, PostgreSQL) with hand-rolled, thread-safe in-memory data structures.

| Concern | Implementation |
|---|---|
| Crawl Queue | `NoSQLStore.crawler_queue` — FIFO list with per-job capacity guards |
| Visited Dedup | SHA-256 hashed URL set with TTL-based freshness |
| Inverted Index | `AtlasTrie` — prefix-tree with per-node URL→frequency metadata |
| Persistence | Atomic JSON flush (`atlas_store.json`) + ETL flat-files (`a-z.data`) |
| Concurrency | `threading.Thread` workers + `threading.RLock()` synchronization |
| Frontend | Jinja2 templates, Vanilla JS polling, flat minimal design system |

---

## 2. Core Requirements

### 2.1 index(origin, k)

- Accept a seed URL and max crawl depth k
- Never crawl the same URL twice (SHA-256 dedup + TTL)
- Enforce per-job queue capacity ceiling as backpressure (default 10,000)
- Rate-limit outbound requests via configurable hit_rate (req/s)
- Index all extracted text into AtlasTrie for search
- Persist index to flat-files on job completion for cross-reboot availability

### 2.2 search(query)

- Accept a free-text query string
- Return a list of triples: (relevant_url, origin_url, depth)
- Rank results by (term_frequency x 10) + 1000 - (depth x 5)
- Support pagination via limit and offset
- Operate concurrently while indexing is active (RLock-safe reads)
- Turkish locale-aware case folding

### 2.3 UI / Monitoring

- Initiate crawl jobs with configurable parameters
- Real-time 6-card telemetry dashboard (2s polling)
- Per-job and global backpressure status display
- Live log streams (last 50 entries per job)
- Pause / Resume / Stop / Delete job controls
- Search interface with paginated result cards

---

## 3. System Architecture

```
FastAPI ASGI Server (Uvicorn)
  /crawler · /status · /search  <- Jinja2 SSR pages
  /api/*                         <- JSON REST endpoints

CrawlerWorker(s)          SearchEngine
threading.Thread          static class
fetch -> parse ->         query -> trie
index -> enqueue          -> rank -> paginate

Shared In-Memory Singletons
NoSQLStore (db)  .  AtlasTrie (trie_db)
threading.Lock       threading.RLock

atlas_store.json  .  data/storage/*.data
```

---

## 4. Module Map

```
atlas_search/
├── api/
│   ├── main.py          # App factory, lifespan hooks
│   └── routes.py        # All REST + UI page routes
├── core/
│   ├── parser.py        # AtlasHTMLParser
│   ├── security.py      # SSRF prevention, sanitization
│   └── config.py        # Configuration constants
├── crawler/
│   ├── queue.py         # CrawlerQueue abstraction
│   └── worker.py        # CrawlerWorker thread
├── search/
│   ├── engine.py        # SearchEngine.query()
│   └── ranking.py       # rank_results()
├── storage/
│   ├── nosql.py         # NoSQLStore singleton
│   ├── trie.py          # AtlasTrie singleton
│   └── exporter.py      # ETL export/import
├── static/
│   ├── js/app.js        # Vanilla JS polling + DOM
│   └── css/style.css    # Flat minimal design system
├── templates/           # Jinja2 HTML pages
├── data/storage/        # ETL flat-files
└── tests/               # Unit tests
```

---

## 5. Backpressure Design

| Layer | Mechanism |
|---|---|
| Queue capacity | enqueue() drops URLs when count >= max_capacity |
| Rate limiting | time.sleep(1 / hit_rate) after each fetch |
| Empty queue backoff | Worker sleeps 2s when queue is empty |
| Dedup relief | seen_urls (per-job) + visited_urls with TTL (global) |

Status labels: Healthy / Back-pressure Active / Critical (Queue Full)

---

## 6. Search Ranking

```
relevance_score = (term_frequency x 10) + 1000 - (depth x 5)
```

Multi-word queries: frequencies summed, minimum depth taken per URL.

---

## 7. Persistence

| Mechanism | Trigger | What is saved |
|---|---|---|
| ETL export | Worker finally block + POST /api/crawler/export | Full Trie to flat-files |
| ETL import | Server startup lifespan hook | flat-files to in-memory Trie |
| JSON flush | Every 5s daemon + db.save() on worker exit | Full NoSQLStore state |

---

## 8. API Summary

| Method | Path | Description |
|---|---|---|
| POST | /api/crawler/create | Create and start a crawl job |
| POST | /api/crawler/pause/{id} | Pause a running job |
| POST | /api/crawler/resume/{id} | Resume a paused job |
| POST | /api/crawler/stop/{id} | Stop a job |
| DELETE | /api/crawler/delete/{id} | Stop, archive, and purge |
| GET | /api/metrics | Global telemetry |
| GET | /api/crawler/status/{id} | Per-job telemetry |
| GET | /api/crawler/list | Active jobs |
| GET | /api/crawler/history | Job history |
| GET | /api/search | Search query |
| POST | /api/crawler/export | Manual Trie export |
| POST | /api/system/reset | Full system reset |

---

## 9. Dependencies

```
fastapi>=0.100.0
uvicorn>=0.23.0
pydantic>=2.0
jinja2>=3.1.2
```

All other functionality uses Python's standard library.

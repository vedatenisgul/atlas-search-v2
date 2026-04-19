# Atlas Search

A zero-dependency web crawler and real-time search engine built from scratch on FastAPI. No Scrapy, no Elasticsearch, no Redis — just Python's standard library and four pip packages.

---

## How It Works

Atlas has three subsystems that share one process:

**Crawler** — One `CrawlerWorker` daemon thread per job. Give it a seed URL and a depth limit. It fetches pages with `urllib`, parses links and visible text with `html.parser`, and indexes every token into an in-memory prefix tree (`AtlasTrie`). Each fetch is rate-limited (`time.sleep(1 / hit_rate)`) and validated against an SSRF allow-list (blocks private / loopback / link-local / cloud-metadata IPs). URLs are deduplicated per-job (SHA-256 set) and globally (1-hour TTL).

**Search** — `SearchEngine` is a static query façade over the Trie + metadata store. User queries are tokenized with the same Turkish-aware case fold the indexer uses (`İ → i`, `I → ı`, diacritics preserved), each token is looked up with an exact match, postings are merged by URL (sum `term_frequency`, take min `depth`), ranked, paginated, and hydrated with title + snippet from `NoSQLStore`. Search runs concurrently while crawls are active — the trie's `RLock` guarantees consistent reads.

**Persistence** — Two layers:
- `atlas_store.json` — the full `NoSQLStore` (jobs, logs, metadata, visited TTL, history) flushed atomically (`.tmp` + `os.replace`) every 5 s by a background daemon, and once more on shutdown.
- `data/storage/{a-z,_misc}.data` — ETL flat-files sharded by the first letter of each indexed word. Written every 25 successfully-indexed URLs and on job finalize, re-imported on boot so search is available immediately after a restart without re-crawling.

---

## Quick Start

```bash
git clone <repo-url>
cd atlas-search-v2
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn api.main:app --reload
```

Open `http://localhost:8000` in your browser.

---

## Pages

| Page | URL | What it does |
|---|---|---|
| Home | `/` | Landing page with navigation |
| Crawler | `/crawler` | Create a new crawl job |
| Status | `/status` | Real-time dashboard — queue depth, backpressure, logs, job controls |
| Search | `/search` | Query the indexed pages |

---

## Creating a Crawl Job

`POST /api/crawler/create` — parameters validated by Pydantic:

| Field | Description | Default | Range |
|---|---|---|---|
| `seed_url` | Starting URL (http / https, SSRF-checked) | — | required |
| `max_depth` | BFS hops from seed | 3 | 0–32 |
| `hit_rate` | Requests per second | 2.0 | 0.1–100 |
| `max_capacity` | Per-job queue capacity | 10,000 | 1–10,000,000 |
| `max_urls` | Stop after N successfully indexed pages | 1,000 | 1–10,000,000 |

---

## Searching

`GET /api/search?q=<query>&limit=<n>&offset=<n>`

Results carry: `url`, `origin_url`, `depth`, `frequency`, `relevance_score`, `title`, `snippet`. The ranking formula is:

```
relevance_score = (term_frequency × 10) + 1000 − (depth × 5)
```

Ties are broken by shallower depth, then by URL for deterministic output. Multi-word queries sum frequencies and take the minimum depth per URL. The Trie does exact-match only — `ist` will not match `istanbul`.

---

## Dashboard

`/status` renders a 6-card grid polling every 2 s: **Status**, **Total Crawled** (as `N / max_urls`), **Pending Queue**, **Back-pressure**, **Effective Speed**, **Uptime**. Between polls the browser interpolates the uptime card at 1 s resolution so it ticks smoothly. Below the grid: live log panel (last 50 entries per job, `LOG_RING_SIZE`) and **Pause / Resume / Stop / Delete** controls.

---

## Backpressure

The queue is a ring buffer. At 80% capacity the dashboard flips to **Back-pressure Active**; at 100% every `push()` evicts the oldest head entry before appending the newcomer at the tail, so newly-discovered URLs are never silently lost — they just displace older frontier items. The `dropped` counter still tracks every eviction for telemetry. The label **Critical (Queue Full)** fires when the queue is at capacity.

Extra relief valves: per-job SHA-256 dedup set, global `visited_urls` TTL (1 h), and a 2 s sleep whenever the queue drains empty.

---

## Persistence & Reset

The search index survives server restarts via the ETL flat-files. Active crawl-queue state is not preserved — create a new job to continue after a restart.

`POST /api/system/reset` performs the Clean Reset Protocol: trip the global flush-abort event, join every worker (2 s timeout each), clear the Trie, wipe `NoSQLStore` + delete `atlas_store.json`, remove every `data/storage/*.data` shard, then re-open the flush gate. Workers still mid-finalize cannot resurrect deleted files because they honor the abort event.

---

## Running Tests

```bash
python3 -m venv venv_test
source venv_test/bin/activate
pip install -r requirements-dev.txt
pytest
```

The suite covers core utilities, storage (Trie + NoSQLStore + ETL round-trip), crawler worker/queue/security, API endpoints, and full end-to-end integration. Configuration is in `pytest.ini`.

---

## Project Layout

```
atlas-search-v2/
├── api/            # FastAPI app factory + REST/UI routes
├── core/           # config, text normalize, HTML parser, security/SSRF
├── crawler/        # CrawlerWorker thread + CrawlerQueue ring buffer
├── search/         # SearchEngine (static) + ranking
├── storage/        # NoSQLStore, AtlasTrie, ETL exporter
├── templates/      # Jinja2 SSR pages (base / index / crawler / status / search)
├── static/         # Vanilla JS + flat design system CSS
├── data/storage/   # ETL shards (a-z, _misc).data
├── tests/          # pytest suite
├── atlas_store.json
├── requirements.txt
├── requirements-dev.txt
└── pytest.ini
```

See `product_prd.md` for the full product spec and `multi_agent_workflow.md` for how the system was designed agent-by-agent.

---

## Dependencies

```
fastapi>=0.100.0
uvicorn>=0.23.0
pydantic>=2.0
jinja2>=3.1.2
```

Everything else — HTTP client, HTML parser, threading, SSL, hashing, JSON persistence — is Python's standard library.

# Crawler Agent

**Role:** Crawl Subsystem Engineer  
**Tool:** Cursor Composer  
**Phase:** Phase 2

---

## Responsibility

Owns everything related to fetching and parsing web pages. Does not touch search logic, storage internals, or UI.

## Prompt

> "You are the Crawler Agent. Build the web crawling subsystem for Atlas Search. Files to create: crawler/worker.py, crawler/queue.py, core/parser.py, core/security.py.
>
> Requirements:
> - CrawlerWorker extends threading.Thread, runs as daemon
> - BFS crawl limited to max_depth hops from seed URL
> - Per-job URL dedup using SHA-256 hashes stored in NoSQLStore
> - Global visited URL set with 1-hour TTL
> - SSRF protection: validate_url() blocks private/loopback IPs before each fetch
> - Rate limiting: time.sleep(1 / hit_rate) after each fetch
> - Backpressure: CrawlerQueue drops URLs when per-job count >= max_capacity
> - Workers use CrawlerQueue.push() / CrawlerQueue.pop() exclusively
> - AtlasHTMLParser extracts links, visible text, title, snippet using html.parser
> - On exit (finally block): persist job state and export Trie to flat-files
>
> Use only Python stdlib for HTTP (urllib), parsing (html.parser), threading, SSL, hashing."

## Inputs

- Architect Agent system design and module map
- NoSQLStore and AtlasTrie API interfaces

## Outputs

- `crawler/worker.py` — full crawl loop with SSRF validation and rate limiting
- `crawler/queue.py` — CrawlerQueue with capacity guard and logging
- `core/parser.py` — AtlasHTMLParser (links, text, title, snippet)
- `core/security.py` — validate_url() SSRF check, sanitize_html_input()

## Issues Raised

- SSL certificate bypass is a security risk in production
- No robots.txt compliance

## Orchestrator Response

SSL bypass kept for development scope. Both issues added to recommendation.md.

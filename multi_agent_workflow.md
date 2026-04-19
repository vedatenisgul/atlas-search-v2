# Multi-Agent Workflow — Atlas Search
**Orchestrator:** Human Developer (Lead Engineer)

---

## Overview

Atlas Search was built using a structured multi-agent AI orchestration workflow. Seven specialized agents were each responsible for a distinct subsystem, executed **sequentially** — not in parallel. The human developer acted as Lead Engineer and Orchestrator at every step: reviewing outputs, resolving architectural conflicts, enforcing code quality, and making all final system design decisions.

### How the Workflow Was Run

Each agent ran in its own **separate Cursor Composer tab**, opened one at a time. Agents did not run simultaneously — each tab was activated only after the previous agent's output was reviewed and accepted by the orchestrator. This sequential, tab-isolated approach gave the orchestrator full control over handoffs and prevented context bleed between agents.

Every agent was briefed using a consistent two-part prompt format:

```
@product_prd.md @agents/<agent_name>_agent.md

You are the [Agent Name]. Follow the instructions in the
agent file and implement these files:
- path/to/file1.py
- path/to/file2.py

Do not touch [other subsystems].
```

The `@product_prd.md` reference injected the global project requirements into every agent's context. The `@agents/<agent_name>_agent.md` reference injected that agent's specific role, responsibilities, inputs, outputs, and constraints — defined in advance inside an `agents/` directory. This made each agent's scope explicit and bounded, preventing scope creep across the codebase.

---

## Agent Roster

| Agent | Tool | Phase | Responsibility |
|---|---|---|---|
| Architect Agent | Claude.ai | 1 | System design, folder scaffold, module boundaries, API contracts |
| Crawler Agent | Cursor Composer | 2 | Crawl loop, fetch, global dedup, backpressure, queue |
| Indexer Agent | Cursor Composer | 3 | Trie structure, text indexing, ETL persistence, cascading deletes |
| Search Agent | Cursor Composer | 4 | Query pipeline, ranking, pagination, Turkish locale fold |
| API Agent | Cursor Composer | 5 (Ad-hoc) | FastAPI application factory, routing layer, subsystem integration |
| UI Agent | Cursor Composer | 6 | Frontend templates, Tailwind CSS, Alpine.js reactivity |
| QA & Test Agent | Claude.ai | 7 | Code review, bug fixing, automated test suite generation (pytest) |

---

## Agent Interaction Flow

```text
[Architect Agent]  —— Produces: system design, full folder scaffold, module map, API contracts
        |
        v
[Crawler Agent]    —— Produces: crawler/worker.py, crawler/queue.py, core/parser.py
        |
        v
[Indexer Agent]    —— Produces: storage/trie.py, storage/nosql.py, storage/exporter.py, core/config.py
        |
        v
[Search Agent]     —— Produces: search/engine.py, search/ranking.py, core/normalize.py
        |
        v
[API Agent]        —— Produces: api/main.py, api/routes.py  (Gateway Layer — ad-hoc)
        |
        v
[UI Agent]         —— Produces: templates/*.html, static/css/style.css, static/js/app.js
        |
        v
[QA & Test Agent]  —— Reviews all output, fixes integration bugs, produces tests/, pytest.ini, requirements-dev.txt
        |
        v
[Orchestrator]     —— Human validation, architectural pivots, manual interventions, git commit
```

Each arrow represents a sequential handoff. The orchestrator reviewed the output of each phase before activating the next Cursor tab.

---

## Agent Details

---

### Agent 1 — Architect Agent
**Role:** System Designer  
**Tool:** Claude.ai  
**Phase:** Phase 1 — before any code is written

---

#### Responsibility

Defines the overall system architecture, the complete folder and file scaffold, module boundaries, API contracts, and data schemas. The Architect Agent is the only agent that does not write implementation code — its outputs are used to brief every subsequent agent. Crucially, this agent also creates all empty files and the full directory structure so other agents have a well-defined target to fill in.

#### Prompt Format

```
@product_prd.md @agents/architect_agent.md

You are the Architect Agent. Follow your instructions
in the agent file and build your assigned files.

Read this PRD and create the complete folder and file
structure with empty files so other agents can fill them in:

api/__init__.py
api/main.py
api/routes.py
core/__init__.py
core/parser.py
core/security.py
core/config.py
core/normalize.py
crawler/__init__.py
crawler/worker.py
crawler/queue.py
search/__init__.py
search/engine.py
search/ranking.py
storage/__init__.py
storage/nosql.py
storage/trie.py
storage/exporter.py
static/js/app.js
static/css/style.css
templates/base.html
templates/crawler.html
templates/status.html
templates/search.html
templates/index.html
data/storage/.gitkeep
tests/__init__.py
requirements.txt
requirements-dev.txt
pytest.ini
```

#### Inputs
- `product_prd.md` — global project requirements and constraints
- `agents/architect_agent.md` — role definition and output specification
- Technology constraints: Python stdlib, single process, localhost

#### Outputs
- Complete folder and file scaffold with empty files (all paths above, including `core/normalize.py`, `requirements-dev.txt`, and `pytest.ini`)
- Module map with file-level responsibilities
- API endpoint table with request/response schemas
- Data flow diagram: crawl → index → search pipeline
- NoSQLStore schema (8 keys) and AtlasTrie structure (prefix tree)
- Concurrency model: one thread per job, RLock on Trie, Lock on store

#### Decisions Made by Orchestrator
- Single-process threading model accepted (no multiprocessing)
- 4 pip dependencies only: `fastapi`, `uvicorn`, `pydantic`, `jinja2`
- JSON flat-file persistence over SQLite for zero-dependency principle

#### Handoff
All outputs — the scaffold, module map, API contracts, and data schema — were passed as shared context to every subsequent agent via `@product_prd.md`.

---

### Agent 2 — Crawler Agent
**Role:** Data Acquisition Engineer  
**Tool:** Cursor Composer (dedicated tab)  
**Phase:** Phase 2 — after scaffold is in place

---

#### Responsibility

Implements the web crawling subsystem. Responsible for the BFS crawl loop, URL fetching, global deduplication with TTL, SSRF protection, and backpressure management via a bounded queue. Does not touch storage, search, or UI files.

#### Prompt Format

```
@product_prd.md @agents/crawler_agent.md

You are the Crawler Agent. Follow the instructions in the
agent file and implement these files:
- crawler/worker.py
- crawler/queue.py
- core/parser.py
- core/security.py

Do not touch storage, search, or UI files.
```

#### Inputs
- `product_prd.md`
- `agents/crawler_agent.md`
- Empty scaffold files from Architect Agent

#### Outputs
- `crawler/worker.py` — `CrawlerWorker` extending `threading.Thread`, BFS crawl limited to `max_depth`
- `crawler/queue.py` — bounded queue with blocking backpressure (`while self.queue.full(): time.sleep(1)`)
- `core/parser.py` — HTML parsing and link extraction
- `core/security.py` — `validate_url()` for SSRF protection

#### Issues Raised
- **SSL certificate bypass** is a security risk in production environments.

#### Orchestrator Response
- SSL bypass was retained for local development only; flagged for the production roadmap.
- Blocking backpressure (`time.sleep` loop on full queue) was enforced to prevent RAM bloat by prioritizing indexing over redundant network I/O. The initial "drop-on-full" logic was rejected.

#### Handoff
`crawler/worker.py`, `crawler/queue.py`, `core/parser.py`, `core/security.py` passed to Indexer Agent as context.

---

### Agent 3 — Indexer Agent
**Role:** Storage & Data Integrity Engineer  
**Tool:** Cursor Composer (dedicated tab)  
**Phase:** Phase 3 — after crawl layer is complete

---

#### Responsibility

Implements the storage subsystem. Responsible for the thread-safe prefix trie, atomic JSON persistence, ETL export, and cascading delete logic. Does not touch crawler, search, or UI files.

#### Prompt Format

```
@product_prd.md @agents/indexer_agent.md

You are the Indexer Agent. Follow the instructions in the
agent file and implement these files:
- storage/nosql.py
- storage/trie.py
- storage/exporter.py
- core/config.py

Do not touch crawler, search, or UI files.
```

#### Inputs
- `product_prd.md`
- `agents/indexer_agent.md`
- Crawler outputs for interface reference

#### Outputs
- `storage/trie.py` — `AtlasTrie` with `RLock`, prefix lookup, and cascading delete
- `storage/nosql.py` — `NoSQLStore` with atomic JSON write (write-to-temp → rename)
- `storage/exporter.py` — `ETLExporter` for structured data export
- `core/config.py` — centralized configuration constants

#### Issues Raised
- **Unbounded Trie growth** — the trie had no eviction mechanism and grew indefinitely.
- **`visited_urls` accumulation** — the global visited set grew without bound across sessions.

#### Orchestrator Response
- Cascading deletes were enforced: deleting a crawl job now recursively purges all associated word/URL entries from the Trie linked to that origin, maintaining search index accuracy.
- Global URL TTL (1-hour) handles visited set bloat from the crawler side.

#### Handoff
`storage/trie.py`, `storage/nosql.py`, `storage/exporter.py`, `core/config.py` passed to Search Agent.

---

### Agent 4 — Search Agent
**Role:** Query Pipeline Engineer  
**Tool:** Cursor Composer (dedicated tab)  
**Phase:** Phase 4 — after storage layer is complete

---

#### Responsibility

Implements the search query pipeline. Responsible for token normalization with Turkish locale support, exact-match trie lookup, result aggregation, scoring, and pagination. The agent extracted normalization logic into a dedicated shared module (`core/normalize.py`) rather than keeping it inline in `search/engine.py` — a scope expansion into `core/` that was accepted by the orchestrator as the correct architectural decision. Does not touch crawler, storage, or UI files.

#### Prompt Format

```
@product_prd.md @agents/search_agent.md

You are the Search Agent. Follow the instructions in the
agent file and implement these files:
- search/engine.py
- search/ranking.py
- core/normalize.py

Do not touch crawler, storage, or UI files.
```

#### Inputs
- `product_prd.md`
- `agents/search_agent.md`
- `storage/trie.py` interface for lookup

#### Outputs
- `core/normalize.py` — Turkish locale case-folding via `str.translate()`, shared normalization utilities extracted as a standalone `core/` module for reuse across subsystems
- `search/engine.py` — query pipeline: tokenize → normalize (via `core/normalize.py`) → Trie lookup → aggregate per URL
- `search/ranking.py` — scoring formula: `score = (term_frequency × 10) + 1000 − (depth × 5)`, with pagination

#### Orchestrator Response
- Turkish locale case-folding was kept as a core product feature. The agent's decision to extract it into `core/normalize.py` rather than keep it embedded in `search/engine.py` was accepted — it makes the normalization logic reusable by other subsystems (e.g., the crawler's parser) without creating a dependency on the search module.
- TF-IDF scoring was explicitly deferred to a future iteration to preserve delivery velocity.

#### Handoff
`core/normalize.py`, `search/engine.py`, `search/ranking.py` passed to API Agent.

---

### Agent 5 — API Agent *(Ad-hoc — Gap Identified by Orchestrator)*
**Role:** API Gateway Engineer  
**Tool:** Cursor Composer (dedicated tab)  
**Phase:** Phase 5 — identified as missing during integration

---

#### Responsibility

Implements the FastAPI application layer. Responsible for the app factory, static file mounting, and wiring all JSON API routes to the crawler and storage engines. This agent was **not part of the original plan** — it was provisioned mid-project after the orchestrator identified a missing subsystem during integration.

#### Prompt Format

```
@product_prd.md @agents/api_agent.md

You are the API Agent. Follow the instructions in the
agent file and implement these files:
- api/main.py
- api/routes.py

Connect the JSON API routes to the storage and crawler engines.
Do not touch crawler internals, storage internals, or UI files.
```

#### Inputs
- `product_prd.md`
- `agents/api_agent.md` (written on the fly by orchestrator upon discovering the gap)
- `crawler/worker.py`, `storage/nosql.py`, `search/engine.py` for wiring

#### Outputs
- `api/main.py` — FastAPI app factory, static directory mounting, lifespan management
- `api/routes.py` — JSON API routing layer connecting all subsystems

#### Issues Raised
- **Uvicorn boot failure** on first run (`AttributeError: module has no attribute 'app'`).

#### Orchestrator Response
- The API gateway was designed in Phase 1 but never explicitly assigned to any agent. This was identified as a critical gap during integration — the system could not start without this layer.
- An ad-hoc API Agent was provisioned immediately. The boot failure was debugged and resolved by correcting the import path and app instantiation pattern in `api/main.py`.

#### Handoff
`api/main.py`, `api/routes.py` passed to UI Agent.

---

### Agent 6 — UI Agent
**Role:** Frontend Engineer  
**Tool:** Cursor Composer (dedicated tab)  
**Phase:** Phase 6 — after API layer is operational

---

#### Responsibility

Implements the frontend presentation layer. Responsible for Jinja2 templates, Tailwind CSS styling, and Alpine.js reactivity. Does not touch backend Python files.

#### Prompt History

**Phase 6.1 — Initial Prompt:**

```
@product_prd.md @agents/ui_agent.md

You are the UI Agent. Follow the instructions in the
agent file and implement these files:
- templates/base.html
- templates/index.html
- templates/crawler.html
- templates/status.html
- templates/search.html
- static/css/style.css
- static/js/app.js

Design a flat minimal UI. Vanilla JS only.
Do not touch any Python files.
```

**Phase 6.2 — Orchestrator Pivot (full rewrite mandated):**

```
@product_prd.md @agents/ui_agent.md

Rejecting the flat UI output. Rewrite all frontend files
using Tailwind CSS (Dark Mode Glassmorphism) and Alpine.js
for reactivity via CDN — no build step.

Implement high-contrast text (bg-slate-950), a sticky
backdrop-blur navbar, and replace all Vanilla JS DOM
manipulation with Alpine.js component state.
```

#### Inputs
- `product_prd.md`
- `agents/ui_agent.md`
- `api/routes.py` for endpoint reference

#### Outputs
- `templates/base.html`, `templates/index.html`, `templates/crawler.html`, `templates/status.html`, `templates/search.html`
- `static/css/style.css` — Tailwind CSS, Dark Mode Glassmorphism palette
- `static/js/app.js` — Alpine.js reactive components, wrapped in `alpine:init`

#### Issues Raised
- **Vanilla JS DOM manipulation (900+ lines)** was brittle, hard to maintain, and tightly coupled to the HTML structure.
- **Text contrast failed WCAG standards** — light text on medium-dark backgrounds.
- **Framework pivot caused breakage:** switching from Vanilla JS to Alpine.js mid-project resulted in Alpine.js `not defined` errors at runtime, component registry failures, and CDN loading race conditions. These were not caught or self-corrected by the agent and required **direct manual intervention** by the orchestrator.

#### Orchestrator Response
- A major frontend framework pivot was authorized despite the mid-project timing.
- Vanilla JS was replaced with Alpine.js (via CDN) for clean declarative state management without introducing a build step.
- A strict Dark Mode color palette was enforced to meet accessibility requirements.
- All Alpine.js loading errors and component registry failures were manually debugged and fixed by the orchestrator directly: this included correcting script load order in `base.html`, ensuring the Alpine CDN `<script>` tag appeared before `app.js`, and fixing component registration syntax to use the `Alpine.data()` factory pattern inside `document.addEventListener('alpine:init', ...)`.

#### Handoff
All template and static files passed to QA & Test Agent for final review.

---

### Agent 7 — QA & Test Agent
**Role:** Quality Assurance & Test Engineer  
**Tool:** Claude.ai  
**Phase:** Phase 7 — final audit after all subsystems are integrated

---

#### Responsibility

Reviews the complete integrated codebase for bugs, race conditions, and integration failures. Fixes critical issues. Generates a comprehensive, runnable automated test suite under `tests/`. Does not introduce new features.

#### Prompt Format

```
@product_prd.md @agents/qa_agent.md

You are the QA & Test Agent. Review the complete codebase
for integration issues.

Fix:
- Alpine.js component registry failures in static/js/app.js
- Persistent state leaks on system reset

Then generate a comprehensive automated test suite in the
tests/ directory covering the API, Trie, and Storage layers
using pytest. All tests must be runnable with `pytest tests/`.
```

#### Inputs
- `product_prd.md`
- `agents/qa_agent.md`
- Complete codebase across all subsystems

#### Outputs
- Bug fixes applied directly to source files
- `tests/conftest.py` — shared pytest fixtures (app client, temporary storage, seeded trie state)
- `tests/test_api.py` — FastAPI endpoint tests
- `tests/test_core.py` — `core/normalize.py` and `core/parser.py` unit tests
- `tests/test_crawler.py` — crawler worker and queue unit tests
- `tests/test_storage.py` — `NoSQLStore` and `AtlasTrie` integration tests
- `tests/test_integration.py` — end-to-end tests covering the full crawl → index → search pipeline
- `pytest.ini` — pytest configuration (test discovery paths, markers)
- `requirements-dev.txt` — development dependencies (`pytest`, `httpx`, `pytest-asyncio`)

#### Issues Found & Resolved

| Issue | Severity | Fix Applied |
|---|---|---|
| Alpine.js Reference Errors | High | Reactive logic centralized into `Alpine.data()` factories inside `app.js`, wrapped in `document.addEventListener('alpine:init', ...)` to guarantee registration before Alpine initializes. |
| Ghost Writes on System Reset | Critical | `GLOBAL_RESET_EVENT` threading sentinel implemented. Workers check the event before any pending disk flush, aborting writes if a reset is in progress. Eliminates the race condition deterministically without `time.sleep` coupling. |
| Missing Automated Verification | Medium | QA Agent generated a full test suite across 6 files (35+ tests) covering API endpoints, core utilities, crawler behavior, storage, and an end-to-end integration pipeline. |

#### Important Note — Initial QA Agent Behavior and Redirect

In its first pass, the QA Agent did not write tests. Instead, it ran the code itself, verified it passed, and reported success — treating self-execution as a substitute for a test suite. This produced no reusable verification artifacts and would have left the project with zero automated coverage going forward.

The orchestrator identified this behavior and issued an explicit redirect: the agent was required to produce a `tests/` directory with runnable `pytest` tests — not to execute the code itself. The agent was reminded that "testing" in this context means writing tests that other developers and CI pipelines can run independently. Following this redirect, a full suite of 6 test files with 35+ automated tests was successfully produced, along with `conftest.py`, `pytest.ini`, and `requirements-dev.txt`.

This is a documented failure mode in QA agents: without an explicit instruction to *author* a test suite, agents will default to *running* code and reporting a pass — which looks like testing but produces nothing reusable.

#### Handoff
Final tested codebase and `tests/` directory handed off to orchestrator for git commit and deployment review.

---

## Key Orchestrator Decisions (Human-in-the-Loop)

### 1. Sequential Tab Isolation Over Parallel Execution

Agents were run one at a time in separate Cursor Composer tabs, each activated only after the previous agent's output was reviewed. This prevented agents from interfering with each other's files and gave the orchestrator a clear review checkpoint between every phase. The tradeoff was speed — the gain was full control over every handoff.

### 2. agents/ Directory as Agent Briefing System

Each agent received its role definition, responsibility scope, inputs, outputs, and file boundaries through a dedicated `.md` file in the `agents/` directory. Referencing these via `@agents/<n>_agent.md` in every prompt ensured agents stayed within their assigned scope and had consistent, repeatable context regardless of which Cursor tab they ran in.

### 3. Missing Subsystem Identification

The API gateway was specified in the architecture but never assigned to an agent in the original plan. The system failed to start during integration because of this gap. The orchestrator identified the missing layer immediately, wrote an ad-hoc API Agent briefing, and provisioned a new Cursor tab to resolve the issue. This reinforced the need for an explicit file-to-agent assignment checklist before starting the sequence.

### 4. UI Framework Pivot and Manual Intervention

The initial Vanilla JS UI was rejected on maintainability and accessibility grounds. A full rewrite using Tailwind CSS and Alpine.js was mandated. However, the framework switch caused Alpine.js to be undefined at runtime due to CDN load order issues and incorrect component registration — neither of which the agent self-corrected. The orchestrator manually intervened: fixing script ordering in `base.html`, ensuring Alpine loaded before `app.js`, and correcting the `Alpine.data()` registration pattern. This is a documented case where an agent-produced output required hands-on human debugging to become functional.

### 5. QA Agent Redirect — From Self-Execution to Test Authorship

The QA Agent's default behavior was to run the code and verify it worked rather than write reusable tests. This was caught and corrected with an explicit redirect. The distinction — between "I ran this and it passed" and "here is a test suite anyone can run" — is a non-obvious failure mode in QA agents that requires active orchestrator attention.

### 6. Architectural Refinement — Global Reset Sentinel

A proposed "Nuclear Reset" strategy using arbitrary `time.sleep` delays was challenged for violating layering principles: it coupled the in-memory Trie flush timing to the file system purge timing in an unpredictable way. The orchestrator accepted a more robust counter-proposal: a `threading.Event`-based Global Reset Sentinel. Workers check the event before any disk flush and abort if a reset is in progress. This fixed the ghost-write race condition deterministically without coupling subsystems.

### 7. Cascading Data Integrity

Deleting a crawl job now triggers a recursive cleanup in the Prefix Trie, purging all word/URL entries associated with that job's origin. This ensures the search index never returns stale results from deleted datasets. Enforced at the Indexer Agent phase.

### 8. Resource-Aware Crawling

The initial queue logic dropped packets silently when the queue was full. This was replaced with a blocking wait (`while self.queue.full(): time.sleep(1)`) that pauses the crawler until the indexer has capacity — preventing silent data loss and significantly improving CPU efficiency under heavy network load.

---

## Lessons Learned

| Area | Lesson |
|---|---|
| Prompt Format | A consistent two-part format (`@prd.md @agents/<n>.md` + explicit file list + negative scope boundary) kept every agent focused and prevented file conflicts between tabs. |
| Sequential Execution | Running agents one tab at a time — rather than in parallel — gave the orchestrator meaningful review checkpoints and prevented codebase conflicts at each handoff. |
| Agent Scope | Every agent needs an explicit list of files to implement **and** an explicit list of files to leave untouched. Without the negative constraint, agents modify files outside their intended scope. |
| Integration Gaps | Agents only see their own subsystem. The orchestrator must maintain a global view and verify that every designed component has been explicitly assigned before starting the sequence. |
| Framework Pivots | Switching frameworks mid-project (Vanilla JS → Alpine.js) causes breakage that agents do not self-detect or self-repair. Budget manual debugging time for any mid-sequence technology decision change. |
| QA Agent Behavior | QA agents will default to self-execution over test authorship unless explicitly instructed otherwise. Always specify that "testing" means producing a runnable test suite, not running the code yourself. |
| Sentinel over Sleep | `threading.Event`-based sentinels are deterministic and subsystem-decoupled. `time.sleep` delays are guesses that mask race conditions. Use sentinels for any shared-state synchronization. |
| Human Oversight | Multi-agent workflows do not self-correct integration failures. Every phase boundary requires a human review step. The orchestrator is not optional — they are the system's integration layer. |

---

## Final Project Structure

```
atlas-search-v2/
├── agents/                          # Agent briefing files (used as @-references in prompts)
│   ├── api_agent.md
│   ├── architect_agent.md
│   ├── crawler_agent.md
│   ├── indexer_agent.md
│   ├── qa_agent.md
│   ├── search_agent.md
│   └── ui_agent.md
├── api/
│   ├── __init__.py
│   ├── main.py                      # FastAPI app factory, static mounting
│   └── routes.py                    # JSON API routing layer
├── core/
│   ├── __init__.py
│   ├── config.py                    # Centralized configuration
│   ├── normalize.py                 # Turkish locale fold, shared text normalization (Search Agent)
│   ├── parser.py                    # HTML parsing and link extraction
│   └── security.py                  # validate_url() — SSRF protection
├── crawler/
│   ├── __init__.py
│   ├── worker.py                    # CrawlerWorker (threading.Thread, BFS)
│   └── queue.py                     # Bounded queue with blocking backpressure
├── search/
│   ├── __init__.py
│   ├── engine.py                    # Query pipeline, delegates normalization to core/normalize.py
│   └── ranking.py                   # score = (freq×10) + 1000 − (depth×5)
├── storage/
│   ├── __init__.py
│   ├── trie.py                      # AtlasTrie (RLock, cascading delete)
│   ├── nosql.py                     # NoSQLStore (atomic JSON write)
│   └── exporter.py                  # ETLExporter
├── static/
│   ├── css/                         # style.css — Tailwind CSS, Dark Mode Glassmorphism
│   └── js/                          # app.js — Alpine.js components (alpine:init)
├── templates/
│   ├── base.html
│   ├── crawler.html
│   ├── index.html
│   ├── search.html
│   └── status.html
├── tests/
│   ├── __init__.py
│   ├── conftest.py                  # Shared pytest fixtures (app client, temp storage, seeded trie)
│   ├── test_api.py                  # FastAPI endpoint tests
│   ├── test_core.py                 # core/normalize.py and core/parser.py unit tests
│   ├── test_crawler.py              # Crawler worker and queue unit tests
│   ├── test_integration.py          # End-to-end: crawl → index → search pipeline
│   └── test_storage.py              # NoSQLStore and AtlasTrie integration tests
├── data/storage/                    # Persistent storage directory
├── atlas_store.json                 # Live JSON data store (runtime artifact)
├── multi_agent_workflow.md          # This document
├── product_prd.md                   # Global project requirements
├── pytest.ini                       # pytest configuration and test discovery
├── readme.md
├── recommendation.md
├── requirements.txt                 # fastapi, uvicorn, pydantic, jinja2
└── requirements-dev.txt             # pytest, httpx, pytest-asyncio
```

---

*This document is a complete record of the Atlas Search multi-agent development process — covering the orchestration method, agent briefing format, sequential execution model, all human intervention points, and the architectural decisions made at each phase. All final decisions were made by the human developer acting as Lead Engineer.*

# API Agent

**Role:** Backend API Gateway Developer  
**Tool:** Cursor Composer  
**Phase:** Phase 6(Integration Phase)

---

## Responsibility

Owns the FastAPI application instance and the routing layer. Bridges the backend subsystems (Crawler, Storage, Search) with the frontend UI by defining the exact REST endpoints and rendering Jinja2 templates. Does not write core crawler logic or search algorithms.

## Prompt

> "You are the API Agent. We need to build the API gateway layer to connect our backend engines to the frontend.
> 
> Please write `api/main.py` and `api/routes.py` based on the API Contracts in the PRD.
> 
> Requirements for `api/main.py`:
> - Instantiate `app = FastAPI(title="Atlas Search")`
> - Mount the `/static` directory
> - Include the router from `api/routes.py`
> - Add a lifespan context manager for startup/shutdown events (like trie hydration).
> 
> Requirements for `api/routes.py`:
> - Create an `APIRouter()`
> - Add the 4 HTML page routes (`/`, `/crawler`, `/status`, `/search`) that return `Jinja2Templates` responses.
> - Add the JSON API routes (`/api/crawler/create`, `/api/metrics`, `/api/search`, etc.) and connect them to the NoSQLStore, AtlasTrie, and CrawlerWorker logic."

## Inputs

- `@product_prd.md` (for endpoint schemas)
- `@crawler/worker.py` (for starting jobs)
- `@search/engine.py` (for querying)
- `@templates/` (for Jinja2 rendering)

## Outputs

- `api/main.py` — FastAPI application factory
- `api/routes.py` — REST API and HTML route definitions

## Issues Raised

- Uvicorn failed to start initially because the `app` instance was completely missing from the project structure. 

## Orchestrator Response

Discovered the gap during integration testing. Spun up this dedicated API Agent to scaffold the missing gateway layer and successfully started the server.
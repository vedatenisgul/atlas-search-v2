"""
App factory and lifespan hooks.

PRD refs: §3 System Architecture, §7 Persistence (ETL import on startup,
JSON flush daemon), §8 API Summary.

Responsibilities:
    - Build the FastAPI application.
    - Mount ``/static`` for the vanilla-JS frontend.
    - Include the route module (HTML pages + JSON REST endpoints).
    - Lifespan: hydrate the AtlasTrie from ``data/storage/*.data`` at boot,
      ensure the NoSQLStore singleton is constructed (its JSON-flush daemon
      starts automatically on first use), and shut both down cleanly.

Owner agent: UI Agent (app wiring) + Search/Crawler agents (lifespan hooks).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.routes import router, shutdown_all_workers
from storage.exporter import (
    export_all_to_legacy_format,
    import_legacy_data_to_trie,
)
from storage.nosql import NoSQLStore
from storage.trie import AtlasTrie


logger = logging.getLogger("atlas.api")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STATIC_DIR = str(_PROJECT_ROOT / "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: ETL import + singleton bootstrap.

    Shutdown: stop every running worker, export the Trie back to flat-files,
    and close the NoSQLStore sync daemon with a final JSON flush.
    """
    try:
        # Touch singletons so their locks/daemons exist before the first
        # request arrives. ``NoSQLStore.__init__`` hydrates from
        # ``atlas_store.json`` and spawns the sync daemon.
        NoSQLStore.get_instance()
        AtlasTrie.get_instance()
    except Exception as exc:
        logger.warning("singleton bootstrap failed: %r", exc)

    try:
        counts = import_legacy_data_to_trie()
        if counts:
            logger.info("trie ETL import restored shards=%s", counts)
    except Exception as exc:
        logger.warning("trie ETL import failed: %r", exc)

    try:
        yield
    finally:
        try:
            shutdown_all_workers(timeout_per_worker=2.0)
        except Exception as exc:
            logger.warning("worker shutdown failed: %r", exc)

        try:
            export_all_to_legacy_format()
        except Exception as exc:
            logger.warning("trie ETL export on shutdown failed: %r", exc)

        try:
            NoSQLStore.get_instance().shutdown(save=True)
        except Exception as exc:
            logger.warning("store shutdown failed: %r", exc)


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    application = FastAPI(title="Atlas Search", lifespan=lifespan)

    if os.path.isdir(_STATIC_DIR):
        application.mount(
            "/static",
            StaticFiles(directory=_STATIC_DIR),
            name="static",
        )
    else:
        logger.error(
            "static directory not found at %s — /static/* will 404 and the "
            "Alpine.js frontend will fail to boot (x-data components won't "
            "register). Verify the project layout.",
            _STATIC_DIR,
        )

    application.include_router(router)
    return application


app = create_app()


__all__ = ["app", "create_app", "lifespan"]

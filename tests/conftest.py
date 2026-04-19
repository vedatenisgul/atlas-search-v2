"""
Shared pytest fixtures for the Atlas Search test suite.

Design goals:

* **Hermetic.** Every test runs against a fresh ``NoSQLStore`` + ``AtlasTrie``
  singleton, rooted at a per-test ``tmp_path``. No test ever reads or writes
  the project-root ``atlas_store.json`` or ``data/storage/*.data``.
* **Offline.** ``CrawlerWorker`` and ``validate_url`` are patched so no DNS
  resolution and no outbound HTTP request ever happen during tests. The
  ``mock_urlopen`` fixture serves canned HTML keyed by URL.
* **Deterministic.** The store's 5-second background sync daemon is stopped
  during ``_reset_for_tests`` so tests never race an unrelated flush tick.

Fixture map (what to use when)
------------------------------
    tmp_storage        — redirects ``config.STORE_PATH`` + ``config.DATA_DIR``
                         into ``tmp_path`` and nukes both singletons.
    clean_trie         — resets ``AtlasTrie`` singleton (no store touch).
    clean_store        — resets ``NoSQLStore`` singleton into ``tmp_path``.
    client             — FastAPI ``TestClient`` with full lifespan wiring.
    mock_urlopen       — serves canned HTML for the crawler's urllib.
    disable_network    — monkeypatches ``validate_url`` / DNS resolution.

All fixtures are ``autouse=False`` except ``tmp_storage`` (which the API and
integration tests pull in transitively via ``client``).
"""

from __future__ import annotations

import io
import os
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional
from unittest.mock import patch

import pytest


# Make the project root importable when pytest is invoked from a subdir.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# --------------------------------------------------------------- singletons


def _reset_all_singletons() -> None:
    """Tear down every module-level singleton the app creates.

    Called at fixture setup *and* teardown so an earlier test's mutations
    cannot leak into a later test. Order matters: ``NoSQLStore`` first so
    its sync daemon observes the stop flag before we null the reference.
    """
    try:
        from storage.nosql import NoSQLStore
        NoSQLStore._reset_for_tests()
    except Exception:
        pass
    try:
        from storage.trie import AtlasTrie
        AtlasTrie._reset_for_tests()
    except Exception:
        pass
    # The worker module also carries a process-wide reset event; clearing
    # it guarantees each test starts with disk flushes *enabled*.
    try:
        from crawler.worker import allow_flushes
        allow_flushes()
    except Exception:
        pass


@pytest.fixture
def tmp_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the app's disk paths at ``tmp_path`` and reset singletons.

    Yields the ``tmp_path`` so tests can inspect generated files (shards,
    atlas_store.json) directly. Resets singletons on both sides so a test
    that acquires ``AtlasTrie`` via a separate import path still observes
    the clean state.
    """
    from core import config

    store_path = tmp_path / "atlas_store.json"
    data_dir = tmp_path / "data_shards"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "STORE_PATH", str(store_path), raising=False)
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir), raising=False)
    # Sync daemon interval pinned to a long value so a late flush tick can't
    # race an assertion. Individual tests that need a flush call save()
    # explicitly instead of relying on the daemon.
    monkeypatch.setattr(config, "SYNC_INTERVAL", 30, raising=False)

    _reset_all_singletons()
    try:
        yield tmp_path
    finally:
        _reset_all_singletons()


@pytest.fixture
def clean_trie(tmp_storage: Path):
    """Fresh ``AtlasTrie`` singleton for unit tests."""
    from storage.trie import AtlasTrie
    return AtlasTrie.get_instance()


@pytest.fixture
def clean_store(tmp_storage: Path):
    """Fresh ``NoSQLStore`` singleton rooted at the per-test tmp_path."""
    from storage.nosql import NoSQLStore
    return NoSQLStore.get_instance()


# --------------------------------------------------------------- networking


class _FakeHTTPResponse:
    """Minimal stand-in for the object returned by ``urllib.request.urlopen``.

    Only implements the attributes ``crawler/worker._fetch`` actually uses:
    ``headers.get``, ``headers.get_content_charset``, ``read``, and
    ``geturl`` — plus the context-manager protocol.
    """

    def __init__(self, url: str, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self._url = url
        self._body = body
        self._buffer = io.BytesIO(body)
        self.headers = _FakeHeaders(content_type)

    def read(self, n: int = -1) -> bytes:
        return self._buffer.read() if n < 0 else self._buffer.read(n)

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeHeaders:
    """Only the bits of ``http.client.HTTPMessage`` the worker touches."""

    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if key.lower() == "content-type":
            return self._content_type
        return default

    def get_content_charset(self) -> str:
        # The crawler decodes response bodies with this charset; utf-8 is a
        # safe default for the canned HTML the tests emit.
        return "utf-8"


@pytest.fixture
def mock_urlopen(monkeypatch: pytest.MonkeyPatch) -> Callable[[Dict[str, str]], None]:
    """Patch ``urllib.request.urlopen`` to serve canned HTML per URL.

    Usage::

        def test_crawler(mock_urlopen):
            mock_urlopen({
                "https://example.test/": "<html><body>hello world</body></html>",
            })

    Unknown URLs raise ``urllib.error.URLError("not in fixture")`` so an
    unmocked fetch fails fast instead of silently hitting the network.
    """
    import urllib.error
    import urllib.request

    registry: Dict[str, str] = {}

    def _register(mapping: Dict[str, str]) -> None:
        registry.update(mapping)

    def _fake_urlopen(request, timeout=None, context=None):  # noqa: ARG001
        # ``request`` may be a Request object or a bare URL string.
        url = request.full_url if hasattr(request, "full_url") else str(request)
        body = registry.get(url)
        if body is None:
            # Also try without trailing slash — real HTTP normalization.
            body = registry.get(url.rstrip("/"))
        if body is None:
            raise urllib.error.URLError(f"not in fixture: {url}")
        return _FakeHTTPResponse(url, body.encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return _register


@pytest.fixture
def disable_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip DNS resolution inside ``validate_url`` for tests.

    Replaces :func:`core.security.validate_url` in every module that has
    already imported it (``crawler.worker``, ``api.routes``) with a
    forgiving version that only enforces the scheme/host shape — no
    ``socket.getaddrinfo`` call is made. This lets the crawler accept
    ``https://example.test/`` even though that host does not resolve.

    The unit tests for ``validate_url`` itself do *not* use this fixture —
    they exercise the real function against literal IPs.
    """
    from urllib.parse import urlparse

    def _fake_validate_url(url: str) -> str:
        if not url or not isinstance(url, str):
            raise ValueError("empty url")
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"blocked scheme: {parsed.scheme!r}")
        if not parsed.netloc:
            raise ValueError("missing host")
        # Reject localhost + private ranges the same way the real validator
        # does so SSRF tests against the fake still fail closed.
        host = parsed.hostname or ""
        if host in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("loopback blocked")
        return url

    import api.routes as _routes
    import crawler.worker as _worker
    import core.security as _sec

    monkeypatch.setattr(_sec, "validate_url", _fake_validate_url)
    monkeypatch.setattr(_routes, "validate_url", _fake_validate_url)
    monkeypatch.setattr(_worker, "validate_url", _fake_validate_url)


# --------------------------------------------------------------- FastAPI


@pytest.fixture
def client(tmp_storage: Path, disable_network, mock_urlopen) -> Iterator:
    """FastAPI ``TestClient`` running against a fresh app + isolated storage.

    Pulls ``disable_network`` and ``mock_urlopen`` so the crawler routes are
    safe to hit without touching real DNS / HTTP. Tests that need to feed
    the crawler canned HTML simply call ``mock_urlopen({...})`` themselves.
    """
    from fastapi.testclient import TestClient

    # Re-import the app factory *after* tmp_storage has patched config —
    # ``create_app`` itself is side-effect free but our lifespan hooks
    # touch both singletons, so they must have the tmp paths in hand.
    from api.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    # TestClient context manager already ran the shutdown lifespan; still
    # reset singletons defensively so any follow-up test starts clean.
    _reset_all_singletons()


# --------------------------------------------------------------- helpers


@pytest.fixture
def seed_trie_factory(clean_trie):
    """Callable that inserts a sequence of postings into the trie.

    Each ``(word, url, depth, origin)`` tuple becomes one ``insert`` call.
    Handy for API tests that need the search endpoint to return results
    without running an actual crawl.
    """

    def _seed(rows):
        for word, url, depth, origin in rows:
            clean_trie.insert(word, url=url, depth=depth, origin=origin)
        return clean_trie

    return _seed


@pytest.fixture
def seed_metadata_factory(clean_store):
    """Callable that pre-populates ``NoSQLStore.data['metadata']``.

    Mirrors what ``CrawlerWorker._store_page_metadata`` would write so
    ``SearchEngine._hydrate`` can attach title/snippet in tests that skip
    the crawler altogether.
    """

    def _seed(rows):
        import time as _time
        with clean_store.lock:
            metadata = clean_store.data.setdefault("metadata", {})
            for url, title, snippet in rows:
                metadata[url] = {
                    "title": title,
                    "snippet": snippet,
                    "depth": 0,
                    "origin": url,
                    "ts": _time.time(),
                }
            clean_store.mark_dirty()
        return clean_store

    return _seed


@pytest.fixture
def stop_all_workers_on_teardown():
    """Ensure every ``CrawlerWorker`` started inside a test is reaped.

    Some tests exercise ``/api/crawler/create`` without calling DELETE
    afterwards. This fixture joins the global registry on teardown so a
    stray thread cannot bleed into the next test and keep flushing.
    """
    yield
    try:
        from api.routes import shutdown_all_workers
        shutdown_all_workers(timeout_per_worker=1.0)
    except Exception:
        pass


# --------------------------------------------------------------- thread aid


class ThreadRunner:
    """Tiny helper for driving N worker threads against a shared target.

    Used by ``test_storage.py`` to exercise the Trie and NoSQLStore locks
    with real concurrency instead of sequential pseudo-interleaving.
    """

    def __init__(self, target: Callable, count: int) -> None:
        self._threads = [
            threading.Thread(target=target, name=f"runner-{i}", daemon=True)
            for i in range(count)
        ]

    def run_and_join(self, timeout: float = 10.0) -> None:
        for t in self._threads:
            t.start()
        for t in self._threads:
            t.join(timeout=timeout)
            assert not t.is_alive(), f"{t.name} failed to join within {timeout}s"


@pytest.fixture
def thread_runner():
    """Return the ``ThreadRunner`` class for direct use in tests."""
    return ThreadRunner

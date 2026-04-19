"""
tests/test_api.py — FastAPI integration tests via ``TestClient``.

Covers the two endpoints the QA spec calls out explicitly:

* ``GET  /api/search``
* ``POST /api/crawler/create``

…plus the ATLAS_CONFIG injection path the UI depends on (``window.ATLAS_CONFIG``
served by every SSR page), and a handful of negative cases that exercise the
validation layer without touching real network.

Every test routes through the ``client`` fixture (see ``conftest.py``), which
stands up a fresh app with tmp-path storage, resets singletons, patches DNS,
and mocks ``urllib.request.urlopen``.
"""

from __future__ import annotations

import json
import time

import pytest


# =============================================================== /api/search


class TestApiSearch:
    def test_empty_query_returns_empty_result_set(self, client):
        resp = client.get("/api/search", params={"q": ""})
        assert resp.status_code == 200
        body = resp.json()
        assert body["query"] == ""
        assert body["results"] == []
        assert body["total"] == 0

    def test_search_returns_seeded_hit(self, client, seed_trie_factory, seed_metadata_factory):
        """Seed the trie + metadata directly so we test the query pipeline
        without running a crawl."""
        seed_trie_factory([
            ("python", "https://docs.test/", 0, "https://docs.test/"),
            ("python", "https://blog.test/", 2, "https://blog.test/"),
        ])
        seed_metadata_factory([
            ("https://docs.test/", "Python Docs", "Official Python documentation"),
            ("https://blog.test/", "Python Blog", "A post about Python tooling"),
        ])

        resp = client.get("/api/search", params={"q": "python", "limit": 10})
        assert resp.status_code == 200
        body = resp.json()
        assert body["query"] == "python"
        assert body["total"] == 2
        assert len(body["results"]) == 2
        # elapsed_ms must be surfaced for the L1 frontend fix.
        assert "elapsed_ms" in body and body["elapsed_ms"] >= 0

        urls = [r["url"] for r in body["results"]]
        # depth=0 ranks above depth=2 per the PRD formula.
        assert urls[0] == "https://docs.test/"

        top = body["results"][0]
        assert top["title"] == "Python Docs"
        assert "Python" in top["snippet"]
        assert top["relevance_score"] >= top["depth"] * -5  # sanity

    def test_search_limit_and_offset_paginate(self, client, seed_trie_factory):
        # Insert 5 URLs sharing the same token.
        seed_trie_factory([
            (f"token", f"https://a/{i}", i, "https://a/")
            for i in range(5)
        ])
        first_page = client.get("/api/search", params={"q": "token", "limit": 2, "offset": 0}).json()
        second_page = client.get("/api/search", params={"q": "token", "limit": 2, "offset": 2}).json()

        assert first_page["total"] == 5
        assert len(first_page["results"]) == 2
        assert len(second_page["results"]) == 2
        # No overlap between the two pages.
        assert set(r["url"] for r in first_page["results"]).isdisjoint(
            r["url"] for r in second_page["results"]
        )

    def test_search_miss_returns_empty_results(self, client, seed_trie_factory):
        seed_trie_factory([("hello", "https://a/", 0, "https://a/")])
        resp = client.get("/api/search", params={"q": "neverindexed"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["results"] == []

    def test_invalid_limit_rejected_by_query_validation(self, client):
        # ``limit`` is bounded 0..100 by Pydantic Query().
        resp = client.get("/api/search", params={"q": "hello", "limit": 999})
        assert resp.status_code == 422


# ======================================================== /api/crawler/create


class TestApiCrawlerCreate:
    def test_create_rejects_missing_seed(self, client):
        resp = client.post("/api/crawler/create", json={})
        assert resp.status_code == 422  # Pydantic field validation

    def test_create_rejects_blocked_scheme(self, client):
        resp = client.post(
            "/api/crawler/create",
            json={"seed_url": "ftp://example.test/"},
        )
        assert resp.status_code == 400
        assert "invalid seed_url" in resp.json()["detail"]

    def test_create_rejects_loopback_seed(self, client):
        resp = client.post(
            "/api/crawler/create",
            json={"seed_url": "http://127.0.0.1/"},
        )
        assert resp.status_code == 400

    def test_create_succeeds_with_valid_seed_and_stops_cleanly(
        self, client, mock_urlopen, stop_all_workers_on_teardown
    ):
        """Happy path: POST /create -> 200, worker fetches mocked seed,
        GET /status returns the job, DELETE reaps it."""
        seed = "https://atlas.test/"
        mock_urlopen({
            seed: "<html><head><title>Home</title></head><body>hello atlas</body></html>",
        })

        resp = client.post(
            "/api/crawler/create",
            json={
                "seed_url": seed,
                "max_depth": 0,        # don't follow children
                "hit_rate": 50.0,      # negligible per-page sleep
                "max_capacity": 10,
                "max_urls": 1,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running"
        assert body["seed_url"] == seed
        job_id = body["job_id"]
        assert job_id and isinstance(job_id, str)

        # Let the worker finish the single page (max_urls=1).
        deadline = time.time() + 5.0
        while time.time() < deadline:
            status = client.get(f"/api/crawler/status/{job_id}")
            if status.status_code == 200 and status.json().get("crawled", 0) >= 1:
                break
            time.sleep(0.05)
        else:
            pytest.fail("worker did not crawl the seed within 5s")

        # The /api/search endpoint now hits the indexed token.
        search = client.get("/api/search", params={"q": "atlas"})
        assert search.status_code == 200
        urls = [r["url"] for r in search.json()["results"]]
        assert seed in urls

        # Clean up the worker explicitly — the DELETE path exercises the
        # cascade purge on top of reaping the thread.
        delete = client.delete(f"/api/crawler/delete/{job_id}")
        assert delete.status_code == 200
        assert delete.json()["deleted"] is True

    def test_create_applies_config_defaults_when_fields_omitted(
        self, client, mock_urlopen, stop_all_workers_on_teardown
    ):
        """Missing knobs fall through to ``core.config`` defaults.

        Regression for M2 — ``DEFAULT_MAX_DEPTH`` is the single source of
        truth for both frontend and backend defaults.
        """
        from core import config

        seed = "https://defaults.test/"
        mock_urlopen({seed: "<html><body>hi</body></html>"})

        resp = client.post("/api/crawler/create", json={"seed_url": seed})
        assert resp.status_code == 200
        body = resp.json()
        assert body["max_depth"] == config.DEFAULT_MAX_DEPTH
        assert body["hit_rate"] == pytest.approx(config.DEFAULT_HIT_RATE)
        assert body["max_capacity"] == config.DEFAULT_MAX_CAPACITY
        assert body["max_urls"] == config.DEFAULT_MAX_URLS

        # Stop the worker immediately — we don't want it crawling the whole
        # (fake) universe, and the seed already indexed one token.
        client.delete(f"/api/crawler/delete/{body['job_id']}")


# ============================================================== UI wiring


class TestSSRConfigInjection:
    """H3 regression — every SSR page emits ``window.ATLAS_CONFIG``."""

    @pytest.mark.parametrize("path", ["/", "/crawler", "/status", "/search"])
    def test_page_renders_with_atlas_config(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 200
        body = resp.text
        assert "window.ATLAS_CONFIG" in body
        # The inlined JSON must be valid and include the keys app.js reads.
        head, _, tail = body.partition("window.ATLAS_CONFIG = ")
        assert tail, f"no ATLAS_CONFIG assignment on {path}"
        # The inline value is JSON terminated by ``;</script>``.
        payload_end = tail.find(";")
        assert payload_end != -1
        payload = json.loads(tail[:payload_end])
        assert "POLL_INTERVAL_MS" in payload
        assert "DEFAULT_MAX_DEPTH" in payload


# =============================================================== /api/metrics


class TestApiMetrics:
    def test_metrics_returns_expected_shape_with_no_jobs(self, client):
        resp = client.get("/api/metrics")
        assert resp.status_code == 200
        body = resp.json()
        for key in (
            "total_crawled", "total_errors", "pending_total",
            "active_jobs", "paused_jobs", "total_jobs",
            "trie_words", "trie_nodes", "indexed_pages", "visited_urls",
        ):
            assert key in body, f"missing metrics key: {key}"
        # Fresh app — everything should be zero.
        assert body["total_jobs"] == 0
        assert body["trie_words"] == 0

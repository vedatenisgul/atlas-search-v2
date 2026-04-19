"""
tests/test_integration.py — end-to-end crawl → search flow.

Exercises the full product loop with DNS + urlopen mocked:

    1. POST /api/crawler/create with a seed URL backed by canned HTML.
    2. Wait for the worker to fetch the seed + one child link.
    3. GET /api/search to confirm the indexed tokens are retrievable.
    4. POST /api/system/reset and confirm the shards + store file are gone,
       the trie is empty, and /api/search returns no hits again.

This is the single test that verifies every component (routes -> worker ->
trie -> store -> search engine) cooperating through their real interfaces.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


def _wait_for(predicate, timeout=5.0, interval=0.05):
    """Poll ``predicate()`` until it returns truthy or ``timeout`` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestEndToEndCrawlAndSearch:
    def test_crawl_then_search_then_reset(
        self, client, mock_urlopen, tmp_storage, stop_all_workers_on_teardown
    ):
        seed = "https://atlas.test/"
        child = "https://atlas.test/docs"
        mock_urlopen({
            seed: (
                "<html><head><title>Atlas Home</title></head><body>"
                "<p>Welcome to the Atlas search demo.</p>"
                f"<a href='{child}'>docs</a>"
                "</body></html>"
            ),
            child: (
                "<html><head><title>Atlas Docs</title></head><body>"
                "<p>Read the Atlas documentation for search tips.</p>"
                "</body></html>"
            ),
        })

        # 1. Start a crawl.
        create = client.post(
            "/api/crawler/create",
            json={
                "seed_url": seed,
                "max_depth": 1,
                "hit_rate": 50.0,
                "max_capacity": 10,
                "max_urls": 2,
            },
        )
        assert create.status_code == 200
        job_id = create.json()["job_id"]

        # 2. Wait for both pages to be indexed.
        def two_pages_crawled():
            resp = client.get(f"/api/crawler/status/{job_id}")
            return (
                resp.status_code == 200
                and resp.json().get("crawled", 0) >= 2
            )

        assert _wait_for(two_pages_crawled, timeout=10.0), (
            "worker did not crawl seed + child within 10s"
        )

        # 3. Search for a token unique to the child page.
        search = client.get("/api/search", params={"q": "documentation"})
        assert search.status_code == 200
        body = search.json()
        assert body["total"] >= 1
        urls = {r["url"] for r in body["results"]}
        assert child in urls

        # A token present in both pages should come back with both URLs.
        both = client.get("/api/search", params={"q": "atlas"}).json()
        assert {r["url"] for r in both["results"]} >= {seed, child}

        # Titles + snippets must be hydrated from NoSQLStore metadata.
        top = next(r for r in body["results"] if r["url"] == child)
        assert top["title"] == "Atlas Docs"
        assert "documentation" in top["snippet"].lower()

        # 4. Reset wipes everything.
        reset = client.post("/api/system/reset")
        assert reset.status_code == 200
        assert reset.json()["reset"] is True

        # Post-reset the trie is empty — search returns nothing.
        after = client.get("/api/search", params={"q": "atlas"}).json()
        assert after["total"] == 0
        assert after["results"] == []

        # The tmp atlas_store.json must be gone. The shard directory may
        # still exist but every *.data shard should have been purged.
        store_path = Path(tmp_storage) / "atlas_store.json"
        assert not store_path.exists(), "reset did not delete atlas_store.json"
        shard_dir = Path(tmp_storage) / "data_shards"
        leftover = list(shard_dir.glob("*.data")) if shard_dir.exists() else []
        assert not leftover, f"reset left shards behind: {leftover}"


class TestDeleteJobCascade:
    """Deleting a job must purge its postings from /api/search."""

    def test_delete_job_removes_its_hits_from_search(
        self, client, mock_urlopen, stop_all_workers_on_teardown
    ):
        seed = "https://deleteme.test/"
        mock_urlopen({
            seed: "<html><body>unique-token-deleteme</body></html>",
        })

        create = client.post(
            "/api/crawler/create",
            json={
                "seed_url": seed,
                "max_depth": 0,
                "hit_rate": 50.0,
                "max_urls": 1,
            },
        )
        job_id = create.json()["job_id"]

        # Wait for the one page to be indexed.
        def indexed():
            hits = client.get(
                "/api/search", params={"q": "unique-token-deleteme"}
            ).json()
            return hits["total"] >= 1

        assert _wait_for(indexed, timeout=5.0)

        # Delete the job; cascade purge drops the posting.
        delete = client.delete(f"/api/crawler/delete/{job_id}")
        assert delete.status_code == 200
        cascade = delete.json()["cascade"]
        # Either the trie or the metadata cascade must report real work done.
        assert cascade.get("trie_postings_removed", 0) >= 1 or \
            cascade.get("metadata_removed", 0) >= 1

        # Search no longer returns the dead URL.
        post = client.get("/api/search", params={"q": "unique-token-deleteme"}).json()
        assert post["total"] == 0

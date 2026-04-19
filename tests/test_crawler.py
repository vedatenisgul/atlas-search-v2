"""
tests/test_crawler.py — Crawler unit tests.

Covers:

* ``core.security.normalize_url`` + ``sha256_url`` edge cases.
* ``CrawlerQueue`` FIFO, per-job dedup, ring-buffer eviction, status thresholds.
* ``CrawlerWorker._enqueue_children`` (M4 regression guard — no bare ``except``).
* End-to-end BFS against a mocked ``urllib.request.urlopen`` (two canned pages
  with an outbound link; verify the worker fetches the seed, indexes tokens,
  and follows the child link at depth+1).

No tests here touch real DNS or HTTP — ``disable_network`` + ``mock_urlopen``
from ``conftest.py`` keep the suite hermetic.
"""

from __future__ import annotations

import time

import pytest

from core.security import (
    normalize_url,
    sanitize_html_input,
    sha256_url,
    validate_url,
)
from crawler.queue import CrawlerQueue


# ================================================================= URL utils


class TestNormalizeURL:
    def test_normalize_lowercases_scheme_and_host(self):
        assert normalize_url("HTTPS://Example.COM/Path").startswith("https://example.com")

    def test_normalize_strips_fragment(self):
        assert normalize_url("https://example.com/#section") == "https://example.com/"

    def test_normalize_keeps_query_string(self):
        assert "?foo=bar" in normalize_url("https://example.com/?foo=bar")

    @pytest.mark.parametrize("bad_input", [None, "", "   ", 12345, object()])
    def test_normalize_raises_value_error_on_non_string(self, bad_input):
        """Regression guard for the M4 fix — caller's ``except ValueError``
        must catch every malformed input without a bare ``except``."""
        with pytest.raises(ValueError):
            normalize_url(bad_input)  # type: ignore[arg-type]


class TestSha256URL:
    def test_same_url_produces_same_hash(self):
        a = sha256_url("https://example.com/")
        b = sha256_url("https://example.com/")
        assert a == b and len(a) == 64  # 256-bit hex digest

    def test_different_urls_differ(self):
        assert sha256_url("https://a/") != sha256_url("https://b/")


class TestSanitize:
    def test_sanitize_html_strips_tags(self):
        out = sanitize_html_input("<script>alert(1)</script>hello")
        assert "<" not in out and "script" not in out.lower()


class TestValidateURL:
    """Black-box checks against the real SSRF validator.

    These tests intentionally do *not* use ``disable_network`` so they
    exercise the real scheme/host filters. Loopback + private IPs are
    checked pre-DNS so no socket.getaddrinfo() call fires.
    """

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",  # AWS metadata service
    ])
    def test_rejects_private_and_loopback(self, url):
        with pytest.raises(ValueError):
            validate_url(url)

    @pytest.mark.parametrize("url", [
        "ftp://example.com/",
        "javascript:alert(1)",
        "file:///etc/passwd",
    ])
    def test_rejects_non_http_schemes(self, url):
        with pytest.raises(ValueError):
            validate_url(url)


# ================================================================== Queue


class TestCrawlerQueue:
    def test_push_and_pop_preserve_fifo(self):
        q = CrawlerQueue(job_id="t", max_capacity=10)
        q.push("https://a/", 0)
        q.push("https://b/", 1)
        q.push("https://c/", 2)
        assert q.pop()["url"] == "https://a/"
        assert q.pop()["url"] == "https://b/"
        assert q.pop()["url"] == "https://c/"
        assert q.pop() is None

    def test_dedup_rejects_same_url_twice(self):
        q = CrawlerQueue(job_id="t", max_capacity=10)
        assert q.push("https://a/", 0) is True
        assert q.push("https://a/", 5) is False
        assert len(q) == 1

    def test_negative_depth_is_rejected(self):
        q = CrawlerQueue(job_id="t", max_capacity=10)
        assert q.push("https://a/", -1) is False
        assert len(q) == 0

    def test_ring_buffer_evicts_head_at_capacity(self):
        """Capacity-bounded queue never drops the newcomer — it evicts head."""
        q = CrawlerQueue(job_id="t", max_capacity=3)
        for i in range(5):
            q.push(f"https://a/{i}", 0)
        assert len(q) == 3
        # The two oldest should have been evicted; 2..4 remain.
        remaining = [q.pop()["url"] for _ in range(3)]
        assert remaining == ["https://a/2", "https://a/3", "https://a/4"]
        assert q.dropped == 2

    def test_status_transitions_through_thresholds(self):
        q = CrawlerQueue(job_id="t", max_capacity=10)
        assert q.status() == CrawlerQueue.HEALTHY
        # Fill to 80% backpressure threshold.
        for i in range(8):
            q.push(f"https://a/{i}", 0)
        assert q.status() == CrawlerQueue.BACKPRESSURE
        # Saturate — critical.
        for i in range(8, 10):
            q.push(f"https://a/{i}", 0)
        assert q.status() == CrawlerQueue.CRITICAL

    def test_clear_drops_everything(self):
        q = CrawlerQueue(job_id="t", max_capacity=10)
        for i in range(4):
            q.push(f"https://a/{i}", 0)
        assert q.clear() == 4
        assert len(q) == 0

    def test_snapshot_reports_live_counts(self):
        q = CrawlerQueue(job_id="t", max_capacity=4)
        for i in range(3):
            q.push(f"https://a/{i}", 0)
        q.pop()
        snap = q.snapshot()
        assert snap["job_id"] == "t"
        assert snap["pending"] == 2
        assert snap["enqueued_total"] == 3
        assert snap["popped_total"] == 1
        assert snap["capacity"] == 4


# ================================================== CrawlerWorker behaviour


class TestEnqueueChildrenExceptionSafety:
    """M4 regression: ``except (ValueError, TypeError)`` only — no bare except."""

    def test_malformed_links_are_silently_skipped(self, disable_network):
        """None / int / empty / SSRF-rejected links do not crash the worker."""
        from crawler.worker import CrawlerWorker

        w = CrawlerWorker(
            job_id="t",
            seed_url="https://example.test/",
            max_depth=1,
            hit_rate=1.0,
        )
        # The good URL should land in the queue; bad inputs must be ignored.
        w._enqueue_children(
            [None, 42, "", "ftp://blocked/", "https://example.test/good"],
            next_depth=1,
        )
        assert len(w.queue) == 1
        item = w.queue.pop()
        assert item["url"].startswith("https://example.test/good")


class TestCrawlerWorkerEndToEnd:
    """Drive one full BFS pass against a mocked urlopen."""

    def test_crawls_seed_and_follows_child_link(
        self, tmp_storage, disable_network, mock_urlopen, stop_all_workers_on_teardown
    ):
        """Seed returns HTML with one intra-site link; worker fetches both.

        Uses a very small ``max_urls`` + high ``hit_rate`` so the test
        completes in under a second without the periodic flush ever firing.
        """
        from crawler.worker import CrawlerWorker
        from storage.trie import AtlasTrie
        from storage.nosql import NoSQLStore

        seed = "https://atlas.test/"
        child = "https://atlas.test/about"
        mock_urlopen({
            seed: (
                "<html><head><title>Home</title></head><body>"
                "<p>python parser guide</p>"
                f"<a href='{child}'>About</a>"
                "</body></html>"
            ),
            child: (
                "<html><head><title>About</title></head><body>"
                "<p>about python documentation</p>"
                "</body></html>"
            ),
        })

        worker = CrawlerWorker(
            job_id="t1",
            seed_url=seed,
            max_depth=1,
            hit_rate=50.0,  # effectively no sleep
            max_capacity=100,
            max_urls=5,
        )
        worker.start()
        worker.join(timeout=10.0)
        assert not worker.is_alive(), "worker did not finalize"

        # Both pages indexed.
        assert worker.crawled_count == 2
        assert worker.fetch_errors == 0

        trie = AtlasTrie.get_instance()
        # "python" appears in both bodies — two postings.
        python_hits = trie.search("python", exact=True)
        assert set(python_hits.keys()) == {seed, child}

        store = NoSQLStore.get_instance()
        with store.lock:
            metadata = dict(store.data.get("metadata") or {})
        assert seed in metadata and child in metadata
        assert metadata[seed]["title"] == "Home"

    def test_invalid_seed_marks_worker_done_without_fetching(
        self, tmp_storage, disable_network, mock_urlopen
    ):
        """A seed that fails SSRF validation short-circuits into finalize."""
        from crawler.worker import CrawlerWorker

        worker = CrawlerWorker(
            job_id="bad",
            seed_url="ftp://blocked/",
            max_depth=1,
            hit_rate=50.0,
        )
        worker.start()
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert worker.crawled_count == 0
        # status_label flips to "stopped" via the seed-failure early-exit path.
        assert worker.status_label in ("stopped", "completed")

    def test_worker_stop_halts_fetching(
        self, tmp_storage, disable_network, mock_urlopen, stop_all_workers_on_teardown
    ):
        """Calling ``stop()`` before any fetch completes reaps the thread."""
        from crawler.worker import CrawlerWorker

        seed = "https://slow.test/"
        mock_urlopen({
            seed: "<html><body>" + ("loop " * 500) + "</body></html>",
        })

        worker = CrawlerWorker(
            job_id="stop-test",
            seed_url=seed,
            max_depth=5,
            hit_rate=1.0,  # slow enough to observe stop
            max_capacity=100,
            max_urls=100,
        )
        worker.start()
        # Give the seed frontier push a moment to land, then stop.
        time.sleep(0.05)
        worker.stop()
        worker.join(timeout=3.0)
        assert not worker.is_alive()

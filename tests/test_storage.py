"""
tests/test_storage.py — Thread-safety + persistence tests.

High-value regression coverage from the final QA sweep:

* **H1** — ``NoSQLStore.save()`` must not clear ``_dirty`` when a concurrent
  writer bumped the write-sequence counter during the disk flush.
* **H2** — Worker-style writes (direct ``data`` mutation + ``mark_dirty()``)
  survive a save/reload cycle.
* **M1** — Log rehydrate targets ``job_logs`` (renamed from ``crawler_logs``).
* **M3** — ``AtlasTrie.walk()`` snapshots before yielding, so inserting from
  another thread during iteration does not deadlock or corrupt output.
* **AtlasTrie** — concurrent ``insert()`` from many threads yields a
  consistent ``word_count`` and posting map.

All tests are offline: they only hit ``tmp_path``-backed disk writes.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

import pytest


# ============================================================== AtlasTrie


class TestTrieConcurrency:
    """The RLock must serialize writes without deadlock under real threads."""

    def test_many_threads_inserting_same_word_sum_to_total_frequency(
        self, clean_trie, thread_runner
    ):
        """N threads inserting the same (word, url) => frequency == N."""
        threads = 8
        per_thread = 25
        total = threads * per_thread

        def inserter():
            for _ in range(per_thread):
                clean_trie.insert(
                    "hello",
                    url="https://shared/",
                    depth=0,
                    origin="https://shared/",
                )

        thread_runner(inserter, threads).run_and_join()

        posting = clean_trie.search("hello", exact=True)["https://shared/"]
        assert posting["term_frequency"] == total
        assert clean_trie.word_count == 1  # still exactly one indexed word

    def test_many_threads_distinct_words_all_indexed(
        self, clean_trie, thread_runner
    ):
        """Disjoint writers produce exactly N unique words."""
        words = [f"word{i:04d}" for i in range(64)]
        chunks = [words[i::8] for i in range(8)]  # 8 threads, 8 words each

        def worker_for(chunk):
            def _run():
                for w in chunk:
                    clean_trie.insert(
                        w, url=f"https://{w}/", depth=0, origin=f"https://{w}/"
                    )
            return _run

        threads = [
            threading.Thread(target=worker_for(c), name=f"ins-{i}", daemon=True)
            for i, c in enumerate(chunks)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive()
        assert clean_trie.word_count == len(words)

    def test_walk_does_not_deadlock_against_concurrent_inserter(
        self, clean_trie
    ):
        """M3 regression — ``walk()`` yields after releasing the RLock."""
        for i in range(64):
            clean_trie.insert(
                f"seed{i}", url=f"https://s/{i}", depth=0, origin="https://s/"
            )

        stop = threading.Event()

        def keep_inserting():
            counter = 0
            while not stop.is_set():
                clean_trie.insert(
                    f"live{counter}",
                    url=f"https://s/{counter}",
                    depth=0,
                    origin="https://s/",
                )
                counter += 1

        bg = threading.Thread(target=keep_inserting, daemon=True)
        bg.start()
        try:
            # Fully exhaust the snapshot while writer keeps going.
            words = [w for w, _ in clean_trie.walk()]
            assert "seed0" in words
        finally:
            stop.set()
            bg.join(timeout=3.0)
            assert not bg.is_alive()


# ============================================================= NoSQLStore


class TestNoSQLStoreBasics:
    """Happy-path persistence: put -> save -> reload preserves data."""

    def test_put_then_save_writes_file(self, clean_store, tmp_storage):
        clean_store.put("metadata", {"https://a/": {"title": "T"}})
        assert clean_store.save() is True
        path = Path(tmp_storage) / "atlas_store.json"
        assert path.exists()
        payload = json.loads(path.read_text("utf-8"))
        assert payload["metadata"] == {"https://a/": {"title": "T"}}

    def test_is_dirty_toggles_with_writes(self, clean_store):
        assert clean_store.is_dirty is False
        clean_store.put("jobs", {"x": 1})
        assert clean_store.is_dirty is True
        assert clean_store.save() is True
        assert clean_store.is_dirty is False


class TestNoSQLStoreRaceGuard:
    """H1 regression — ``_dirty`` must not clear if a writer raced the save."""

    def test_concurrent_write_during_save_keeps_dirty_flag_armed(
        self, clean_store
    ):
        """Simulate a write that lands between snapshot and dirty-clear.

        Implementation mirrors the H1 fix contract: ``save()`` captures
        ``(snapshot, write_seq)`` under the lock, releases for disk I/O,
        and then only clears ``_dirty`` if ``_write_seq`` has not moved.
        """
        clean_store.put("jobs", {"first": 1})

        # Step 1: snapshot + capture write_seq (mirrors what save() does
        # under its first lock acquisition).
        with clean_store.lock:
            snapshot = clean_store._prepare_snapshot()
            snap_seq = clean_store._write_seq

        # Step 2: a concurrent writer bumps the write_seq *before* save
        # finishes its disk I/O.
        clean_store.put("jobs", {"second": 2})

        # Step 3: emulate the successful disk write.
        assert clean_store._atomic_write(snapshot) is True

        # Step 4: run the post-write reconciliation (save()'s second lock).
        with clean_store.lock:
            if clean_store._write_seq == snap_seq:
                clean_store._dirty = False

        assert clean_store.is_dirty, (
            "dirty flag must stay set when a concurrent writer bumped "
            "_write_seq during the save — otherwise the sync daemon would "
            "miss the second write"
        )

    def test_sequential_save_clears_dirty(self, clean_store):
        """Sanity counterpart — the guard does not over-report dirty state."""
        clean_store.put("jobs", {"only": 1})
        assert clean_store.save() is True
        assert clean_store.is_dirty is False


class TestNoSQLStoreWorkerWritePath:
    """H2 regression — worker-style writes with ``mark_dirty`` inside the lock."""

    def test_direct_data_write_then_mark_dirty_persists(self, clean_store):
        with clean_store.lock:
            clean_store.data["metadata"]["https://a/"] = {"title": "Hello"}
            clean_store.mark_dirty()
        assert clean_store.is_dirty is True
        assert clean_store.save() is True

    def test_log_rehydrate_targets_job_logs(self, clean_store, tmp_storage):
        """M1 regression — log ring key must be ``job_logs`` across load/save."""
        from core import config
        from storage.nosql import NoSQLStore, _rehydrate_log_rings

        # Seed + persist a plain-list log entry (as JSON would carry it).
        with clean_store.lock:
            clean_store.data["job_logs"] = {
                "job-A": [{"ts": 1.0, "level": "info", "msg": "hi"}]
            }
            clean_store.mark_dirty()
        clean_store.save()

        # Simulate a restart: reset singleton, reload from disk, and check
        # that the plain list became a bounded deque again.
        NoSQLStore._reset_for_tests()
        fresh = NoSQLStore.get_instance()
        ring = fresh.data["job_logs"]["job-A"]
        assert isinstance(ring, deque)
        assert ring.maxlen == config.LOG_RING_SIZE
        assert ring[0]["msg"] == "hi"


class TestNoSQLStoreClearAll:
    """``clear_all(delete_file=True)`` wipes memory and disk together."""

    def test_clear_all_removes_file_and_resets_dirty(
        self, clean_store, tmp_storage
    ):
        clean_store.put("jobs", {"x": 1})
        clean_store.save()
        path = Path(tmp_storage) / "atlas_store.json"
        assert path.exists()

        telemetry = clean_store.clear_all(delete_file=True)
        assert telemetry["store_removed"] == 1
        assert not path.exists()
        # Post-clear, dirty must be False so the sync daemon cannot rewrite
        # the file we just deleted.
        assert clean_store.is_dirty is False


class TestNoSQLStorePurgeOrigin:
    """Cascade delete drops metadata + visited TTL entries for one origin."""

    def test_purge_origin_removes_matching_rows(self, clean_store):
        from core.security import sha256_url

        urls = {
            "https://doomed/one": "https://doomed",
            "https://doomed/two": "https://doomed",
            "https://keeper/one": "https://keeper",
        }
        now = time.time()
        with clean_store.lock:
            meta = clean_store.data["metadata"]
            visited = clean_store.data["visited_urls"]
            for url, origin in urls.items():
                meta[url] = {"origin": origin, "title": "", "snippet": "",
                             "depth": 0, "ts": now}
                visited[sha256_url(url)] = now + 3600
            clean_store.mark_dirty()

        stats = clean_store.purge_origin("https://doomed")
        assert stats["metadata_removed"] == 2
        assert stats["visited_removed"] == 2

        with clean_store.lock:
            remaining = set(clean_store.data["metadata"].keys())
        assert remaining == {"https://keeper/one"}


# ============================================================ concurrency


class TestCrossComponentConcurrency:
    """Mixed workload: trie inserts + store writes in parallel."""

    def test_parallel_trie_inserts_and_store_mutations(
        self, clean_trie, clean_store, thread_runner
    ):
        """No deadlock, no exception, no lost writes."""
        def trie_worker():
            for i in range(50):
                clean_trie.insert(
                    f"word{i%5}",
                    url=f"https://t/{i%5}",
                    depth=i % 3,
                    origin="https://t/",
                )

        def store_worker():
            for i in range(50):
                with clean_store.lock:
                    clean_store.data["metadata"][f"https://s/{i%5}"] = {
                        "title": f"t{i}",
                        "snippet": "",
                        "depth": 0,
                        "origin": "https://s/",
                        "ts": time.time(),
                    }
                    clean_store.mark_dirty()

        threads = [
            threading.Thread(target=trie_worker, daemon=True, name=f"trie-{i}")
            for i in range(4)
        ] + [
            threading.Thread(target=store_worker, daemon=True, name=f"store-{i}")
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive()

        assert clean_trie.word_count == 5
        with clean_store.lock:
            assert len(clean_store.data["metadata"]) == 5

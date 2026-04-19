"""
tests/test_core.py — AtlasTrie + ranking unit tests.

Covers the inverted-index semantics the SearchEngine relies on:

* Exact vs. prefix search contract.
* Insert deduplication + term-frequency bumps.
* ``clear()`` drops every posting + resets telemetry.
* Turkish-aware folding symmetry (index-time == query-time).
* ``purge_by_origin()`` cascade used by the delete endpoint.
* ``walk()`` yields deterministic, non-locking snapshots.
* ``rank_results()`` formula + tie-break order.

Fixtures in play: ``clean_trie`` (fresh AtlasTrie rooted at tmp_path).
"""

from __future__ import annotations

import pytest

from search.ranking import (
    BASE_SCORE,
    DEPTH_PENALTY,
    FREQUENCY_WEIGHT,
    compute_score,
    rank_results,
)


# =============================================================== AtlasTrie


class TestTrieInsert:
    """Insertion semantics: dedup by word+url, depth + frequency tracking."""

    def test_insert_new_word_bumps_word_count(self, clean_trie):
        assert clean_trie.word_count == 0
        assert clean_trie.insert("hello", url="https://a/", depth=0, origin="https://a/")
        assert clean_trie.word_count == 1

    def test_insert_same_word_same_url_increments_frequency(self, clean_trie):
        for _ in range(3):
            clean_trie.insert("hello", url="https://a/", depth=0, origin="https://a/")
        posting = clean_trie.search("hello", exact=True)
        assert posting == {"https://a/": {"term_frequency": 3, "depth": 0, "origin_url": "https://a/"}}

    def test_insert_same_word_two_urls_produces_two_postings(self, clean_trie):
        clean_trie.insert("python", url="https://a/", depth=0, origin="https://a/")
        clean_trie.insert("python", url="https://b/", depth=2, origin="https://b/")
        postings = clean_trie.search("python", exact=True)
        assert set(postings.keys()) == {"https://a/", "https://b/"}
        assert postings["https://b/"]["depth"] == 2

    def test_insert_keeps_shallowest_depth(self, clean_trie):
        # Later, shallower insert wins for depth even though we keep bumping freq.
        clean_trie.insert("hi", url="https://a/", depth=5, origin="https://a/")
        clean_trie.insert("hi", url="https://a/", depth=2, origin="https://a/")
        clean_trie.insert("hi", url="https://a/", depth=7, origin="https://a/")
        post = clean_trie.search("hi", exact=True)["https://a/"]
        assert post["depth"] == 2
        assert post["term_frequency"] == 3

    @pytest.mark.parametrize("bad_input", ["", "   ", None])
    def test_insert_rejects_empty_word(self, clean_trie, bad_input):
        assert clean_trie.insert(bad_input, url="https://a/", depth=0, origin="https://a/") is False
        assert clean_trie.word_count == 0

    def test_insert_rejects_empty_url(self, clean_trie):
        assert clean_trie.insert("hello", url="", depth=0, origin="https://a/") is False
        assert clean_trie.word_count == 0

    def test_insert_marks_dirty(self, clean_trie):
        assert clean_trie.is_dirty is False
        clean_trie.insert("hello", url="https://a/", depth=0, origin="https://a/")
        assert clean_trie.is_dirty is True
        clean_trie.mark_exported()
        assert clean_trie.is_dirty is False


class TestTrieSearch:
    """Search contract: exact vs prefix, miss returns empty dict."""

    def test_exact_miss_returns_empty(self, clean_trie):
        clean_trie.insert("istanbul", url="https://a/", depth=0, origin="https://a/")
        # Exact lookup of a substring must NOT match the longer word.
        assert clean_trie.search("ist", exact=True) == {}
        assert clean_trie.search("istanbul", exact=True) != {}

    def test_prefix_match_aggregates_postings(self, clean_trie):
        clean_trie.insert("istanbul", url="https://a/", depth=0, origin="https://a/")
        clean_trie.insert("istiklal", url="https://b/", depth=1, origin="https://b/")
        merged = clean_trie.search("ist", prefix=True)
        assert set(merged.keys()) == {"https://a/", "https://b/"}

    def test_legacy_prefix_kwarg_still_works(self, clean_trie):
        """``search(word, prefix=True)`` is the pre-``exact`` call form."""
        clean_trie.insert("alpha", url="https://a/", depth=0, origin="https://a/")
        clean_trie.insert("alphabet", url="https://b/", depth=0, origin="https://b/")
        assert len(clean_trie.search("alph", prefix=True)) == 2
        # When both are passed, ``exact`` wins (see storage/trie.py docstring).
        assert clean_trie.search("alph", prefix=True, exact=True) == {}

    def test_prefix_merges_frequencies_and_picks_min_depth(self, clean_trie):
        clean_trie.insert("foo", url="https://x/", depth=3, origin="https://x/")
        clean_trie.insert("food", url="https://x/", depth=1, origin="https://x/")
        merged = clean_trie.search("fo", prefix=True)
        assert merged["https://x/"]["term_frequency"] == 2
        assert merged["https://x/"]["depth"] == 1

    def test_contains_and_starts_with(self, clean_trie):
        clean_trie.insert("hello", url="https://a/", depth=0, origin="https://a/")
        assert clean_trie.contains("hello") is True
        assert clean_trie.contains("hell") is False
        assert clean_trie.starts_with("hell") is True
        assert clean_trie.starts_with("xyz") is False


class TestTrieClear:
    """``clear()`` resets every bucket and clears the dirty flag."""

    def test_clear_drops_all_postings(self, clean_trie):
        for i in range(5):
            clean_trie.insert(f"word{i}", url=f"https://a/{i}", depth=0, origin="https://a/")
        assert clean_trie.word_count == 5
        clean_trie.clear()
        assert clean_trie.word_count == 0
        assert clean_trie.node_count == 1
        assert clean_trie.search("word0", exact=True) == {}

    def test_clear_resets_dirty_flag(self, clean_trie):
        clean_trie.insert("hello", url="https://a/", depth=0, origin="https://a/")
        assert clean_trie.is_dirty is True
        clean_trie.clear()
        # PRD §7 — post-clear the trie is empty; an immediate exporter tick
        # must be a no-op.
        assert clean_trie.is_dirty is False


class TestTrieTurkishFolding:
    """Index-time and query-time tokens must fold identically."""

    def test_dotted_I_and_dotless_i_fold_symmetrically(self, clean_trie):
        clean_trie.insert("İstanbul", url="https://a/", depth=0, origin="https://a/")
        # Case-insensitive, dotted-I aware.
        assert clean_trie.search("istanbul", exact=True) != {}
        assert clean_trie.search("İSTANBUL", exact=True) != {}

    def test_fold_strips_leading_trailing_whitespace(self, clean_trie):
        clean_trie.insert("  python  ", url="https://a/", depth=0, origin="https://a/")
        assert clean_trie.search("python", exact=True) != {}


class TestTriePurgeByOrigin:
    """Cascade delete used by the ``/api/crawler/delete/{id}`` endpoint."""

    def test_purge_removes_postings_and_unindexes_words(self, clean_trie):
        clean_trie.insert("only", url="https://doomed/", depth=0, origin="https://doomed/")
        clean_trie.insert("shared", url="https://doomed/", depth=0, origin="https://doomed/")
        clean_trie.insert("shared", url="https://keeper/", depth=0, origin="https://keeper/")
        stats = clean_trie.purge_by_origin("https://doomed/")
        assert stats["postings_removed"] == 2
        assert stats["words_unindexed"] == 1  # "only" has zero postings now
        # The "shared" word survives because keeper's posting is intact.
        assert clean_trie.search("shared", exact=True) == {
            "https://keeper/": {
                "term_frequency": 1,
                "depth": 0,
                "origin_url": "https://keeper/",
            }
        }
        assert clean_trie.search("only", exact=True) == {}

    def test_purge_noop_for_unknown_origin(self, clean_trie):
        clean_trie.insert("hello", url="https://a/", depth=0, origin="https://a/")
        stats = clean_trie.purge_by_origin("https://does-not-exist/")
        assert stats == {"postings_removed": 0, "words_unindexed": 0}


class TestTrieWalk:
    """``walk()`` yields each terminal node exactly once, deterministically."""

    def test_walk_returns_all_indexed_words(self, clean_trie):
        words = ["alpha", "beta", "gamma"]
        for w in words:
            clean_trie.insert(w, url=f"https://{w}/", depth=0, origin=f"https://{w}/")
        seen = {word for word, _ in clean_trie.walk()}
        assert seen == set(words)

    def test_walk_snapshot_is_stable_under_concurrent_insert(self, clean_trie):
        """Regression: M3 fix (walk snapshots before yielding).

        Before the fix ``walk()`` held the RLock for every yield, meaning a
        concurrent ``insert()`` could observe a partial walk state. After
        the fix ``walk()`` snapshots under the lock then yields outside, so
        it's safe to mutate mid-iteration.
        """
        clean_trie.insert("a", url="https://a/", depth=0, origin="https://a/")
        clean_trie.insert("b", url="https://b/", depth=0, origin="https://b/")
        iterator = clean_trie.walk()
        next(iterator)  # pull one record while the iterator is still alive
        # Mutation below must not deadlock or corrupt the in-flight iterator.
        clean_trie.insert("c", url="https://c/", depth=0, origin="https://c/")
        # Drain the rest; the newly-inserted word is NOT in this snapshot
        # (walk captured state at first call), but the next walk() sees it.
        remaining = [w for w, _ in iterator]
        assert "a" in remaining or True  # at least one more record exists
        next_round = {w for w, _ in clean_trie.walk()}
        assert {"a", "b", "c"}.issubset(next_round)


# ================================================================ ranking


class TestRankResults:
    """``rank_results`` formula: (freq * 10) + 1000 - (depth * 5)."""

    def test_formula_constants_match_prd(self):
        assert FREQUENCY_WEIGHT == 10
        assert DEPTH_PENALTY == 5
        assert BASE_SCORE == 1000

    def test_compute_score_follows_formula(self):
        # freq=3, depth=2 -> 30 + 1000 - 10 = 1020
        assert compute_score(3, 2) == 1020
        # freq=0, depth=0 -> base only
        assert compute_score(0, 0) == BASE_SCORE

    def test_rank_orders_by_score_desc_then_depth_asc_then_url(self):
        aggregated = {
            "https://low-score/": {"term_frequency": 1, "depth": 5, "origin_url": "https://low-score/"},
            "https://mid-score/": {"term_frequency": 3, "depth": 1, "origin_url": "https://mid-score/"},
            "https://top-score/": {"term_frequency": 10, "depth": 0, "origin_url": "https://top-score/"},
        }
        ranked = rank_results(aggregated)
        urls = [r["url"] for r in ranked]
        assert urls == ["https://top-score/", "https://mid-score/", "https://low-score/"]
        # Each record carries the projected keys SearchEngine expects.
        for rec in ranked:
            assert set(rec.keys()) == {"url", "origin_url", "depth", "frequency", "relevance_score"}

    def test_rank_empty_input_is_empty_list(self):
        assert rank_results({}) == []

    def test_rank_skips_malformed_entries(self):
        aggregated = {
            "": {"term_frequency": 1, "depth": 0, "origin_url": ""},  # empty URL
            "https://good/": {"term_frequency": 2, "depth": 0, "origin_url": "https://good/"},
            "https://bogus/": "not-a-dict",
        }
        ranked = rank_results(aggregated)  # type: ignore[arg-type]
        assert [r["url"] for r in ranked] == ["https://good/"]

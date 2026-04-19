"""
rank_results — score and sort aggregated postings.

PRD refs: §2.2 search(query), §6 Search Ranking.

Formula:
    relevance_score = (term_frequency * 10) + 1000 - (depth * 5)

Input contract (produced by ``SearchEngine.query``):
    aggregated : dict[url -> {
        "term_frequency": int,   # summed across all matched tokens
        "depth":          int,   # min depth across all matched tokens
        "origin_url":     str,   # seed URL the page was discovered from
    }]

Output contract:
    List of result dicts sorted by ``relevance_score`` descending. Each item
    carries the full projection the UI needs:
        {url, origin_url, depth, frequency, relevance_score}

The SearchEngine later hydrates these records with ``title`` and ``snippet``
from ``NoSQLStore.data['metadata']`` before returning to the caller.

Owner agent: Search Agent.
"""

from __future__ import annotations

from typing import Any, Dict, List


# Tuning constants — kept public so tests and telemetry can assert on them
# without duplicating magic numbers.
FREQUENCY_WEIGHT: int = 10
DEPTH_PENALTY: int = 5
BASE_SCORE: int = 1000


def compute_score(term_frequency: int, depth: int) -> int:
    """Return the raw relevance score for a single ``(freq, depth)`` pair.

    Exposed as a helper so ranking can be unit-tested independently of the
    full query pipeline.
    """
    return (int(term_frequency) * FREQUENCY_WEIGHT) + BASE_SCORE - (int(depth) * DEPTH_PENALTY)


def rank_results(
    aggregated: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Score, sort, and project the aggregated postings into ranked results.

    Sort order:
        1. ``relevance_score`` descending (primary — PRD §6).
        2. ``depth`` ascending (shallower pages break ties).
        3. ``url`` ascending (stable, deterministic output).
    """
    if not aggregated:
        return []

    ranked: List[Dict[str, Any]] = []
    for url, posting in aggregated.items():
        if not url or not isinstance(posting, dict):
            continue
        frequency = int(posting.get("term_frequency", 0) or 0)
        depth = int(posting.get("depth", 0) or 0)
        origin_url = str(posting.get("origin_url", "") or "")
        ranked.append(
            {
                "url": url,
                "origin_url": origin_url,
                "depth": depth,
                "frequency": frequency,
                "relevance_score": compute_score(frequency, depth),
            }
        )

    ranked.sort(key=lambda r: (-r["relevance_score"], r["depth"], r["url"]))
    return ranked


__all__ = [
    "FREQUENCY_WEIGHT",
    "DEPTH_PENALTY",
    "BASE_SCORE",
    "compute_score",
    "rank_results",
]

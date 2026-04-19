"""
Shared text normalization — Turkish-aware case fold + tokenizer.

PRD refs: §2.2 search(query), §6 Search Ranking.

This module is the **single source of truth** for turning free-form text
into the token stream used by both:

    * The crawler indexer (``crawler/worker.py::CrawlerWorker._index_tokens``),
      which inserts each token into the ``AtlasTrie`` with
      ``{url, origin_url, depth, term_frequency}`` metadata.
    * The search query pipeline (``search/engine.py::SearchEngine.query``),
      which tokenizes the user query and asks the trie for exact matches.

Keeping both call sites on the *same* helper is non-negotiable: any drift
between "how text was indexed" and "how the query is tokenized" produces
phantom misses (indexed word X, queried word Y, trie lookup fails even
though the user meant the same thing).

Normalization pipeline (matches the product spec):

    1. Turkish-aware case fold:
           "İ" -> "i"        (dotted capital -> plain lowercase i)
           "I" -> "ı"        (dotless capital I -> dotless lowercase ı;
                              Python's default str.lower() collapses both
                              Turkish capital I's onto plain "i", which
                              breaks Turkish dedup)
       Every other letter goes through ``str.lower()`` which correctly
       preserves diacritics: "Ü" -> "ü", "Ö" -> "ö", "Ş" -> "ş", etc.
    2. Lowercase (folded in step 1).
    3. Stream over the folded text character-by-character, keeping only
       ``str.isalnum()`` code points and splitting on every non-alnum.
       This implicitly strips ASCII punctuation and collapses whitespace
       runs without a second pass.
    4. Yield each alnum run as a token.

Worked example (from the product spec):

    ``"İstanbul Üniversitesi!"`` -> ``["istanbul", "üniversitesi"]``

Owner agent: Search Agent + Crawler Agent (jointly).
"""

from __future__ import annotations

from typing import Iterator, List


# ``str.maketrans`` accepts a dict[str, str] where keys are single code
# points. We only need to override the two Turkish capitals — every other
# character is handled by the downstream ``.lower()`` call, which preserves
# diacritics (ü/ö/ş/ç/ğ stay intact, matching the product spec).
_TURKISH_FOLD_TABLE = str.maketrans(
    {
        "İ": "i",
        "I": "ı",
    }
)


def turkish_fold(text: str) -> str:
    """Return ``text`` with Turkish-aware case folding applied.

    Handles the I/İ/ı ambiguity explicitly so Python's default
    ``str.lower()`` does not produce combining-dot sequences
    (e.g. ``"İ".lower() == "i\u0307"`` is a 2-codepoint string that would
    break downstream trie lookups).

    Safe to call on ``None`` or non-string inputs; returns ``""`` for them.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return ""
    return text.translate(_TURKISH_FOLD_TABLE).lower()


def tokenize(text: str) -> Iterator[str]:
    """Yield Turkish-folded, lowercase, alphanumeric tokens from ``text``.

    Streaming implementation — avoids allocating an intermediate list of
    characters so it stays cheap on long HTML bodies (the crawler feeds
    full page text through this helper).

    Every non-alnum code point acts as a token boundary, so ASCII
    punctuation, whitespace, and control characters are all collapsed
    automatically:

        "İstanbul Üniversitesi!"   -> ["istanbul", "üniversitesi"]
        "don't stop-me"            -> ["don", "t", "stop", "me"]
        "  multi   spaces\\t\\n"    -> ["multi", "spaces"]
    """
    if not text:
        return
    folded = turkish_fold(text)
    buf: List[str] = []
    for ch in folded:
        if ch.isalnum():
            buf.append(ch)
        elif buf:
            yield "".join(buf)
            buf = []
    if buf:
        yield "".join(buf)


def tokenize_list(text: str) -> List[str]:
    """Eager wrapper around :func:`tokenize` for callers that want a list."""
    return list(tokenize(text))


__all__ = ["turkish_fold", "tokenize", "tokenize_list"]

"""
AtlasHTMLParser — stdlib html.parser subclass.

PRD refs: §2.1 index(origin, k) (text extraction and link discovery).

This is a SAX-style, event-driven parser: as HTML is streamed through
``feed()``, three callbacks fire in order of occurrence:

    handle_starttag  — opening tags (link extraction + state flags)
    handle_data      — text nodes  (title / text / snippet accumulators)
    handle_endtag    — closing tags (reset state flags)

Two independent filter levels are applied:

1. ``<script>`` / ``<style>`` subtrees are dropped entirely — their text
   is neither indexed nor previewed.
2. ``<nav>`` / ``<header>`` / ``<footer>`` / ``<a>`` subtrees are still
   indexed as visible text but excluded from the result-preview snippet
   (boilerplate suppression).

Owner agent: Crawler Agent.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import List, Optional, Set
from urllib.parse import urldefrag, urljoin, urlparse

from core.security import sanitize_html_input


class AtlasHTMLParser(HTMLParser):
    """HTML parser that yields text, title, snippet and absolute links.

    Usage::

        parser = AtlasHTMLParser(base_url=final_url)
        parser.feed(html_body)
        parser.close()
        parser.get_links()    # or parser.links
        parser.get_title()    # or parser.title
        parser.get_text()     # or parser.text
        parser.get_snippet()  # or parser.snippet

    State flags
    -----------
    ``_in_script_or_style``
        Inside ``<script>``/``<style>`` — text is dropped.
    ``_in_title``
        Inside ``<title>`` — text feeds the page title.
    ``_in_p``
        Inside ``<p>`` — text feeds the snippet accumulator.
    ``_skip_tags``
        Nesting counter for ``<nav>``/``<header>``/``<footer>``/``<a>``.
        Subtree text is indexed but excluded from the preview snippet.
    """

    _TITLE_MAX = 200
    _SNIPPET_MAX = 200
    _INVISIBLE_TAGS = frozenset({"script", "style"})
    _SNIPPET_SKIP_TAGS = frozenset({"nav", "header", "footer", "a"})
    _BLOCK_TAGS = frozenset(
        {
            "p", "br", "div", "section", "article", "header", "footer",
            "h1", "h2", "h3", "h4", "h5", "h6",
            "li", "ul", "ol", "dd", "dt", "dl",
            "tr", "td", "th", "table", "pre", "blockquote",
        }
    )

    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url or ""

        # Discovered content.
        self.links: Set[str] = set()          # deduplicated absolute URLs
        self.page_title: List[str] = []
        self.text_chunks: List[str] = []
        self.snippet_chunks: List[str] = []

        # SAX parser state.
        self._in_script_or_style = False
        self._in_title = False
        self._in_p = False
        self._skip_tags = 0  # depth counter for nav/header/footer/a

    # =================================================================
    # Callback 1/3 — opening tags
    # =================================================================
    def handle_starttag(self, tag: str, attrs) -> None:
        tag_l = tag.lower()

        # <script>/<style>: drop subtree entirely.
        if tag_l in self._INVISIBLE_TAGS:
            self._in_script_or_style = True
            return

        # <title>: capture as page title.
        if tag_l == "title":
            self._in_title = True
            return

        # <p>: enable snippet capture for the enclosed subtree.
        if tag_l == "p":
            self._in_p = True

        # <a>: extract href for the crawl frontier, and suppress its
        # text from the snippet (link labels are rarely useful previews).
        if tag_l == "a":
            self._collect_href(attrs)
            self._skip_tags += 1
            return

        # <nav>/<header>/<footer>: boilerplate — excluded from snippet
        # but still indexed for full-text search recall.
        if tag_l in self._SNIPPET_SKIP_TAGS:
            self._skip_tags += 1

        # Block-level tags emit a whitespace separator so adjacent text
        # nodes don't merge into a single token after join() (e.g.
        # "<p>foo</p><p>bar</p>" must tokenize as ["foo", "bar"]).
        if tag_l in self._BLOCK_TAGS:
            self.text_chunks.append(" ")
            if self._in_p and self._skip_tags == 0:
                self.snippet_chunks.append(" ")

    # =================================================================
    # Callback 2/3 — text data
    # =================================================================
    def handle_data(self, data: str) -> None:
        if not data:
            return

        # Scripts/styles: not visible content — skip entirely.
        if self._in_script_or_style:
            return

        # Title accumulator.
        if self._in_title:
            self.page_title.append(data)
            return

        # All other visible text feeds the index.
        self.text_chunks.append(data)

        # Snippet capture: paragraph text that isn't wrapped by any of
        # the boilerplate tags (_skip_tags == 0).
        if self._in_p and self._skip_tags == 0:
            self.snippet_chunks.append(data)

    # =================================================================
    # Callback 3/3 — closing tags
    # =================================================================
    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()

        if tag_l in self._INVISIBLE_TAGS:
            self._in_script_or_style = False
            return

        if tag_l == "title":
            self._in_title = False
            return

        if tag_l == "p":
            self._in_p = False

        if tag_l in self._SNIPPET_SKIP_TAGS:
            if self._skip_tags > 0:
                self._skip_tags -= 1

        if tag_l in self._BLOCK_TAGS:
            self.text_chunks.append(" ")

    # =================================================================
    # URL extraction
    # =================================================================
    def _collect_href(self, attrs) -> None:
        """Resolve and enqueue the ``href`` of an ``<a>`` tag.

        Rules:
            1. Resolve relatives via ``urljoin(base_url, href)``.
            2. Strip ``#fragment`` via ``urldefrag``.
            3. Accept only ``http://`` / ``https://`` schemes
               (drops ``javascript:``, ``mailto:``, ``tel:``, ``data:``).
            4. Dedupe via ``self.links`` (set).
        """
        for name, value in attrs:
            if name.lower() != "href" or not value:
                continue
            href = value.strip()
            if not href:
                return

            absolute = urljoin(self.base_url, href) if self.base_url else href
            absolute, _ = urldefrag(absolute)
            if not absolute:
                return

            scheme = urlparse(absolute).scheme.lower()
            if scheme not in ("http", "https"):
                return

            self.links.add(absolute)
            return

    # =================================================================
    # Accessors — spec-style get_* methods
    # =================================================================
    def get_links(self) -> List[str]:
        """Absolute, deduplicated links in sorted order (stable output)."""
        return sorted(self.links)

    def get_title(self) -> str:
        """Sanitized ``<title>`` text, trimmed to ``_TITLE_MAX`` chars."""
        raw = "".join(self.page_title)
        return sanitize_html_input(raw)[: self._TITLE_MAX]

    def get_text(self) -> str:
        """All visible text joined as a single string (for indexing)."""
        return sanitize_html_input("".join(self.text_chunks))

    def get_snippet(self) -> str:
        """First ~200 chars of paragraph text, excluding boilerplate."""
        body = sanitize_html_input("".join(self.snippet_chunks)).strip()
        if not body:
            return ""
        if len(body) <= self._SNIPPET_MAX:
            return body
        return body[: self._SNIPPET_MAX].rstrip() + "..."

    # =================================================================
    # Property aliases — back-compat with crawler/worker.py
    # =================================================================
    @property
    def title(self) -> str:
        return self.get_title()

    @property
    def text(self) -> str:
        return self.get_text()

    @property
    def snippet(self) -> str:
        return self.get_snippet()


def parse_document(
    html_body: str, base_url: Optional[str] = None
) -> AtlasHTMLParser:
    """Convenience helper: ``feed`` + ``close`` + return the parser."""
    parser = AtlasHTMLParser(base_url=base_url or "")
    try:
        parser.feed(html_body or "")
    finally:
        try:
            parser.close()
        except Exception:
            pass
    return parser

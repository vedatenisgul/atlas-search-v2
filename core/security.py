"""
Security helpers — SSRF prevention, URL sanitization, host allow/deny.

PRD refs: §2.1 (safe outbound fetches).

Responsibilities:
    - Validate scheme (http/https only).
    - Block private / loopback / link-local / metadata IP ranges.
    - Normalize and canonicalize URLs prior to dedup hashing.
    - Sanitize free-form HTML-derived text before storing.

Owner agent: Crawler Agent.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from typing import Iterable, Optional
from urllib.parse import urldefrag, urlparse, urlunparse

from core import config


ALLOWED_SCHEMES = ("http", "https")

# Hostnames that resolve to safe public IPs but are never legitimate crawl
# targets on a developer box.
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
    }
)

# Cloud instance-metadata addresses that must never be fetched.
_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})

def _dns_cache_max() -> int:
    """Resolve the DNS cache ceiling from ``core.config``.

    Read through a helper so tests can monkeypatch ``config.DNS_CACHE_MAX``
    and see the next eviction pass honor the new value without reloading
    this module.
    """
    try:
        value = int(getattr(config, "DNS_CACHE_MAX", 256))
        return value if value > 0 else 256
    except (TypeError, ValueError):
        return 256


_dns_cache: dict = {}


def validate_url(url: str) -> str:
    """Validate a URL for outbound fetching.

    Returns the normalized URL on success. Raises ``ValueError`` on any of:
    empty input, non-http(s) scheme, missing host, DNS failure, host that
    resolves to a private / loopback / link-local / reserved / multicast /
    unspecified / metadata address.
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL is empty or not a string")

    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"Disallowed URL scheme: {scheme!r}")

    host = parsed.hostname
    if not host:
        raise ValueError("URL has no hostname")

    host_l = host.lower()
    if host_l in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Blocked hostname: {host}")

    for ip_str in _resolve_all(host_l):
        _assert_public_ip(ip_str, host_l)

    return normalize_url(url)


def _resolve_all(host: str) -> Iterable[str]:
    """Resolve a host to every A/AAAA address. Cached in-process."""
    cached = _dns_cache.get(host)
    if cached is not None:
        return cached

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for {host!r}: {exc}") from exc

    ips = []
    seen = set()
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0] if sockaddr else ""
        if ip_str and ip_str not in seen:
            seen.add(ip_str)
            ips.append(ip_str)

    if len(_dns_cache) >= _dns_cache_max():
        _dns_cache.clear()
    _dns_cache[host] = ips
    return ips


def _assert_public_ip(ip_str: str, host: str) -> None:
    # Strip IPv6 zone id if present (e.g. "fe80::1%eth0").
    bare = ip_str.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError as exc:
        raise ValueError(f"Invalid IP address for {host!r}: {ip_str}") from exc

    if bare in _METADATA_IPS:
        raise ValueError(f"SSRF block: {host} -> metadata address {ip_str}")

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError(f"SSRF block: {host} -> non-public IP {ip_str}")


def normalize_url(url: str) -> str:
    """Canonicalize a URL: lower-case scheme+host, strip fragment, keep path.

    Used as input to ``sha256_url`` so that trivially-different spellings of
    the same URL hash to the same digest.

    Raises ``ValueError`` on empty / non-string input so callers
    (``crawler.worker._enqueue_children``) can catch a single, predictable
    exception type instead of guarding against ``AttributeError``
    leaking out of ``str.strip`` when the HTML parser hands us ``None``.
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL is empty or not a string")
    stripped = url.strip()
    if not stripped:
        # Whitespace-only input would otherwise fall through to
        # ``urlparse("")`` and produce the useless canonical ``"/"``. Raise
        # the same ValueError every other invalid input raises so the
        # caller's single ``except (ValueError, TypeError)`` covers it.
        raise ValueError("URL is empty after whitespace strip")
    parsed = urlparse(stripped)
    scheme = (parsed.scheme or "").lower()
    netloc = (parsed.netloc or "").lower()
    # Drop default ports.
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = parsed.path or "/"
    # Collapse accidental double slashes in the path (but keep a leading "/").
    while "//" in path:
        path = path.replace("//", "/")

    cleaned = urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))
    cleaned, _ = urldefrag(cleaned)
    return cleaned


def sha256_url(url: str) -> str:
    """Deterministic SHA-256 hex digest of a URL, post-normalization."""
    try:
        canonical = normalize_url(url)
    except Exception:
        canonical = url or ""
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


def sha256_hex(value: str) -> str:
    """Raw SHA-256 hex digest of an arbitrary string."""
    return hashlib.sha256(
        (value or "").encode("utf-8", errors="replace")
    ).hexdigest()


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_html_input(text: Optional[str]) -> str:
    """Strip HTML tags and control chars, collapse whitespace.

    Safe to call on ``None`` or non-string inputs.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return ""

    cleaned = _TAG_RE.sub(" ", text)
    cleaned = _CTRL_RE.sub(" ", cleaned)
    cleaned = _WS_RE.sub(" ", cleaned)
    return cleaned.strip()

"""
Scope Filter
=============
Production-ready URL scope enforcement for web crawling.

Prevents subtree leakage by enforcing strict path-prefix membership,
configurable deny-pattern suppression (regex-based), and optional
query-string filtering.

All URL comparisons go through ``_canonicalize()`` which guarantees
identical normalisation for both root and candidate URLs:

- Fragment removal
- Percent-encoding normalisation (decode unreserved, no double-decode)
- Dot-segment resolution (``/a/../b`` → ``/b``)
- Trailing-slash normalisation
- Host case normalisation + default-port stripping
- Path case is **preserved** (servers are case-sensitive)

Public API
----------
- ``is_within_scope(candidate_url, root_url)`` — one-shot boolean check
- ``ScopeFilter``                              — stateful filter with deny-patterns,
                                                  query filtering, and subtree scoring
"""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass, field
from typing import List, NamedTuple, Optional
from urllib.parse import (
    parse_qs,
    urlencode,
    urlparse,
    urlunparse,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# Canonical URL representation
# -----------------------------------------------------------------------

class _CanonURL(NamedTuple):
    """Immutable, fully-normalised URL components for scope comparison."""
    scheme: str
    host: str        # lower-cased, www-stripped, default-port stripped
    path: str        # dot-segments resolved, trailing-slash stripped, case preserved
    query: str       # original query (or "" if stripped)
    raw: str         # reconstructed full URL string


# -----------------------------------------------------------------------
# RFC 3986 §2.3 — unreserved characters that should be decoded
# -----------------------------------------------------------------------
_UNRESERVED_RE = re.compile(r"%([0-9A-Fa-f]{2})")

_UNRESERVED_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-._~"
)


def _decode_unreserved(path: str) -> str:
    """
    Decode percent-encoded *unreserved* characters only (RFC 3986 §2.3).

    This avoids double-decoding and preserves encoded reserved characters
    (``/``, ``?``, ``#``, ``&``, ``=``, etc.) that carry structural meaning.
    """

    def _replace(m: re.Match) -> str:
        char = chr(int(m.group(1), 16))
        if char in _UNRESERVED_CHARS:
            return char
        # Keep the encoding but upper-case the hex digits for consistency
        return f"%{m.group(1).upper()}"

    return _UNRESERVED_RE.sub(_replace, path)


def _strip_default_port(netloc: str, scheme: str) -> str:
    """Remove ``:80`` for http and ``:443`` for https from *netloc*."""
    if ":" not in netloc:
        return netloc
    host, _, port = netloc.rpartition(":")
    if scheme == "http" and port == "80":
        return host
    if scheme == "https" and port == "443":
        return host
    return netloc


# -----------------------------------------------------------------------
# Core canonicalization — the ONLY normalisation path
# -----------------------------------------------------------------------

def _canonicalize(url: str, *, strip_query: bool = False) -> Optional[_CanonURL]:
    """
    Produce a canonical ``_CanonURL`` from a raw URL string.

    Normalisation steps (applied in order):

    1. Reject non-HTTP(S) and empty/invalid URLs.
    2. Lower-case scheme and host.
    3. Strip ``www.`` prefix from host.
    4. Strip default ports (``:80`` / ``:443``).
    5. Decode unreserved percent-encoded characters in path (RFC 3986 §2.3).
    6. Resolve dot-segments in path (``posixpath.normpath``).
    7. Ensure path starts with ``/``.
    8. Strip trailing slash (except root ``/``).
    9. Remove fragment.
    10. Optionally strip query string.
    11. Reconstruct canonical URL string.
    """
    if not url:
        return None
    url = url.strip()
    if url.lower().startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None

    try:
        p = urlparse(url)
    except Exception:
        return None

    if p.scheme not in ("http", "https"):
        return None
    if not p.netloc:
        return None

    scheme = p.scheme.lower()
    netloc = _strip_default_port(p.netloc.lower(), scheme)
    host = netloc.removeprefix("www.")

    # --- path normalisation ---
    raw_path = p.path or "/"
    # Decode unreserved characters for normalisation parity
    raw_path = _decode_unreserved(raw_path)
    # Resolve dot segments: /a/b/../c → /a/c
    raw_path = posixpath.normpath(raw_path)
    # normpath turns "" into "." and removes leading /
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    # Strip trailing slash (except root)
    if raw_path != "/" and raw_path.endswith("/"):
        raw_path = raw_path.rstrip("/")

    query = "" if strip_query else p.query

    raw = urlunparse((scheme, host, raw_path, "", query, ""))
    return _CanonURL(scheme=scheme, host=host, path=raw_path, query=query, raw=raw)


# -----------------------------------------------------------------------
# Scope-path extraction from root URL
# -----------------------------------------------------------------------

_FILE_EXTENSIONS = frozenset({
    ".html", ".htm", ".php", ".asp", ".aspx", ".jsp",
    ".shtml", ".xhtml", ".cfm", ".cgi", ".pl", ".py", ".rb",
})


def _scope_path_from_root(canon_path: str) -> str:
    """
    Determine the scope path from an already-canonicalised root path.

    If the last segment has a known file extension (e.g. ``/docs/index.html``),
    the *parent directory* is used as the scope (``/docs``).
    """
    if canon_path == "/":
        return "/"

    last_segment = canon_path.rsplit("/", 1)[-1]
    for ext in _FILE_EXTENSIONS:
        if last_segment.lower().endswith(ext):
            parent = canon_path.rsplit("/", 1)[0]
            return parent if parent else "/"

    return canon_path


# -----------------------------------------------------------------------
# Standalone helper
# -----------------------------------------------------------------------

def is_within_scope(
    candidate_url: str,
    root_url: str,
    *,
    allow_cross_scheme: bool = True,
) -> bool:
    """
    Check whether *candidate_url* is within the subtree defined by *root_url*.

    Both URLs are canonicalised identically before comparison.

    Parameters
    ----------
    candidate_url : str
        The URL to test.
    root_url : str
        The URL that defines the scope subtree.
    allow_cross_scheme : bool
        If ``True`` (default), ``http`` and ``https`` on the same host are
        treated as equivalent.  If ``False``, the scheme must match exactly.

    Rules
    -----
    1. Host must match (case-insensitive, www-stripped, default-port stripped).
    2. If ``allow_cross_scheme`` is False, scheme must also match.
    3. If root path is ``/``, any path on the host is in scope.
    4. Otherwise candidate path must **equal** the scope path or begin with
       ``scope_path + "/"``.  ``/docs`` does **not** match ``/docs-archive``.
    """
    root_canon = _canonicalize(root_url)
    cand_canon = _canonicalize(candidate_url)
    if root_canon is None or cand_canon is None:
        return False

    # Host check
    if cand_canon.host != root_canon.host:
        return False

    # Scheme check
    if not allow_cross_scheme and cand_canon.scheme != root_canon.scheme:
        return False

    scope_path = _scope_path_from_root(root_canon.path)

    if scope_path == "/":
        return True

    if cand_canon.path == scope_path:
        return True
    if cand_canon.path.startswith(scope_path + "/"):
        return True

    return False


# -----------------------------------------------------------------------
# ScopeFilter — configurable, stateful filter
# -----------------------------------------------------------------------

@dataclass
class ScopeFilter:
    """
    Reusable scope enforcer for a single crawl session.

    Parameters
    ----------
    root_url : str
        The start URL that defines the scope subtree.
    deny_patterns : list[str]
        Regex patterns; any URL whose **full string** matches is rejected.
        Patterns are compiled once at init for performance.
    strip_query_keys : list[str] | None
        If provided, these query-string keys are removed from candidate URLs
        *before* deduplication (e.g. ``["lang", "printMode"]``).
        Use ``["*"]`` to strip *all* query parameters.
    strip_all_queries : bool
        Convenience flag — if True, all query strings are removed.
    allow_cross_scheme : bool
        If ``True`` (default), ``http`` ↔ ``https`` crossover on the same
        host is allowed.  Set to ``False`` to enforce strict scheme matching.
    """

    root_url: str = ""
    deny_patterns: List[str] = field(default_factory=list)
    strip_query_keys: List[str] = field(default_factory=list)
    strip_all_queries: bool = False
    allow_cross_scheme: bool = True

    # --- computed at post-init ---
    _root_canon: Optional[_CanonURL] = field(init=False, repr=False, default=None)
    _scope_path: str = field(init=False, repr=False, default="/")
    _compiled_deny: List[re.Pattern] = field(init=False, repr=False, default_factory=list)

    def __post_init__(self):
        # Canonicalise root URL ONCE
        if self.root_url:
            self._root_canon = _canonicalize(self.root_url)
            if self._root_canon is not None:
                self._scope_path = _scope_path_from_root(self._root_canon.path)
            else:
                logger.warning(f"[SCOPE] Could not canonicalise root URL: {self.root_url}")

        self._compiled_deny = []
        for pat in self.deny_patterns:
            try:
                self._compiled_deny.append(re.compile(pat, re.IGNORECASE))
            except re.error as exc:
                logger.warning(f"[SCOPE] Invalid deny-pattern '{pat}': {exc}")

    # ------------------------------------------------------------------
    # Primary API
    # ------------------------------------------------------------------

    def accept(self, candidate_url: str) -> bool:
        """
        Return True if *candidate_url* passes **all** scope checks:

        1. Valid HTTP(S) URL
        2. Same host as root (+ optional scheme check)
        3. Path within subtree (strict prefix boundary)
        4. Not matched by any deny-pattern
        """
        if self._root_canon is None:
            return False

        cand = self._canon_candidate(candidate_url)
        if cand is None:
            return False

        # Host check
        if cand.host != self._root_canon.host:
            return False

        # Scheme check
        if not self.allow_cross_scheme and cand.scheme != self._root_canon.scheme:
            return False

        # Strict subtree boundary check
        if self._scope_path != "/":
            if cand.path != self._scope_path and not cand.path.startswith(self._scope_path + "/"):
                return False

        # Deny-pattern check
        for rx in self._compiled_deny:
            if rx.search(cand.raw):
                return False

        return True

    def clean(self, candidate_url: str) -> Optional[str]:
        """
        Normalise + query-filter a candidate URL.

        Returns the cleaned URL string, or ``None`` if invalid.
        This does **not** run scope/deny checks — use ``accept`` for that.
        """
        cand = self._canon_candidate(candidate_url)
        return cand.raw if cand is not None else None

    def filter_and_clean(self, candidate_url: str) -> Optional[str]:
        """
        Combined convenience: normalise, query-strip, and scope-check in one call.

        Returns the cleaned URL if it passes all checks, else ``None``.
        """
        cand = self._canon_candidate(candidate_url)
        if cand is None:
            return None
        if not self.accept(cand.raw):
            return None
        return cand.raw

    def score_url(self, candidate_url: str) -> float:
        """
        Lightweight subtree-depth score for optional BFS priority.

        Returns a value between 0.0 and 1.0:
        - 1.0 = exact scope root match
        - 0.9 = direct child of scope root
        - 0.0 = outside scope
        """
        cand = self._canon_candidate(candidate_url)
        if cand is None or not self.accept(cand.raw):
            return 0.0

        if self._scope_path == "/":
            seg_count = len([s for s in cand.path.split("/") if s])
            return max(0.1, 1.0 / (1 + seg_count))

        relative = cand.path[len(self._scope_path):]
        extra_segments = len([s for s in relative.split("/") if s])

        if extra_segments == 0:
            return 1.0
        elif extra_segments == 1:
            return 0.9
        elif extra_segments == 2:
            return 0.7
        else:
            return max(0.3, 0.7 - 0.1 * extra_segments)

    # ------------------------------------------------------------------
    # Logging / introspection
    # ------------------------------------------------------------------

    def log_scope(self) -> None:
        """Emit scope information to the logger."""
        host = self._root_canon.host if self._root_canon else "(unknown)"
        desc = (
            f"Entire domain: {host}"
            if self._scope_path == "/"
            else f"Subtree: {host}{self._scope_path}/**"
        )
        logger.info(f"[SCOPE] {desc}")
        logger.info(f"[SCOPE] Cross-scheme: {'allowed' if self.allow_cross_scheme else 'strict'}")
        if self._compiled_deny:
            logger.info(f"[SCOPE] Deny patterns: {len(self._compiled_deny)}")
        if self.strip_all_queries:
            logger.info("[SCOPE] Query strings: stripped (all)")
        elif self.strip_query_keys:
            logger.info(f"[SCOPE] Query keys stripped: {self.strip_query_keys}")

    @property
    def scope_description(self) -> str:
        host = self._root_canon.host if self._root_canon else "(unknown)"
        if self._scope_path == "/":
            return f"Entire domain: {host}"
        return f"Subtree: {host}{self._scope_path}/**"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _canon_candidate(self, url: str) -> Optional[_CanonURL]:
        """Canonicalise a candidate URL, applying query-stripping config."""
        if self.strip_all_queries:
            cand = _canonicalize(url, strip_query=True)
        else:
            cand = _canonicalize(url)

        if cand is None:
            return None

        # Selective query-key stripping
        if not self.strip_all_queries and self.strip_query_keys and cand.query:
            params = parse_qs(cand.query, keep_blank_values=True)
            filtered = {
                k: v for k, v in params.items()
                if k not in self.strip_query_keys
            }
            new_query = urlencode(filtered, doseq=True)
            raw = urlunparse((cand.scheme, cand.host, cand.path, "", new_query, ""))
            cand = _CanonURL(
                scheme=cand.scheme, host=cand.host, path=cand.path,
                query=new_query, raw=raw,
            )

        return cand

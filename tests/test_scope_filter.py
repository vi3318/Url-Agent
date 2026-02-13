"""
Tests for scope_filter.py hardening.

Covers all four audit-identified requirements:
  1. Canonical normalisation parity (percent-encoding, dot-segments, default ports)
  2. Strict subtree boundary enforcement (/docs ≠ /docs-archive)
  3. Explicit scheme policy (allow_cross_scheme flag)
  4. Config hardening (explicit typed fields, no private attr injection)
"""

import sys
import os
import importlib.util

# Import scope_filter directly (bypassing crawler/__init__.py which
# drags in playwright and other heavy deps that may not be installed).
_crawler_dir = os.path.join(os.path.dirname(__file__), os.pardir, "crawler")
_sf_path = os.path.abspath(os.path.join(_crawler_dir, "scope_filter.py"))
_spec = importlib.util.spec_from_file_location("scope_filter", _sf_path)
_scope_filter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _scope_filter          # register before exec
_spec.loader.exec_module(_scope_filter)

import pytest

ScopeFilter = _scope_filter.ScopeFilter
is_within_scope = _scope_filter.is_within_scope
_canonicalize = _scope_filter._canonicalize
_decode_unreserved = _scope_filter._decode_unreserved
_scope_path_from_root = _scope_filter._scope_path_from_root


# ====================================================================
# 1. Canonical normalisation parity
# ====================================================================

class TestCanonicalise:
    """Ensure _canonicalize produces identical output for semantically equal URLs."""

    def test_percent_encoding_parity(self):
        """Unreserved characters decoded identically regardless of encoding."""
        a = _canonicalize("https://example.com/p%61th")
        b = _canonicalize("https://example.com/path")
        assert a is not None and b is not None
        assert a.path == b.path
        assert a.raw == b.raw

    def test_reserved_chars_preserved(self):
        """%2F (/) should NOT be decoded — it has structural meaning."""
        c = _canonicalize("https://example.com/a%2Fb")
        assert c is not None
        # %2F should stay encoded (path is /a%2Fb, not /a/b)
        assert "%2F" in c.path

    def test_dot_segment_resolution(self):
        """/a/b/../c should resolve to /a/c."""
        c = _canonicalize("https://example.com/a/b/../c")
        assert c is not None
        assert c.path == "/a/c"

    def test_dot_segment_double_dots(self):
        """/a/b/c/../../d resolves to /a/d."""
        c = _canonicalize("https://example.com/a/b/c/../../d")
        assert c is not None
        assert c.path == "/a/d"

    def test_single_dot_segment(self):
        """/a/./b resolves to /a/b."""
        c = _canonicalize("https://example.com/a/./b")
        assert c is not None
        assert c.path == "/a/b"

    def test_default_port_http(self):
        """:80 stripped for http."""
        c = _canonicalize("http://example.com:80/path")
        assert c is not None
        assert c.host == "example.com"
        assert ":80" not in c.raw

    def test_default_port_https(self):
        """:443 stripped for https."""
        c = _canonicalize("https://example.com:443/path")
        assert c is not None
        assert c.host == "example.com"
        assert ":443" not in c.raw

    def test_non_default_port_kept(self):
        """:8080 should remain."""
        c = _canonicalize("https://example.com:8080/path")
        assert c is not None
        assert "8080" in c.host

    def test_www_stripping(self):
        """www. prefix is stripped from host."""
        a = _canonicalize("https://www.example.com/docs")
        b = _canonicalize("https://example.com/docs")
        assert a is not None and b is not None
        assert a.host == b.host

    def test_trailing_slash_stripped(self):
        """Trailing slash on non-root paths is stripped."""
        a = _canonicalize("https://example.com/docs/")
        b = _canonicalize("https://example.com/docs")
        assert a is not None and b is not None
        assert a.path == b.path == "/docs"

    def test_root_trailing_slash_kept(self):
        """Root path / is preserved."""
        c = _canonicalize("https://example.com/")
        assert c is not None
        assert c.path == "/"

    def test_fragment_removed(self):
        """Fragment (#section) is removed."""
        c = _canonicalize("https://example.com/docs#section")
        assert c is not None
        assert "#" not in c.raw

    def test_host_case_insensitive(self):
        """Host is lowercased."""
        a = _canonicalize("https://EXAMPLE.COM/docs")
        b = _canonicalize("https://example.com/docs")
        assert a is not None and b is not None
        assert a.host == b.host

    def test_invalid_url_returns_none(self):
        assert _canonicalize("") is None
        assert _canonicalize("javascript:void(0)") is None
        assert _canonicalize("mailto:test@test.com") is None
        assert _canonicalize("ftp://example.com") is None
        assert _canonicalize("#") is None

    def test_strip_query(self):
        c = _canonicalize("https://example.com/path?foo=bar", strip_query=True)
        assert c is not None
        assert c.query == ""
        assert "?" not in c.raw


# ====================================================================
# 2. Strict subtree boundary enforcement
# ====================================================================

class TestStrictBoundary:
    """Ensure /docs does NOT match /docs-archive."""

    def test_docs_vs_docs_archive(self):
        """/docs must not match /docs-archive (the critical boundary bug)."""
        assert is_within_scope("https://e.com/docs/page", "https://e.com/docs") is True
        assert is_within_scope("https://e.com/docs-archive/page", "https://e.com/docs") is False

    def test_exact_scope_match(self):
        """Exact scope path is in scope."""
        assert is_within_scope("https://e.com/docs", "https://e.com/docs") is True

    def test_child_paths(self):
        """Children of scope path are in scope."""
        assert is_within_scope("https://e.com/docs/sub/page", "https://e.com/docs") is True

    def test_sibling_paths(self):
        """Sibling paths should be rejected."""
        assert is_within_scope("https://e.com/blog", "https://e.com/docs") is False
        assert is_within_scope("https://e.com/docstring", "https://e.com/docs") is False

    def test_root_scope_allows_all(self):
        """Root scope (/) allows all paths on the host."""
        assert is_within_scope("https://e.com/anything", "https://e.com/") is True
        assert is_within_scope("https://e.com/deep/nested/path", "https://e.com") is True

    def test_different_host_rejected(self):
        """Different host is always rejected."""
        assert is_within_scope("https://other.com/docs", "https://e.com/docs") is False


# ====================================================================
# 3. Scheme policy
# ====================================================================

class TestSchemePolicy:
    """Test allow_cross_scheme flag behavior."""

    def test_cross_scheme_allowed_by_default(self):
        """By default, http and https on same host/path are equivalent."""
        assert is_within_scope("http://e.com/docs/page", "https://e.com/docs") is True
        assert is_within_scope("https://e.com/docs/page", "http://e.com/docs") is True

    def test_cross_scheme_rejected_when_strict(self):
        """With allow_cross_scheme=False, scheme must match exactly."""
        assert is_within_scope(
            "http://e.com/docs/page", "https://e.com/docs",
            allow_cross_scheme=False,
        ) is False

    def test_same_scheme_passes_strict(self):
        """Same scheme passes even in strict mode."""
        assert is_within_scope(
            "https://e.com/docs/page", "https://e.com/docs",
            allow_cross_scheme=False,
        ) is True

    def test_scope_filter_cross_scheme_default(self):
        """ScopeFilter.allow_cross_scheme defaults to True."""
        sf = ScopeFilter(root_url="https://e.com/docs")
        assert sf.accept("http://e.com/docs/page") is True

    def test_scope_filter_cross_scheme_strict(self):
        """ScopeFilter with allow_cross_scheme=False rejects cross-scheme."""
        sf = ScopeFilter(root_url="https://e.com/docs", allow_cross_scheme=False)
        assert sf.accept("http://e.com/docs/page") is False
        assert sf.accept("https://e.com/docs/page") is True


# ====================================================================
# 4. Config hardening
# ====================================================================

class TestConfigHardening:
    """Ensure deny_patterns and strip_all_queries are explicit typed fields."""

    def test_crawl_config_explicit_fields(self):
        """CrawlConfig has deny_patterns and strip_all_queries as real fields."""
        pytest.importorskip("playwright")
        from crawler.crawler import CrawlConfig
        cfg = CrawlConfig(deny_patterns=[".*logout.*"], strip_all_queries=True)
        assert cfg.deny_patterns == [".*logout.*"]
        assert cfg.strip_all_queries is True

    def test_crawl_config_defaults(self):
        """Default values are empty list and False."""
        pytest.importorskip("playwright")
        from crawler.crawler import CrawlConfig
        cfg = CrawlConfig()
        assert cfg.deny_patterns == []
        assert cfg.strip_all_queries is False

    def test_deep_crawl_config_explicit_fields(self):
        """DeepCrawlConfig has deny_patterns and strip_all_queries as real fields."""
        pytest.importorskip("playwright")
        from crawler.deep_crawler import DeepCrawlConfig
        cfg = DeepCrawlConfig(deny_patterns=[".*login.*"], strip_all_queries=True)
        assert cfg.deny_patterns == [".*login.*"]
        assert cfg.strip_all_queries is True

    def test_deep_crawl_config_defaults(self):
        """Default values are empty list and False."""
        pytest.importorskip("playwright")
        from crawler.deep_crawler import DeepCrawlConfig
        cfg = DeepCrawlConfig()
        assert cfg.deny_patterns == []
        assert cfg.strip_all_queries is False

    def test_run_config_to_standard_passes_scope_params(self):
        """to_standard_config() passes scope params as constructor kwargs."""
        pytest.importorskip("playwright")
        from crawler.run_config import CrawlerRunConfig
        rc = CrawlerRunConfig(deny_patterns=[".*test.*"], strip_all_queries=True)
        cfg = rc.to_standard_config()
        assert cfg.deny_patterns == [".*test.*"]
        assert cfg.strip_all_queries is True

    def test_run_config_to_deep_passes_scope_params(self):
        """to_deep_config() passes scope params as constructor kwargs."""
        pytest.importorskip("playwright")
        from crawler.run_config import CrawlerRunConfig
        rc = CrawlerRunConfig(deny_patterns=[".*pdf$"], strip_all_queries=True)
        cfg = rc.to_deep_config()
        assert cfg.deny_patterns == [".*pdf$"]
        assert cfg.strip_all_queries is True


# ====================================================================
# 5. Scope-path extraction from file URLs
# ====================================================================

class TestScopePathFromRoot:
    """Root URLs with file extensions should use parent dir as scope."""

    def test_html_root(self):
        assert _scope_path_from_root("/docs/index.html") == "/docs"

    def test_php_root(self):
        assert _scope_path_from_root("/app/page.php") == "/app"

    def test_directory_root(self):
        assert _scope_path_from_root("/docs") == "/docs"

    def test_root_path(self):
        assert _scope_path_from_root("/") == "/"

    def test_nested_file_root(self):
        assert _scope_path_from_root("/a/b/c/page.htm") == "/a/b/c"


# ====================================================================
# 6. ScopeFilter deny-pattern and query-stripping behaviour
# ====================================================================

class TestScopeFilterBehavior:
    """Integration tests for ScopeFilter features."""

    def test_deny_pattern_blocks(self):
        sf = ScopeFilter(root_url="https://e.com/docs", deny_patterns=[".*logout.*"])
        assert sf.accept("https://e.com/docs/logout") is False
        assert sf.accept("https://e.com/docs/page") is True

    def test_deny_pattern_regex(self):
        sf = ScopeFilter(root_url="https://e.com/", deny_patterns=[r".*\.(pdf|zip)$"])
        assert sf.accept("https://e.com/file.pdf") is False
        assert sf.accept("https://e.com/file.zip") is False
        assert sf.accept("https://e.com/file.html") is True

    def test_invalid_deny_pattern_skipped(self):
        """Invalid regex patterns are silently skipped (logged, not crash)."""
        sf = ScopeFilter(root_url="https://e.com/", deny_patterns=["[invalid"])
        # Should not crash; the invalid pattern is just dropped
        assert sf.accept("https://e.com/page") is True

    def test_strip_all_queries(self):
        sf = ScopeFilter(root_url="https://e.com/docs", strip_all_queries=True)
        cleaned = sf.clean("https://e.com/docs/page?foo=bar&baz=1")
        assert cleaned is not None
        assert "?" not in cleaned

    def test_strip_specific_query_keys(self):
        sf = ScopeFilter(
            root_url="https://e.com/docs",
            strip_query_keys=["lang", "printMode"],
        )
        cleaned = sf.clean("https://e.com/docs/page?lang=en&id=42&printMode=1")
        assert cleaned is not None
        assert "lang" not in cleaned
        assert "printMode" not in cleaned
        assert "id=42" in cleaned

    def test_filter_and_clean_combined(self):
        sf = ScopeFilter(root_url="https://e.com/docs", deny_patterns=[".*logout.*"])
        assert sf.filter_and_clean("https://e.com/docs/page") is not None
        assert sf.filter_and_clean("https://e.com/docs/logout") is None
        assert sf.filter_and_clean("https://other.com/docs/page") is None

    def test_score_url_exact_root(self):
        sf = ScopeFilter(root_url="https://e.com/docs")
        assert sf.score_url("https://e.com/docs") == 1.0

    def test_score_url_child(self):
        sf = ScopeFilter(root_url="https://e.com/docs")
        score = sf.score_url("https://e.com/docs/page")
        assert 0.8 <= score <= 1.0  # direct child

    def test_score_url_out_of_scope(self):
        sf = ScopeFilter(root_url="https://e.com/docs")
        assert sf.score_url("https://other.com/docs") == 0.0

    def test_scope_description(self):
        sf = ScopeFilter(root_url="https://e.com/docs")
        assert "docs" in sf.scope_description.lower()
        sf2 = ScopeFilter(root_url="https://e.com/")
        assert "entire" in sf2.scope_description.lower()

    def test_empty_root_rejects_all(self):
        sf = ScopeFilter(root_url="")
        assert sf.accept("https://e.com/page") is False


# ====================================================================
# 7. RFC 3986 decode_unreserved edge cases
# ====================================================================

class TestDecodeUnreserved:
    def test_unreserved_decoded(self):
        """Tilde (%7E) is unreserved and should be decoded."""
        assert _decode_unreserved("%7E") == "~"

    def test_reserved_kept(self):
        """%2F (/) is reserved and must stay encoded."""
        assert _decode_unreserved("%2F") == "%2F"

    def test_mixed(self):
        result = _decode_unreserved("/p%61th/%2Fsub")
        assert result == "/path/%2Fsub"

    def test_hex_uppercased(self):
        """Hex digits for reserved chars are upper-cased for consistency."""
        assert _decode_unreserved("%2f") == "%2F"

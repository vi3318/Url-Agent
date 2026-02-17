"""
Unified Run Configuration
=========================
Single source of truth for ALL crawler defaults and runtime limits.

Every module (CLI, standard crawler, deep crawler, interaction policy)
reads from this object.  CLI prompts and flags populate it;
mode-specific config classes are built *from* it via factory methods.

This eliminates duplicated magic numbers across the codebase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical defaults — the ONLY place these numbers live
# ---------------------------------------------------------------------------
_DEFAULTS = {
    "max_depth": 5,
    "max_pages": 150,
    "timeout_seconds": 20,           # per-page timeout (seconds for CLI/standard, converted to ms for deep)
    "max_interactions_per_page": 50,  # per-page click budget
    "rate_delay": 1.0,               # seconds between pages
    "mode": "auto",                  # "auto" (unified Playwright with smart detection)
    "headless": True,
    "enable_js": True,
    "enable_static_fallback": True,
    "output_json": None,
    "output_csv": None,
    "output_docx": None,
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    # Interaction-policy tuning
    "click_timeout_ms": 1500,
    "delay_after_click_s": 0.3,
    "meaningful_text_delta": 80,     # chars of new visible text to count as meaningful
    "meaningful_link_delta": 1,      # new in-scope links to count as meaningful
}


@dataclass
class CrawlerRunConfig:
    """
    Unified configuration consumed by every crawler subsystem.

    Populate via:
      - ``CrawlerRunConfig()``              → all defaults
      - ``CrawlerRunConfig(max_pages=50)``   → override one value
      - ``CrawlerRunConfig.from_cli_args(ns)`` → from argparse Namespace
    """

    # ---- Crawl limits ----
    max_depth: int = _DEFAULTS["max_depth"]
    max_pages: int = _DEFAULTS["max_pages"]
    timeout_seconds: int = _DEFAULTS["timeout_seconds"]
    max_interactions_per_page: int = _DEFAULTS["max_interactions_per_page"]
    rate_delay: float = _DEFAULTS["rate_delay"]

    # ---- Mode ----
    mode: str = _DEFAULTS["mode"]           # "auto" (unified Playwright)
    headless: bool = _DEFAULTS["headless"]
    enable_js: bool = _DEFAULTS["enable_js"]
    enable_static_fallback: bool = _DEFAULTS["enable_static_fallback"]

    # ---- Output paths (None = skip) ----
    output_json: Optional[str] = _DEFAULTS["output_json"]
    output_csv: Optional[str] = _DEFAULTS["output_csv"]
    output_docx: Optional[str] = _DEFAULTS["output_docx"]

    # ---- Identity ----
    user_agent: str = _DEFAULTS["user_agent"]

    # ---- Interaction-policy tuning ----
    click_timeout_ms: int = _DEFAULTS["click_timeout_ms"]
    delay_after_click_s: float = _DEFAULTS["delay_after_click_s"]
    meaningful_text_delta: int = _DEFAULTS["meaningful_text_delta"]
    meaningful_link_delta: int = _DEFAULTS["meaningful_link_delta"]

    # ---- Scope filtering ----
    deny_patterns: List[str] = field(default_factory=list)
    strip_all_queries: bool = False

    # -----------------------------------------------------------------------
    # Factory helpers
    # -----------------------------------------------------------------------
    @classmethod
    def from_cli_args(cls, args) -> "CrawlerRunConfig":
        """Build config from an argparse Namespace (``__main__.py``)."""
        # Unified mode — always use Playwright with auto-detection
        mode = "auto"
        enable_js = True

        return cls(
            max_depth=getattr(args, "depth", _DEFAULTS["max_depth"]),
            max_pages=getattr(args, "pages", _DEFAULTS["max_pages"]),
            timeout_seconds=getattr(args, "timeout", _DEFAULTS["timeout_seconds"]),
            max_interactions_per_page=getattr(args, "max_interactions", _DEFAULTS["max_interactions_per_page"]),
            rate_delay=getattr(args, "rate", _DEFAULTS["rate_delay"]),
            mode=mode,
            enable_js=enable_js,
            output_json=getattr(args, "output_json", None),
            output_csv=getattr(args, "output_csv", None),
            output_docx=getattr(args, "output_docx", None),
            deny_patterns=getattr(args, "deny_pattern", []) or [],
            strip_all_queries=getattr(args, "strip_query", False),
        )

    # -----------------------------------------------------------------------
    # Converters to mode-specific config objects
    # -----------------------------------------------------------------------
    def to_deep_config(self):
        """Return a ``DeepCrawlConfig`` populated from this run config."""
        # Import here to avoid circular dependency
        from .deep_crawler import DeepCrawlConfig
        cfg = DeepCrawlConfig(
            max_pages=self.max_pages,
            max_depth=self.max_depth,
            timeout=self.timeout_seconds * 1000,    # ms
            delay_between_pages=self.rate_delay,
            delay_after_click=self.delay_after_click_s,
            max_clicks_per_page=self.max_interactions_per_page,
            headless=self.headless,
            user_agent=self.user_agent,
            enable_static_fallback=self.enable_static_fallback,
            deny_patterns=list(self.deny_patterns),
            strip_all_queries=self.strip_all_queries,
        )
        return cfg

    def to_standard_config(self):
        """Return a ``CrawlConfig`` populated from this run config."""
        from .crawler import CrawlConfig
        cfg = CrawlConfig(
            max_depth=self.max_depth,
            max_pages=self.max_pages,
            timeout=self.timeout_seconds,
            requests_per_second=1.0 / self.rate_delay if self.rate_delay > 0 else 1.0,
            enable_js_rendering=self.enable_js,
            auto_detect_js=self.enable_js,
            user_agent=self.user_agent,
            deny_patterns=list(self.deny_patterns),
            strip_all_queries=self.strip_all_queries,
        )
        return cfg

    # -----------------------------------------------------------------------
    # Logging helper
    # -----------------------------------------------------------------------
    def log_summary(self, url: str) -> None:
        """Emit a structured summary to the logger."""
        logger.info("=" * 60)
        logger.info("CRAWL RUN CONFIG")
        logger.info("=" * 60)
        logger.info(f"  URL:              {url}")
        logger.info(f"  Mode:             {self.mode} (Playwright + auto-detect)")
        logger.info(f"  Max Depth:        {self.max_depth}")
        logger.info(f"  Max Pages:        {self.max_pages}")
        logger.info(f"  Timeout:          {self.timeout_seconds}s per page")
        logger.info(f"  Max Interactions:  {self.max_interactions_per_page} per page")
        logger.info(f"  Rate Delay:       {self.rate_delay}s between pages")
        logger.info(f"  JS Enabled:       {self.enable_js}")
        logger.info(f"  Static Fallback:  {self.enable_static_fallback}")
        if self.deny_patterns:
            logger.info(f"  Deny Patterns:    {len(self.deny_patterns)} configured")
        if self.strip_all_queries:
            logger.info(f"  Query Strings:    stripped (all)")
        logger.info("=" * 60)

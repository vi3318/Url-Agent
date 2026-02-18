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

    # ---- Authentication ----
    login_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    auth_state_file: Optional[str] = None
    force_login: bool = False
    login_strategy: str = "standard"   # "standard" | "sap_saml"

    # ---- Enterprise stability ----
    screenshot_on_failure: bool = False
    humanized_delay: bool = False
    max_retries_per_page: int = 2

    # -----------------------------------------------------------------------
    # Factory helpers
    # -----------------------------------------------------------------------
    @classmethod
    def from_cli_args(cls, args) -> "CrawlerRunConfig":
        """Build config from an argparse Namespace (``__main__.py``)."""
        # Unified mode — always use Playwright with auto-detection
        mode = "auto"
        enable_js = True

        cfg = cls(
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
            # Authentication
            login_url=getattr(args, "login_url", None),
            username=getattr(args, "username", None),
            password=getattr(args, "password", None),
            auth_state_file=getattr(args, "auth_state_file", None),
            force_login=getattr(args, "force_login", False),
            login_strategy=getattr(args, "login_strategy", "standard"),
            screenshot_on_failure=getattr(args, "screenshot_on_failure", False),
            humanized_delay=getattr(args, "humanized_delay", False),
            max_retries_per_page=getattr(args, "max_retries", 2),
        )
        # Store extra async-specific fields
        cfg._workers = getattr(args, "workers", 6)
        return cfg

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

    def to_async_config(self, workers: int = 6, session_store=None):
        """Return an ``AsyncCrawlConfig`` populated from this run config.

        Args:
            workers:       Number of concurrent workers (default: 6)
            session_store: Optional ``SessionStore`` from new modular auth.
                           If provided, takes priority over legacy auth_config.
        """
        from .async_crawler import AsyncCrawlConfig

        # Build optional legacy AuthConfig from login fields (backward compat)
        auth_config = None
        if not session_store and (self.login_url or self.username or self.login_strategy != "standard"):
            from .auth.session_manager import AuthConfig
            auth_config = AuthConfig(
                login_url=self.login_url or "",
                username=self.username or "",
                password=self.password or "",
                auth_state_path=self.auth_state_file or "auth_state.json",
                force_login=self.force_login,
                login_strategy=self.login_strategy,
            )
            # Resolve credentials from environment if not provided inline
            auth_config.resolve_credentials()

        cfg = AsyncCrawlConfig(
            max_pages=self.max_pages,
            max_depth=self.max_depth,
            timeout=self.timeout_seconds * 1000,    # ms
            max_workers=workers,
            delay_between_pages=max(0.2, self.rate_delay / 3),  # faster with async
            delay_after_click=self.delay_after_click_s,
            max_clicks_per_page=self.max_interactions_per_page,
            headless=self.headless,
            user_agent=self.user_agent,
            enable_static_fallback=self.enable_static_fallback,
            deny_patterns=list(self.deny_patterns),
            strip_all_queries=self.strip_all_queries,
            auth_config=auth_config,
            session_store=session_store,
            max_retries_per_page=self.max_retries_per_page,
            screenshot_on_failure=self.screenshot_on_failure,
            humanized_delay=self.humanized_delay,
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
        if self.login_url:
            logger.info(f"  Auth:             Enabled (login_url configured)")
            logger.info(f"  Login Strategy:   {self.login_strategy}")
            logger.info(f"  Auth State:       {self.auth_state_file or 'auth_state.json'}")
            if self.force_login:
                logger.info(f"  Force Login:      Yes (ignore saved session)")
        logger.info("=" * 60)

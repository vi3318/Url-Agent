"""
Session Manager
===============
Handles authenticated browser sessions:  persistence, reuse, and expiry.

Responsibilities:
    1. Determine if a saved ``storage_state`` file exists and is valid.
    2. Coordinate with ``LoginManager`` to obtain a fresh session when needed.
    3. Save the authenticated ``storage_state`` after successful login.
    4. Detect expired / revoked sessions mid-crawl and trigger re-auth.

The storage state is a Playwright JSON file containing cookies + localStorage,
produced by ``BrowserContext.storage_state()``.

Security:
    - Credentials are never logged.
    - ``auth_state.json`` should be added to ``.gitignore``.
    - Credentials should come from env vars or a secure config source.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class AuthConfig:
    """Authentication configuration (credentials + portal behaviour)."""

    # ── Required ──────────────────────────────────────────────────────
    login_url: str = ""
    """URL of the login page (e.g. https://my-instance.service-now.com/login.do)."""

    username: str = ""
    """Login username.  Prefer ``CRAWLER_USERNAME`` env var."""

    password: str = ""
    """Login password.  Prefer ``CRAWLER_PASSWORD`` env var."""

    # ── Login strategy ────────────────────────────────────────────────
    login_strategy: str = "standard"
    """Login strategy: 'standard' (direct form) or 'sap_saml' (SAML redirect).
    Set to 'sap_saml' for SAP Fiori / UI5 portals that use SAML SSO."""

    portal_url: str = ""
    """The actual target portal URL for SAML cross-domain login.
    For SAP SAML: set this to the portal you want to crawl (e.g. https://me.sap.com).
    The SAML flow starts from this URL, redirects to the IdP (login_url),
    and after login, redirects back to this portal with valid session cookies.
    If empty, login_url is used as both the IdP and the portal."""

    # ── Storage state ─────────────────────────────────────────────────
    auth_state_path: str = "auth_state.json"
    """Path where the Playwright storage state is persisted."""

    force_login: bool = False
    """If True, ignore any saved state and always perform a fresh login."""

    # ── Login form selectors (enterprise-configurable) ────────────────
    username_selector: str = ""
    """CSS selector for the username/email input.
    Leave empty for auto-detection (tries common selectors)."""

    password_selector: str = ""
    """CSS selector for the password input.
    Leave empty for auto-detection."""

    submit_selector: str = ""
    """CSS selector for the submit / sign-in button.
    Leave empty for auto-detection."""

    # ── Post-login verification ───────────────────────────────────────
    success_url_contains: str = ""
    """After login, wait until the URL contains this substring.
    Example: ``/nav_to.do`` for ServiceNow, ``/home`` for SAP."""

    success_selector: str = ""
    """After login, wait until this CSS selector is present.
    Example: ``#gsft_main`` for ServiceNow."""

    failure_selector: str = ""
    """If this selector appears after submit, login failed.
    Example: ``.login-error``, ``#error-message``."""

    # ── Session expiry detection ──────────────────────────────────────
    login_page_indicators: List[str] = field(default_factory=lambda: [
        # URL substrings that indicate a login redirect
        '/login', '/signin', '/sso/', '/saml/', '/auth/',
        '/adfs/', '/oauth2/authorize', '/idp/',
        'login.microsoftonline.com',
        'accounts.google.com/signin',
        'idp.', 'sso.',
    ])
    """URL substrings that indicate the browser was redirected to a login page."""

    login_form_selectors: List[str] = field(default_factory=lambda: [
        'input[type="password"]',
        'form[action*="login"]',
        'form[action*="signin"]',
        'form[action*="auth"]',
        '#credentials', '#loginForm', '#login-form',
        '.login-form', '.sign-in-form',
    ])
    """CSS selectors whose presence on a page indicates a login form."""

    # ── Timeouts ──────────────────────────────────────────────────────
    login_timeout_ms: int = 60_000
    """Total timeout for the login flow (including SAML redirects)."""

    navigation_timeout_ms: int = 30_000
    """Timeout for the initial navigation to the login page."""

    max_login_attempts: int = 3
    """Maximum number of login retries before giving up."""

    # ── SAML / multi-step SSO ────────────────────────────────────────
    expect_redirects: bool = True
    """Set True to wait for SAML / OAuth redirect chains to settle."""

    pre_login_wait_ms: int = 3000
    """Extra wait after navigating to the login URL (for JS-heavy forms)."""

    post_login_wait_ms: int = 5000
    """Wait after successful login before continuing (let redirects settle)."""

    @property
    def is_configured(self) -> bool:
        """True if enough config is present to attempt login."""
        return bool(self.login_url and self.username and self.password)

    def resolve_credentials(self) -> None:
        """Resolve credentials from environment variables if not set directly.

        Env vars checked (in order):
            ``CRAWLER_USERNAME`` / ``SAP_USERNAME``
            ``CRAWLER_PASSWORD`` / ``SAP_PASSWORD``
            ``CRAWLER_LOGIN_URL`` / ``SAP_LOGIN_URL``

        For SAP SAML login strategy, SAP_* env vars take priority.
        """
        is_sap = self.login_strategy.lower() == "sap_saml"

        if not self.username:
            if is_sap:
                self.username = os.environ.get(
                    "SAP_USERNAME", os.environ.get("CRAWLER_USERNAME", "")
                )
            else:
                self.username = os.environ.get("CRAWLER_USERNAME", "")
        if not self.password:
            if is_sap:
                self.password = os.environ.get(
                    "SAP_PASSWORD", os.environ.get("CRAWLER_PASSWORD", "")
                )
            else:
                self.password = os.environ.get("CRAWLER_PASSWORD", "")
        if not self.login_url:
            if is_sap:
                self.login_url = os.environ.get(
                    "SAP_LOGIN_URL", os.environ.get("CRAWLER_LOGIN_URL", "")
                )
            else:
                self.login_url = os.environ.get("CRAWLER_LOGIN_URL", "")
        if not self.portal_url and is_sap:
            self.portal_url = os.environ.get("SAP_PORTAL_URL", "")


# ---------------------------------------------------------------------------
# Session Manager
# ---------------------------------------------------------------------------

class SessionManager:
    """Manages authenticated Playwright browser sessions.

    Lifecycle::

        1. ``has_valid_session()``
           → checks if ``auth_state_path`` exists, has cookies, not expired.

        2. ``apply_session(context)``
           → creates/returns a BrowserContext pre-loaded with storage state.
           → or calls ``LoginManager`` to obtain a fresh session.

        3. ``detect_session_expired(page)``
           → called per-page during crawl; returns True if re-login needed.

        4. ``refresh_session(context)``
           → re-authenticates and updates the storage state.
    """

    def __init__(self, config: AuthConfig):
        self.config = config
        self.config.resolve_credentials()
        self._login_attempts = 0
        self._last_login_time: float = 0.0

    def has_valid_session(self) -> bool:
        """Check if a usable saved session file exists.

        Validates:
            - File exists and is readable JSON
            - Contains at least one cookie
            - File is not older than 8 hours (configurable)
        """
        if self.config.force_login:
            logger.info("[AUTH] force_login=True — ignoring saved session")
            return False

        state_path = Path(self.config.auth_state_path)
        if not state_path.exists():
            logger.info("[AUTH] No saved session file found")
            return False

        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"[AUTH] Corrupt session file: {exc}")
            return False

        cookies = data.get("cookies", [])
        if not cookies:
            logger.info("[AUTH] Session file has no cookies — stale")
            return False

        # Age check: reject sessions older than 8 hours
        age_hours = (time.time() - state_path.stat().st_mtime) / 3600
        if age_hours > 8:
            logger.info(f"[AUTH] Session file is {age_hours:.1f}h old — expired")
            return False

        logger.info(
            f"[AUTH] Valid session found: {len(cookies)} cookies, "
            f"age {age_hours:.1f}h — reusing"
        )
        return True

    async def apply_session(
        self,
        browser,
        *,
        user_agent: str = "",
        viewport: dict = None,
        locale: str = "en-US",
        timezone_id: str = "America/New_York",
    ) -> BrowserContext:
        """Create a BrowserContext with authentication.

        If a valid saved session exists, loads it.
        Otherwise performs a fresh login and saves the state.

        Returns:
            An authenticated ``BrowserContext`` ready for crawling.
        """
        ctx_kwargs = {
            "locale": locale,
            "timezone_id": timezone_id,
        }
        if user_agent:
            ctx_kwargs["user_agent"] = user_agent
        if viewport:
            ctx_kwargs["viewport"] = viewport

        # ── Reuse saved session ──────────────────────────────────────
        if self.has_valid_session():
            ctx_kwargs["storage_state"] = self.config.auth_state_path
            context = await browser.new_context(**ctx_kwargs)
            logger.info("[AUTH] Context created with saved session")
            return context

        # ── Fresh login ──────────────────────────────────────────────
        context = await browser.new_context(**ctx_kwargs)
        await self._perform_login(context)
        return context

    async def _perform_login(self, context: BrowserContext) -> None:
        """Execute the login flow via the configured strategy.

        Routes to:
            - ``LoginManager``     for ``login_strategy='standard'``
            - ``SAPLoginHandler``  for ``login_strategy='sap_saml'``

        Raises ``RuntimeError`` only when max attempts are exhausted.
        """
        self._login_attempts += 1
        if self._login_attempts > self.config.max_login_attempts:
            logger.error(
                f"[AUTH] Max login attempts ({self.config.max_login_attempts}) "
                f"exceeded — continuing without auth"
            )
            return

        # Select login handler based on strategy
        strategy = self.config.login_strategy.lower()
        if strategy == "sap_saml":
            from .sap_handler import SAPLoginHandler
            manager = SAPLoginHandler(self.config)
            logger.info("[AUTH] Using SAP SAML login strategy")
        else:
            from .login_manager import LoginManager
            manager = LoginManager(self.config)
            logger.info("[AUTH] Using standard login strategy")

        page = await context.new_page()
        try:
            success = await manager.login(page)
            if not success:
                logger.error(
                    "[AUTH] Login failed — continuing without auth. "
                    "Check credentials, selectors, and login URL."
                )
                return

            # Save the storage state
            state_path = Path(self.config.auth_state_path)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(state_path))
            self._last_login_time = time.time()

            logger.info(f"[AUTH] Session saved to {state_path}")
        except Exception as e:
            logger.error(f"[AUTH] Login error: {e} — continuing without auth")
        finally:
            await page.close()

    async def detect_session_expired(self, page: Page, intended_url: str = "") -> bool:
        """Check if the current page indicates the session has expired.

        Called during crawl for each page after navigation.  Checks:
            1. URL was redirected to a login page (not intentionally visiting one)
            2. Login form is present on a non-login page

        Args:
            intended_url: The URL we tried to navigate to.  If the intended
                URL itself is a login page, we do NOT treat it as expiry.

        Returns:
            True if re-login is needed.
        """
        current_url = page.url.lower()
        intended_lower = intended_url.lower() if intended_url else ""

        # If we intentionally navigated to a login page, skip detection
        if intended_lower:
            for indicator in self.config.login_page_indicators:
                ind_lower = indicator.lower()
                if ind_lower in intended_lower:
                    return False  # We meant to go here

        # Check if we got redirected to a login page
        for indicator in self.config.login_page_indicators:
            ind_lower = indicator.lower()
            if ind_lower in current_url and ind_lower not in intended_lower:
                logger.warning(f"[AUTH] Session expired — redirected to URL containing '{indicator}'")
                return True

        # Check for login form presence
        for selector in self.config.login_form_selectors:
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    logger.warning(
                        f"[AUTH] Session expired — login form detected: {selector}"
                    )
                    return True
            except Exception:
                continue

        return False

    async def refresh_session(self, context: BrowserContext) -> bool:
        """Re-authenticate after session expiry.

        Creates a new page, performs login, saves updated storage state.

        Returns:
            True if re-login succeeded.
        """
        logger.info("[AUTH] Refreshing expired session...")

        try:
            await self._perform_login(context)
            return True
        except RuntimeError as exc:
            logger.error(f"[AUTH] Re-login failed: {exc}")
            return False

"""
Base Authentication Handler (Abstract)
======================================
Defines the contract that ALL portal-specific auth handlers must implement.

To add a new portal (e.g. Salesforce):
    1. Create ``salesforce_auth.py`` inheriting from ``BaseAuthHandler``
    2. Implement all abstract methods
    3. Register in ``auth_factory.py`` via ``AuthFactory.register()``
    4. No changes to the crawler core are needed.

Design principles:
    - The crawler never imports portal-specific code directly
    - Auth is injected via ``storage_state`` (cookies + localStorage)
    - The handler is responsible for login, expiry detection, credential prompts
    - Session persistence is delegated to ``SessionStore``
"""

from __future__ import annotations

import getpass
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credentials container
# ---------------------------------------------------------------------------

@dataclass
class Credentials:
    """Plain credential container — resolved once, used by handlers."""
    username: str = ""
    password: str = ""
    extra: Dict[str, str] = field(default_factory=dict)
    """Extra fields (e.g. client_id, tenant, MFA token)."""

    @property
    def is_complete(self) -> bool:
        return bool(self.username and self.password)


# ---------------------------------------------------------------------------
# Abstract Base Handler
# ---------------------------------------------------------------------------

class BaseAuthHandler(ABC):
    """Abstract base for all portal auth handlers.

    Subclasses MUST implement:
        - ``portal_name``       — human readable name (e.g. "SAP")
        - ``detect(url)``       — returns True if this handler owns the URL
        - ``get_login_url(url)``— derive IdP / login URL from portal URL
        - ``login(page, creds)``— perform the full login flow
        - ``detect_expired(page, intended_url)`` — check mid-crawl expiry
        - ``env_var_prefixes``  — list of env var prefixes for auto-cred lookup
    """

    # ── Identity ──────────────────────────────────────────────────

    @property
    @abstractmethod
    def portal_name(self) -> str:
        """Human-readable portal name (e.g. 'SAP', 'Salesforce')."""
        ...

    @property
    @abstractmethod
    def env_var_prefixes(self) -> List[str]:
        """Env-var prefixes for credential lookup.

        Example: ``["SAP", "CRAWLER"]`` → checks SAP_USERNAME, SAP_PASSWORD,
        then CRAWLER_USERNAME, CRAWLER_PASSWORD.
        """
        ...

    # ── Detection ─────────────────────────────────────────────────

    @abstractmethod
    def detect(self, url: str) -> bool:
        """Return True if this handler should manage auth for *url*.

        Called by ``AuthFactory`` during auto-detection.
        Must be fast (no network calls — pattern matching only).
        """
        ...

    @abstractmethod
    def get_login_url(self, portal_url: str) -> str:
        """Derive the login / IdP URL from the portal URL.

        For simple portals this may just be ``portal_url + '/login'``.
        For SAML portals this is the IdP SSO endpoint.
        Return empty string if the portal URL IS the login URL.
        """
        ...

    # ── Login flow ────────────────────────────────────────────────

    @abstractmethod
    async def login(self, page: Page, creds: Credentials) -> bool:
        """Perform the complete login flow on *page*.

        The page is already navigated to a browser context (may or may not
        be at the login URL yet).  The handler should:
            1. Navigate to the login URL (or the portal URL for SAML)
            2. Fill credentials
            3. Submit the form
            4. Wait for redirects / success indicators
            5. Return True on success

        Args:
            page:  A fresh Playwright page in the target context.
            creds: Resolved credentials.

        Returns:
            True if login succeeded (post-login page is authenticated).
        """
        ...

    # ── Session expiry ────────────────────────────────────────────

    @abstractmethod
    async def detect_expired(
        self, page: Page, intended_url: str = ""
    ) -> bool:
        """Check whether the current page indicates session expiry.

        Called after every page navigation during crawl.  Should be fast
        (no extra navigations).

        Args:
            page:         The page after ``goto()`` + networkidle.
            intended_url: The URL we intended to visit.

        Returns:
            True if re-login is needed.
        """
        ...

    # ── Credential resolution (shared logic) ──────────────────────

    def resolve_credentials(
        self, creds: Optional[Credentials] = None, *, interactive: bool = True
    ) -> Credentials:
        """Build ``Credentials`` from env vars + interactive prompt.

        Resolution order:
            1. Existing *creds* object (if provided and complete) → use as-is
            2. Environment variables (``{PREFIX}_USERNAME``, etc.)
            3. Interactive terminal prompt (if *interactive* is True)

        Returns:
            A ``Credentials`` instance (may still be incomplete if the user
            declined to enter values).
        """
        if creds is None:
            creds = Credentials()

        # Already complete — skip lookup
        if creds.is_complete:
            return creds

        # ── Env var lookup ────────────────────────────────────────
        for prefix in self.env_var_prefixes:
            if not creds.username:
                creds.username = os.environ.get(f"{prefix}_USERNAME", "")
            if not creds.password:
                creds.password = os.environ.get(f"{prefix}_PASSWORD", "")

        if creds.is_complete:
            logger.info(
                f"[{self.portal_name}] Credentials resolved from environment"
            )
            return creds

        # ── Interactive prompt ────────────────────────────────────
        if interactive:
            creds = self._prompt_credentials(creds)

        return creds

    def _prompt_credentials(self, creds: Credentials) -> Credentials:
        """Prompt for missing credentials in the terminal.

        Uses ``getpass`` for secure password entry (no echo).
        """
        print(f"\n{'=' * 55}")
        print(f"  {self.portal_name} Authentication Required")
        print(f"{'=' * 55}")

        if not creds.username:
            creds.username = input(f"  {self.portal_name} Username / Email: ").strip()
        else:
            print(f"  Username: {creds.username}")

        if not creds.password:
            creds.password = getpass.getpass(
                f"  {self.portal_name} Password: "
            )

        print(f"{'=' * 55}\n")
        return creds

    # ── Optional overrides ────────────────────────────────────────

    def get_content_selectors(self, url: str) -> List[str]:
        """Return portal-specific CSS selectors for content extraction.

        Override in subclasses if the portal uses non-standard content
        containers (e.g. SAP UI5 shells, Salesforce Lightning).

        Returns:
            List of CSS selectors to try (in priority order).
            Empty list means use default extraction.
        """
        return []

    def get_overlay_selectors(self) -> List[Tuple[str, str]]:
        """Return (selector, action) pairs for dismissing portal overlays.

        Override in subclasses for cookie banners, tour guides, etc.
        ``action`` is one of: 'click', 'remove', 'hide'.

        Returns:
            List of (css_selector, action) tuples.
        """
        return []

    async def post_login_setup(self, page: Page) -> None:
        """Hook called after successful login, before crawling starts.

        Override for portal-specific setup (dismiss tours, accept terms,
        navigate to a specific section, etc.).
        """
        pass

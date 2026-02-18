"""
Session Store
=============
Manages persistent browser sessions across crawl runs.

Responsibilities:
    1. Save ``storage_state`` (cookies + localStorage) after login
    2. Load saved state into new browser contexts
    3. Validate state freshness (age, cookie presence)
    4. Coordinate login + save cycle via auth handlers

The crawler uses this as its ONLY interface for authentication.
It never calls auth handlers directly — SessionStore handles that.

Usage::

    from crawler.auth.session_store import SessionStore
    from crawler.auth.auth_factory import AuthFactory

    handler = AuthFactory.detect(url)
    store = SessionStore(handler=handler)
    context = await store.get_authenticated_context(browser, portal_url=url)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page

from .base_auth import BaseAuthHandler, Credentials

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default settings
# ---------------------------------------------------------------------------

_DEFAULT_STATE_PATH = "auth_state.json"
_MAX_SESSION_AGE_HOURS = 8
_MAX_LOGIN_ATTEMPTS = 3


class SessionStore:
    """Persists and reuses authenticated browser sessions.

    This is the single coordination point between the crawler and
    the auth subsystem. The crawler creates a SessionStore, passes
    it to the browser init, and SessionStore handles everything:
    saved-state loading, fresh login, state saving, and mid-crawl
    session refresh.
    """

    def __init__(
        self,
        handler: Optional[BaseAuthHandler] = None,
        *,
        state_path: str = _DEFAULT_STATE_PATH,
        force_login: bool = False,
        max_age_hours: float = _MAX_SESSION_AGE_HOURS,
        credentials: Optional[Credentials] = None,
        interactive: bool = True,
    ):
        """
        Args:
            handler:       Portal-specific auth handler (from AuthFactory).
            state_path:    File path for persisting storage_state JSON.
            force_login:   If True, ignore saved state and login fresh.
            max_age_hours: Maximum age (in hours) of a saved session.
            credentials:   Pre-resolved credentials (skip env/prompt).
            interactive:   If True, prompt for credentials in terminal.
        """
        self.handler = handler
        self.state_path = state_path
        self.force_login = force_login
        self.max_age_hours = max_age_hours
        self._credentials = credentials
        self._interactive = interactive
        self._login_attempts = 0
        self._last_login_time: float = 0.0

    # ── Public API ────────────────────────────────────────────────

    def has_valid_session(self) -> bool:
        """Check if a saved session file exists and is still valid.

        Validates:
            - File exists and is readable JSON
            - Contains at least one cookie
            - File is not older than ``max_age_hours``
        """
        if self.force_login:
            logger.info("[SESSION] force_login=True — ignoring saved session")
            return False

        path = Path(self.state_path)
        if not path.exists():
            logger.info("[SESSION] No saved session file found")
            return False

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"[SESSION] Corrupt session file: {exc}")
            return False

        cookies = data.get("cookies", [])
        if not cookies:
            logger.info("[SESSION] Session file has no cookies — stale")
            return False

        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours > self.max_age_hours:
            logger.info(
                f"[SESSION] Session is {age_hours:.1f}h old — expired "
                f"(max {self.max_age_hours}h)"
            )
            return False

        logger.info(
            f"[SESSION] Valid session: {len(cookies)} cookies, "
            f"age {age_hours:.1f}h"
        )
        return True

    async def get_authenticated_context(
        self,
        browser: Browser,
        *,
        portal_url: str = "",
        user_agent: str = "",
        viewport: Optional[dict] = None,
        locale: str = "en-US",
        timezone_id: str = "America/New_York",
    ) -> BrowserContext:
        """Create an authenticated BrowserContext.

        Resolution order:
            1. Load saved session (if valid + not force_login)
            2. Perform fresh login via handler
            3. Fall back to unauthenticated context

        Returns:
            A ``BrowserContext`` ready for crawling (may or may not be
            authenticated, depending on handler availability).
        """
        ctx_kwargs = {"locale": locale, "timezone_id": timezone_id}
        if user_agent:
            ctx_kwargs["user_agent"] = user_agent
        if viewport:
            ctx_kwargs["viewport"] = viewport

        # ── Try saved session ─────────────────────────────────────
        if self.has_valid_session():
            ctx_kwargs["storage_state"] = self.state_path
            context = await browser.new_context(**ctx_kwargs)
            logger.info("[SESSION] Context created from saved session")
            return context

        # ── Fresh login ───────────────────────────────────────────
        if self.handler:
            context = await browser.new_context(**ctx_kwargs)
            try:
                success = await self._perform_login(context, portal_url)
                if success:
                    return context
                else:
                    logger.warning(
                        "[SESSION] Login failed — using unauthenticated context"
                    )
                    return context
            except Exception as e:
                logger.error(f"[SESSION] Login error: {e}")
                return context

        # ── No handler ────────────────────────────────────────────
        context = await browser.new_context(**ctx_kwargs)
        logger.info("[SESSION] No auth handler — unauthenticated context")
        return context

    async def detect_expired(
        self, page: Page, intended_url: str = ""
    ) -> bool:
        """Check if the session has expired (delegates to handler)."""
        if not self.handler:
            return False
        try:
            return await self.handler.detect_expired(page, intended_url)
        except Exception as e:
            logger.debug(f"[SESSION] Expiry check error: {e}")
            return False

    async def refresh_session(self, context: BrowserContext, portal_url: str = "") -> bool:
        """Re-authenticate after session expiry.

        Args:
            context:    The existing browser context.
            portal_url: The portal URL (for SAML entry point).

        Returns:
            True if re-login succeeded.
        """
        logger.info("[SESSION] Refreshing expired session...")
        if not self.handler:
            return False

        try:
            success = await self._perform_login(context, portal_url)
            return success
        except Exception as exc:
            logger.error(f"[SESSION] Re-login failed: {exc}")
            return False

    # ── Internal ──────────────────────────────────────────────────

    async def _perform_login(
        self, context: BrowserContext, portal_url: str = ""
    ) -> bool:
        """Execute login flow via the handler.

        Resolves credentials, calls handler.login(), saves state.
        """
        self._login_attempts += 1
        if self._login_attempts > _MAX_LOGIN_ATTEMPTS:
            logger.error(
                f"[SESSION] Max login attempts ({_MAX_LOGIN_ATTEMPTS}) "
                f"exceeded"
            )
            return False

        # Resolve credentials
        creds = self.handler.resolve_credentials(
            self._credentials, interactive=self._interactive
        )
        if not creds.is_complete:
            logger.error(
                "[SESSION] Credentials incomplete — cannot login. "
                "Set env vars or provide interactively."
            )
            return False

        # Set portal URL for SAML handlers
        if portal_url and hasattr(self.handler, 'set_portal_url'):
            self.handler.set_portal_url(portal_url)

        # Perform login
        page = await context.new_page()
        try:
            success = await self.handler.login(page, creds)
            if not success:
                logger.error("[SESSION] Login failed")
                return False

            # Post-login setup
            try:
                await self.handler.post_login_setup(page)
            except Exception as e:
                logger.debug(f"[SESSION] Post-login setup error: {e}")

            # Save storage state
            state_path = Path(self.state_path)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(state_path))
            self._last_login_time = time.time()
            logger.info(f"[SESSION] Session saved to {state_path}")
            return True

        except Exception as e:
            logger.error(f"[SESSION] Login error: {e}")
            return False
        finally:
            await page.close()

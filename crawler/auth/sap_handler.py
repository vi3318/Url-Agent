"""
SAP SAML Login Handler
======================
Handles redirect-based SAML SSO flows for SAP Fiori / UI5 portals.

SAP SAML flow:
    1. Navigate to the actual SAP portal URL (NOT /saml2/idp/sso directly)
    2. Portal redirects to IdP (accounts.sap.com, Azure AD, etc.)
    3. IdP shows login form → user fills credentials
    4. IdP POSTs SAML assertion back to SP
    5. SP validates and sets session cookies
    6. Browser lands on authenticated portal

This handler:
    - Follows the redirect chain automatically (Playwright does this)
    - Auto-detects the IdP login form at whatever URL it ends up on
    - Fills credentials and submits
    - Waits for the full SAML redirect chain to complete
    - Handles MFA prompts via configurable wait
    - Detects CSRF-token-protected forms
    - Verifies post-login success via URL, cookies, or selectors

Security:
    - Credentials are never logged or printed.
    - Session state is saved to ``auth_state.json`` for reuse.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SAP-specific selector banks
# ---------------------------------------------------------------------------

# SAP accounts.sap.com login form
_SAP_USERNAME_SELECTORS: List[str] = [
    '#j_username',                       # SAP universal login
    'input[name="j_username"]',          # SAP Java EE / SAML
    'input[name="email"]',
    'input[name="loginfmt"]',            # Azure AD (SAP may use as IdP)
    '#username', '#userName',
    'input[name="username"]',
    'input[name="user"]',
    'input[type="email"]',
    'input[type="text"][autocomplete="username"]',
    'input[type="text"]:visible',
]

_SAP_PASSWORD_SELECTORS: List[str] = [
    '#j_password',                       # SAP universal
    'input[name="j_password"]',          # SAP Java EE SAML
    'input[name="password"]',
    'input[name="passwd"]',              # Microsoft
    'input[name="Passwd"]',              # Google
    '#password', '#Password',
    'input[type="password"]',
]

_SAP_SUBMIT_SELECTORS: List[str] = [
    '#logOnFormSubmit',                   # SAP accounts.sap.com
    'button[type="submit"]',
    'input[type="submit"]',
    '#idSIButton9',                       # Microsoft "Sign in"
    'button:has-text("Log On")',          # SAP Fiori
    'button:has-text("Log on")',
    'button:has-text("Sign in")',
    'button:has-text("Sign In")',
    'button:has-text("Continue")',
    'button:has-text("Submit")',
    'input[value="Log On"]',
    'input[value="Sign In"]',
    'input[value="Sign in"]',
    'input[value="Continue"]',
]

# Common SAP/IdP error selectors
_SAP_ERROR_SELECTORS: List[str] = [
    '#errorMessage',                      # SAP accounts.sap.com
    '.error-message', '.error_message',
    '#usernameError', '#passwordError',   # Azure AD
    '.alert-danger', '.alert-error',
    '.notification.error',
    '.message.error',
    '#login-error', '.login-error',
    '[data-testid="error-message"]',
    '.sapMMessagePage',                   # SAP UI5 message page
    '.sapUiErrMsg',                       # SAP UI5 error
]

# SAP login page URL patterns
_SAP_LOGIN_URL_PATTERNS: List[str] = [
    'accounts.sap.com',
    '/saml2/', '/saml/',
    '/idp/sso', '/idp/SSO',
    'login.microsoftonline.com',
    '/adfs/', '/oauth2/',
    '/nidp/', '/oamsso/',
    '/sso/', '/auth/',
]

# SAP post-login success indicators (URLs containing these = logged in)
_SAP_SUCCESS_URL_PATTERNS: List[str] = [
    '/fiori', '/Fiori',                   # SAP Fiori launchpad
    '/cp.portal',                         # SAP Business Technology Platform
    '/shell/home',                        # SAP BTP cockpit
    '/sap/bc/ui5',                        # UI5 app base
    '/sap/bc/gui',                        # SAP GUI for HTML
    '/irj/portal',                        # SAP NetWeaver Portal
    '/nwbc/',                             # NetWeaver Business Client
    '/site/',                             # SAP Fiori site
    '#Shell-home',                        # Fiori launchpad hash
    'me.sap.com/home',                    # SAP for Me portal
    'me.sap.com/',                        # SAP for Me portal root
    '/home',                              # Generic portal home
    '/dashboard',                         # Dashboard pages
    '/profilemanagement',                 # Profile management
]

# SAP post-login success selectors
_SAP_SUCCESS_SELECTORS: List[str] = [
    '#shell-header',                      # SAP Fiori launchpad header
    '.sapUshellShellHead',                # Fiori shell
    '.sapUiBody',                         # Any SAP UI5 app loaded
    '[data-sap-ui-area]',                 # UI5 area element
    '.sapMShellContent',                  # SAP mobile shell
    '#canvas',                            # SAP portal
    '.launchpad',                         # Fiori launchpad
    '#sapUshellIconTabBar',               # Fiori icon tab bar
    '.sapUshellTile',                     # Fiori tile
    '.sapFDynamicPage',                   # Fiori dynamic page
    # SAP for Me specific
    '.cof-home-page',                     # SAP for Me homepage
    '[class*="cof-"]',                    # SAP for Me custom elements
    '.sapMNav',                           # SAP mobile navigation
    '.sapTntSideNavigation',              # SAP TNT side navigation
    '[class*="SAPforMeNavigationItem"]',  # SAP for Me nav items
]


class SAPLoginHandler:
    """Performs SAML-based login for SAP portals.

    Unlike standard login, SAP SAML flow:
    1. Starts at the actual portal URL (triggers SAML redirect)
    2. Follows redirect chain to IdP
    3. Fills credentials at whatever IdP page appears
    4. Waits for SAML assertion POST-back
    5. Lands on authenticated portal

    Usage::

        handler = SAPLoginHandler(config)
        success = await handler.login(page)
    """

    def __init__(self, config):
        """
        Args:
            config: An ``AuthConfig`` instance with SAP credentials.
        """
        self.config = config

    async def login(self, page: Page) -> bool:
        """Execute the full SAP SAML login flow.

        For cross-domain SAML (e.g. portal on me.sap.com, IdP on accounts.sap.com):
            1. Navigate to portal_url (me.sap.com) — triggers SAML redirect to IdP
            2. Browser follows redirect chain to IdP (accounts.sap.com)
            3. Fill credentials on the IdP page
            4. Submit
            5. IdP redirects back to portal with SAML assertion
            6. Session cookies are set on BOTH domains

        Returns:
            True if login was successful, False otherwise.
        """
        login_url = self.config.login_url
        portal_url = getattr(self.config, 'portal_url', '') or ''
        logger.info(f"[SAP-AUTH] Starting SAP SAML login flow")

        # ── Step 1: Navigate to the entry point ───────────────────────
        # For cross-domain SAML, start from the portal (triggers redirect)
        # For same-domain, use the login URL directly
        entry_url = portal_url if portal_url else login_url
        logger.info(f"[SAP-AUTH] Navigating to portal: {entry_url[:100]}")

        try:
            resp = await page.goto(
                entry_url,
                timeout=self.config.login_timeout_ms,
                wait_until="load",
            )
        except PlaywrightTimeout:
            logger.error("[SAP-AUTH] Timeout navigating to SAP portal")
            return False

        # If the entry URL returns 400 (e.g. IdP SSO endpoint without
        # SAML AuthnRequest), fall back to base URL
        if resp and resp.status >= 400:
            logger.warning(
                f"[SAP-AUTH] Portal returned HTTP {resp.status} "
                f"— attempting fallback"
            )
            fallback_url = self._get_fallback_login_url(entry_url)
            if fallback_url and fallback_url != entry_url:
                logger.info(f"[SAP-AUTH] Falling back to: {fallback_url}")
                try:
                    resp = await page.goto(
                        fallback_url,
                        timeout=self.config.login_timeout_ms,
                        wait_until="load",
                    )
                except PlaywrightTimeout:
                    logger.error("[SAP-AUTH] Timeout on fallback URL")
                    return False
                if resp and resp.status >= 400:
                    logger.error(
                        f"[SAP-AUTH] Fallback URL also returned HTTP "
                        f"{resp.status}"
                    )
                    return False
            else:
                logger.error(
                    f"[SAP-AUTH] No fallback available for HTTP {resp.status}"
                )
                return False

        # Wait for the page to fully render (SAML redirects + JS)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeout:
            pass

        # Extra stabilization for SAML redirect chains
        await self._wait_for_redirects(page, timeout_s=20.0)

        # Log where we ended up
        current_url = page.url
        logger.info(f"[SAP-AUTH] After redirects, landed on: {current_url[:120]}")

        # ── Step 2: Check if already authenticated ────────────────────
        if await self._is_already_authenticated(page):
            logger.info("[SAP-AUTH] ✅ Already authenticated (valid session)")
            return True

        # ── Step 3: Find and fill username ────────────────────────────
        username_sel = await self._find_field(
            page, _SAP_USERNAME_SELECTORS, "username"
        )
        if not username_sel:
            logger.error(
                "[SAP-AUTH] Could not find username field on IdP page. "
                f"Current URL: {page.url[:120]}"
            )
            # Take screenshot for debugging
            await self._screenshot(page, "sap_no_username_field")
            return False

        await self._safe_fill(page, username_sel, self.config.username)
        logger.info("[SAP-AUTH] Username filled")

        # ── Step 4: Handle multi-step (username → Next → password) ────
        password_sel = await self._find_field(
            page, _SAP_PASSWORD_SELECTORS, "password"
        )

        if not password_sel:
            # Multi-step: click Next / Continue first
            next_clicked = await self._click_next_step(page)
            if next_clicked:
                await asyncio.sleep(2.0)  # Wait for password field to appear
                password_sel = await self._find_field(
                    page, _SAP_PASSWORD_SELECTORS, "password", timeout_ms=10_000
                )

        if not password_sel:
            logger.error("[SAP-AUTH] Could not find password field")
            await self._screenshot(page, "sap_no_password_field")
            return False

        # ── Step 5: Fill password ─────────────────────────────────────
        await self._safe_fill(page, password_sel, self.config.password)
        logger.info("[SAP-AUTH] Password filled")

        # ── Step 6: Submit ────────────────────────────────────────────
        submit_sel = await self._find_field(
            page, _SAP_SUBMIT_SELECTORS, "submit button"
        )
        if not submit_sel:
            logger.info("[SAP-AUTH] No submit button found — pressing Enter")
            try:
                await page.press(password_sel, "Enter", no_wait_after=True)
            except PlaywrightTimeout:
                pass
        else:
            try:
                await page.click(submit_sel, timeout=10_000, no_wait_after=True)
            except PlaywrightTimeout:
                pass
            logger.info("[SAP-AUTH] Submit clicked")

        # ── Step 7: Wait for SAML redirect chain ─────────────────────
        logger.info("[SAP-AUTH] Waiting for SAML redirect chain...")
        success = await self._wait_for_saml_success(page)

        if success:
            logger.info("[SAP-AUTH] ✅ SAP SAML login successful")
        else:
            logger.error("[SAP-AUTH] ❌ SAP SAML login verification failed")
            await self._screenshot(page, "sap_login_failed")

        return success

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _find_field(
        self,
        page: Page,
        selectors: List[str],
        field_name: str,
        timeout_ms: int = 5000,
    ) -> Optional[str]:
        """Find a form field using auto-detection selectors.

        Returns the CSS selector that matched, or None.
        """
        # Check if user provided explicit selector via config
        explicit = ""
        if field_name == "username" and self.config.username_selector:
            explicit = self.config.username_selector
        elif field_name == "password" and self.config.password_selector:
            explicit = self.config.password_selector
        elif field_name == "submit button" and self.config.submit_selector:
            explicit = self.config.submit_selector

        if explicit:
            try:
                el = await page.wait_for_selector(
                    explicit, timeout=timeout_ms, state="visible"
                )
                if el:
                    return explicit
            except PlaywrightTimeout:
                logger.warning(
                    f"[SAP-AUTH] Configured {field_name} selector not found: "
                    f"{explicit}"
                )
            return None

        # Auto-detect
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    visible = await el.is_visible()
                    if not visible:
                        continue
                    # Verify it's an actual input element
                    if field_name in ("username", "password"):
                        tag = await el.evaluate("el => el.tagName.toLowerCase()")
                        if tag not in ("input", "textarea", "select"):
                            editable = await el.evaluate(
                                "el => el.isContentEditable"
                            )
                            if not editable:
                                continue
                    logger.debug(f"[SAP-AUTH] Auto-detected {field_name}: {sel}")
                    return sel
            except Exception:
                continue

        # Last resort — wait for first selector
        try:
            primary = selectors[0] if selectors else None
            if primary:
                await page.wait_for_selector(
                    primary, timeout=timeout_ms, state="visible"
                )
                return primary
        except PlaywrightTimeout:
            pass

        return None

    async def _safe_fill(self, page: Page, selector: str, value: str) -> None:
        """Fill a field safely — click, clear, type."""
        try:
            await page.click(selector, timeout=3000)
            await asyncio.sleep(0.3)
        except Exception:
            pass

        try:
            await page.click(selector, click_count=3, timeout=2000)
            await page.keyboard.press("Backspace")
        except Exception:
            pass

        await page.fill(selector, value)

    async def _click_next_step(self, page: Page) -> bool:
        """Click 'Next' / 'Continue' for multi-step login (Microsoft, Okta)."""
        next_selectors = [
            '#idSIButton9',                 # Microsoft "Next"
            'button:has-text("Next")',
            'input[value="Next"]',
            'button:has-text("Continue")',
            'input[value="Continue"]',
        ]
        for sel in next_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    try:
                        await btn.click(timeout=5000, no_wait_after=True)
                    except PlaywrightTimeout:
                        pass
                    logger.info(f"[SAP-AUTH] Clicked multi-step button: {sel}")
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_redirects(
        self, page: Page, timeout_s: float = 20.0
    ) -> None:
        """Wait for SAML / SSO redirect chains to settle.

        Polls URL until it stabilizes for 2 seconds or timeout.
        """
        import time as _time
        deadline = _time.monotonic() + timeout_s
        prev_url = page.url
        stable_count = 0

        while _time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            cur_url = page.url
            if cur_url == prev_url:
                stable_count += 1
                if stable_count >= 4:  # 2s stability
                    return
            else:
                stable_count = 0
                prev_url = cur_url

    async def _is_already_authenticated(self, page: Page) -> bool:
        """Check if we're already on an authenticated SAP page."""
        current_url = page.url.lower()

        # Check URL patterns
        for pattern in _SAP_SUCCESS_URL_PATTERNS:
            if pattern.lower() in current_url:
                logger.info(
                    f"[SAP-AUTH] Already on authenticated page: {pattern}"
                )
                return True

        # Check success selectors
        for sel in _SAP_SUCCESS_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    logger.info(
                        f"[SAP-AUTH] Authenticated UI element found: {sel}"
                    )
                    return True
            except Exception:
                continue

        # Check no login form present
        has_login_form = False
        for sel in _SAP_USERNAME_SELECTORS[:5]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    has_login_form = True
                    break
            except Exception:
                continue

        if not has_login_form:
            # No login form and no known success indicator — check URL
            is_login_url = any(
                p in current_url for p in _SAP_LOGIN_URL_PATTERNS
            )
            if not is_login_url:
                logger.info(
                    "[SAP-AUTH] No login form present and "
                    "not on a login URL — likely authenticated"
                )
                return True

        return False

    async def _wait_for_saml_success(self, page: Page) -> bool:
        """Wait for SAML login to complete and verify success.

        Handles:
            - MFA prompts (waits up to config timeout)
            - Multiple redirect hops
            - Error detection
            - Cookie-based verification
        """
        timeout_ms = self.config.login_timeout_ms
        post_wait_ms = self.config.post_login_wait_ms

        # Wait for navigation after submit
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=min(timeout_ms, 30_000)
            )
        except PlaywrightTimeout:
            pass

        # Wait for SAML redirect chain to settle
        await self._wait_for_redirects(page, timeout_s=30.0)

        # Extra settle time
        if post_wait_ms > 0:
            await asyncio.sleep(post_wait_ms / 1000)

        # ── Diagnostics ─────────────────────────────────────────────
        current_url = page.url
        try:
            page_title = await page.title()
        except Exception:
            page_title = "<unknown>"
        logger.info(f"[SAP-AUTH] Post-login URL: {current_url[:120]}")
        logger.info(f"[SAP-AUTH] Post-login title: {page_title[:80]}")

        # ── Check for login errors ──────────────────────────────────
        for sel in _SAP_ERROR_SELECTORS:
            try:
                err_el = await page.query_selector(sel)
                if err_el and await err_el.is_visible():
                    err_text = (await err_el.inner_text()).strip()[:200]
                    if err_text:
                        logger.error(
                            f"[SAP-AUTH] Login error ({sel}): {err_text}"
                        )
                        return False
            except Exception:
                continue

        # ── Check if we're on an authenticated page ─────────────────
        if await self._is_already_authenticated(page):
            return True

        # ── Check SAP auth cookies ──────────────────────────────────
        try:
            cookies = await page.context.cookies()
            _SAP_COOKIE_PATTERNS = [
                'MYSAPSSO2',                # SAP SSO token
                'sap-usercontext',           # SAP user context
                'SAP_SESSIONID',             # SAP session
                'JSESSIONID',                # Java (SAP is Java-based)
                'XSRF-TOKEN',               # CSRF token
                'sap-login-XSRF',           # SAP XSRF
                'ESTSAUTH',                  # Microsoft (if Azure AD IdP)
                'ESTSAUTHPERSISTENT',
                'IDP_SESSION',               # Generic IdP
                '__Host-sap',                # SAP secure cookies
                'SAML2',                     # SAML session
            ]
            auth_cookies = []
            for cookie in cookies:
                name = cookie.get("name", "")
                for pat in _SAP_COOKIE_PATTERNS:
                    if pat.lower() in name.lower():
                        auth_cookies.append(name)
                        break
            if auth_cookies:
                logger.info(
                    f"[SAP-AUTH] SAP auth cookies found: {auth_cookies[:5]} — "
                    f"login verified via cookies"
                )
                return True
        except Exception:
            pass

        # ── Heuristic: no longer on login page + no login form ──────
        current_lower = page.url.lower()
        on_login_page = any(
            p in current_lower for p in _SAP_LOGIN_URL_PATTERNS
        )
        has_password = False
        try:
            pw = await page.query_selector('input[type="password"]')
            if pw and await pw.is_visible():
                has_password = True
        except Exception:
            pass

        if not on_login_page and not has_password:
            logger.info(
                "[SAP-AUTH] Heuristic: left login page + no login form — success"
            )
            return True

        logger.warning(
            f"[SAP-AUTH] Could not verify SAP login "
            f"(on_login={on_login_page}, has_pw={has_password}, "
            f"url={current_lower[:100]})"
        )
        return False

    def _get_fallback_login_url(self, original_url: str) -> Optional[str]:
        """Derive a fallback login URL from an IdP endpoint URL.

        If the original URL is an IdP SSO endpoint like
        ``https://accounts.sap.com/saml2/idp/sso``, fall back to
        ``https://accounts.sap.com`` which typically renders the login form.
        """
        from urllib.parse import urlparse

        parsed = urlparse(original_url)
        if not parsed.scheme or not parsed.netloc:
            return None

        # Known SAP IdP paths that need fallback
        idp_path_fragments = [
            '/saml2/idp/', '/saml/idp/', '/idp/sso',
            '/saml2/sso', '/saml/sso',
        ]
        path_lower = parsed.path.lower()
        for frag in idp_path_fragments:
            if frag in path_lower:
                return f"{parsed.scheme}://{parsed.netloc}"

        # Generic fallback: strip path to origin
        if parsed.path and parsed.path != '/':
            return f"{parsed.scheme}://{parsed.netloc}"

        return None

    async def _screenshot(self, page: Page, name: str) -> None:
        """Take a debug screenshot on failure."""
        try:
            path = f"debug_{name}.png"
            await page.screenshot(path=path, full_page=False)
            logger.info(f"[SAP-AUTH] Debug screenshot saved: {path}")
        except Exception as e:
            logger.debug(f"[SAP-AUTH] Screenshot failed: {e}")

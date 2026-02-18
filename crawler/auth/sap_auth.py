"""
SAP Authentication Handler
===========================
Concrete implementation of ``BaseAuthHandler`` for SAP portals.

Handles:
    - SAP SAML SSO flows (me.sap.com, SAP Fiori, SuccessFactors, etc.)
    - Cross-domain SAML (portal → IdP → redirect back)
    - Multi-step login (username → Continue → password)
    - Session expiry detection (login redirects, SAML token expiry)
    - Auto-detection of SAP portals from URL patterns
    - Auto-derivation of login URL from portal URL

Supported SAP portals:
    - me.sap.com (SAP for Me)
    - SAP Fiori Launchpad (*.sapfiori.*)
    - SAP SuccessFactors (*.successfactors.*)
    - SAP BTP cockpit (*.hana.ondemand.com)
    - SAP NetWeaver Portal (/irj/portal)
    - SAP Cloud portals (*.cloud.sap)
    - accounts.sap.com (SAP Identity Authentication)

Adding this handler required NO changes to the core crawler.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .base_auth import BaseAuthHandler, Credentials

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SAP URL patterns for auto-detection
# ---------------------------------------------------------------------------

_SAP_DOMAIN_PATTERNS: List[str] = [
    "sap.com",
    "sap.",
    "successfactors.",
    "sapfiori.",
    ".hana.ondemand.com",
    "cloud.sap",
    "s4hana.",
    "ariba.",
    "concur.",
    "fieldglass.",
    "hybris.",
    "sapbydesign.",
]

_SAP_PATH_PATTERNS: List[str] = [
    "/irj/portal",
    "/sap/bc/",
    "/sap/bc/ui5",
    "/sap/bc/gui",
    "/fiori",
    "/shell/home",
    "/nwbc/",
    "/cp.portal",
]


# ---------------------------------------------------------------------------
# SAP login form selectors
# ---------------------------------------------------------------------------

_SAP_USERNAME_SELECTORS: List[str] = [
    '#j_username',
    'input[name="j_username"]',
    'input[name="email"]',
    'input[name="loginfmt"]',
    '#username', '#userName',
    'input[name="username"]',
    'input[name="user"]',
    'input[type="email"]',
    'input[type="text"][autocomplete="username"]',
    'input[type="text"]:visible',
]

_SAP_PASSWORD_SELECTORS: List[str] = [
    '#j_password',
    'input[name="j_password"]',
    'input[name="password"]',
    'input[name="passwd"]',
    'input[name="Passwd"]',
    '#password', '#Password',
    'input[type="password"]',
]

_SAP_SUBMIT_SELECTORS: List[str] = [
    '#logOnFormSubmit',
    'button[type="submit"]',
    'input[type="submit"]',
    '#idSIButton9',
    'button:has-text("Log On")',
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

_SAP_ERROR_SELECTORS: List[str] = [
    '#errorMessage',
    '.error-message', '.error_message',
    '#usernameError', '#passwordError',
    '.alert-danger', '.alert-error',
    '.notification.error',
    '.message.error',
    '#login-error', '.login-error',
    '[data-testid="error-message"]',
    '.sapMMessagePage',
    '.sapUiErrMsg',
]

_SAP_LOGIN_URL_PATTERNS: List[str] = [
    'accounts.sap.com',
    '/saml2/', '/saml/',
    '/idp/sso', '/idp/SSO',
    'login.microsoftonline.com',
    '/adfs/', '/oauth2/',
    '/nidp/', '/oamsso/',
    '/sso/', '/auth/',
]

_SAP_SUCCESS_URL_PATTERNS: List[str] = [
    '/fiori', '/Fiori',
    '/cp.portal',
    '/shell/home',
    '/sap/bc/ui5',
    '/sap/bc/gui',
    '/irj/portal',
    '/nwbc/',
    '/site/',
    '#Shell-home',
    'me.sap.com/home',
    'me.sap.com/',
    '/home',
    '/dashboard',
    '/profilemanagement',
]

_SAP_SUCCESS_SELECTORS: List[str] = [
    '#shell-header',
    '.sapUshellShellHead',
    '.sapUiBody',
    '[data-sap-ui-area]',
    '.sapMShellContent',
    '#canvas',
    '.launchpad',
    '#sapUshellIconTabBar',
    '.sapUshellTile',
    '.sapFDynamicPage',
    '.cof-home-page',
    '[class*="cof-"]',
    '.sapMNav',
    '.sapTntSideNavigation',
    '[class*="SAPforMeNavigationItem"]',
]

_SAP_COOKIE_PATTERNS: List[str] = [
    'MYSAPSSO2',
    'sap-usercontext',
    'SAP_SESSIONID',
    'JSESSIONID',
    'XSRF-TOKEN',
    'sap-login-XSRF',
    'ESTSAUTH',
    'ESTSAUTHPERSISTENT',
    'IDP_SESSION',
    '__Host-sap',
    'SAML2',
]

# Default SAP IdP login URL
_SAP_DEFAULT_IDP = "https://accounts.sap.com/saml2/idp/sso"


# ---------------------------------------------------------------------------
# SAP Auth Handler
# ---------------------------------------------------------------------------

class SAPAuthHandler(BaseAuthHandler):
    """SAP portal authentication handler.

    Implements the full SAML SSO flow for SAP portals:
    1. Navigate to portal URL → triggers SAML redirect to IdP
    2. Fill credentials on IdP page (accounts.sap.com, Azure AD, etc.)
    3. Wait for SAML assertion POST-back to portal
    4. Return authenticated context with session cookies

    Auto-detects SAP portals from URL patterns (no user config needed).
    """

    @property
    def portal_name(self) -> str:
        return "SAP"

    @property
    def env_var_prefixes(self) -> List[str]:
        return ["SAP", "CRAWLER"]

    # ── Detection ─────────────────────────────────────────────────

    def detect(self, url: str) -> bool:
        """Return True if the URL belongs to an SAP portal."""
        url_lower = url.lower()
        parsed = urlparse(url_lower)
        domain = parsed.netloc
        path = parsed.path

        # Domain patterns
        for pattern in _SAP_DOMAIN_PATTERNS:
            if pattern in domain:
                return True

        # Path patterns
        for pattern in _SAP_PATH_PATTERNS:
            if pattern.lower() in path:
                return True

        return False

    def get_login_url(self, portal_url: str) -> str:
        """Derive the SAP IdP login URL from the portal URL.

        For SAP portals, the login flow starts at the portal itself
        (which triggers a SAML redirect). The actual IdP URL is
        ``accounts.sap.com`` by default.
        """
        # For SAP portals, we start at the portal URL directly
        # (the SAML redirect happens automatically)
        # Return the default IdP as a reference
        return _SAP_DEFAULT_IDP

    # ── Login flow ────────────────────────────────────────────────

    async def login(self, page: Page, creds: Credentials) -> bool:
        """Execute the full SAP SAML login flow.

        For cross-domain SAML (e.g. portal on me.sap.com, IdP on accounts.sap.com):
            1. Navigate to portal URL → triggers SAML redirect to IdP
            2. Browser follows redirect chain to IdP (accounts.sap.com)
            3. Fill credentials on the IdP page
            4. Submit
            5. IdP redirects back to portal with SAML assertion
            6. Session cookies are set on BOTH domains

        Args:
            page:  A fresh Playwright page.
            creds: Resolved credentials with username and password.

        Returns:
            True if login was successful, False otherwise.
        """
        portal_url = self._portal_url or ""
        entry_url = portal_url if portal_url else self.get_login_url(portal_url)

        logger.info(f"[SAP] Starting SAML login flow")
        logger.info(f"[SAP] Entry URL: {entry_url[:100]}")

        # ── Step 1: Navigate to entry point ───────────────────────
        try:
            resp = await page.goto(
                entry_url, timeout=60_000, wait_until="load"
            )
        except PlaywrightTimeout:
            logger.error("[SAP] Timeout navigating to portal")
            return False

        if resp and resp.status >= 400:
            logger.warning(f"[SAP] Portal returned HTTP {resp.status} — trying fallback")
            fallback = self._get_fallback_url(entry_url)
            if fallback and fallback != entry_url:
                try:
                    resp = await page.goto(fallback, timeout=60_000, wait_until="load")
                except PlaywrightTimeout:
                    logger.error("[SAP] Timeout on fallback URL")
                    return False
                if resp and resp.status >= 400:
                    logger.error(f"[SAP] Fallback also returned HTTP {resp.status}")
                    return False
            else:
                return False

        # Wait for redirects to settle
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeout:
            pass
        await self._wait_for_redirects(page, timeout_s=20.0)

        logger.info(f"[SAP] After redirects: {page.url[:120]}")

        # ── Step 2: Already authenticated? ────────────────────────
        if await self._is_already_authenticated(page):
            logger.info("[SAP] Already authenticated (valid session)")
            return True

        # ── Step 3: Fill username ─────────────────────────────────
        username_sel = await self._find_field(
            page, _SAP_USERNAME_SELECTORS, "username"
        )
        if not username_sel:
            logger.error(f"[SAP] Cannot find username field. URL: {page.url[:120]}")
            await self._screenshot(page, "sap_no_username")
            return False

        await self._safe_fill(page, username_sel, creds.username)
        logger.info("[SAP] Username filled")

        # ── Step 4: Multi-step check ─────────────────────────────
        password_sel = await self._find_field(
            page, _SAP_PASSWORD_SELECTORS, "password"
        )
        if not password_sel:
            next_clicked = await self._click_next_step(page)
            if next_clicked:
                await asyncio.sleep(2.0)
                password_sel = await self._find_field(
                    page, _SAP_PASSWORD_SELECTORS, "password", timeout_ms=10_000
                )

        if not password_sel:
            logger.error("[SAP] Cannot find password field")
            await self._screenshot(page, "sap_no_password")
            return False

        # ── Step 5: Fill password ─────────────────────────────────
        await self._safe_fill(page, password_sel, creds.password)
        logger.info("[SAP] Password filled")

        # ── Step 6: Submit ────────────────────────────────────────
        submit_sel = await self._find_field(
            page, _SAP_SUBMIT_SELECTORS, "submit button"
        )
        if not submit_sel:
            logger.info("[SAP] No submit button — pressing Enter")
            try:
                await page.press(password_sel, "Enter", no_wait_after=True)
            except PlaywrightTimeout:
                pass
        else:
            try:
                await page.click(submit_sel, timeout=10_000, no_wait_after=True)
            except PlaywrightTimeout:
                pass
            logger.info("[SAP] Submit clicked")

        # ── Step 7: Wait for SAML redirect chain ─────────────────
        logger.info("[SAP] Waiting for SAML redirect chain...")
        success = await self._wait_for_saml_success(page)

        if success:
            logger.info("[SAP] Login successful")
        else:
            logger.error("[SAP] Login verification failed")
            await self._screenshot(page, "sap_login_failed")

        return success

    # ── Session expiry ────────────────────────────────────────────

    async def detect_expired(
        self, page: Page, intended_url: str = ""
    ) -> bool:
        """Check whether the current page indicates SAP session expiry.

        Checks:
            1. Redirected to a login / IdP page (not intentionally)
            2. Login form is present on a non-login page
            3. SAP-specific session expiry indicators

        Returns:
            True if re-login is needed.
        """
        current_url = page.url.lower()
        intended_lower = intended_url.lower() if intended_url else ""

        # Skip detection if we intentionally navigated to a login page
        if intended_lower:
            for indicator in _SAP_LOGIN_URL_PATTERNS:
                if indicator.lower() in intended_lower:
                    return False

        # Check redirect to login / IdP page
        for indicator in _SAP_LOGIN_URL_PATTERNS:
            ind_lower = indicator.lower()
            if ind_lower in current_url and ind_lower not in intended_lower:
                logger.warning(f"[SAP] Session expired — redirected to '{indicator}'")
                return True

        # Check for login form presence
        login_form_selectors = [
            'input[type="password"]',
            '#j_username', 'input[name="j_username"]',
            'form[action*="login"]', 'form[action*="saml"]',
        ]
        for selector in login_form_selectors:
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    logger.warning(f"[SAP] Session expired — login form detected: {selector}")
                    return True
            except Exception:
                continue

        # SAP-specific expiry indicators
        try:
            from ..sap_extractor import detect_sap_session_expiry
            if await detect_sap_session_expiry(page, intended_url=intended_url):
                return True
        except ImportError:
            pass

        return False

    # ── Content helpers (portal-specific) ─────────────────────────

    def get_content_selectors(self, url: str) -> List[str]:
        """Return SAP-specific CSS selectors for content extraction."""
        return [
            '.sapMPage',
            '.sapUiBody main',
            '.sapFDynamicPageContent',
            '[data-sap-ui-area="content"]',
            '.sapMPageContent',
            '.sapUshellCloneArea',
            '.sapMShellContent',
            '#content',
            'main',
            '[role="main"]',
        ]

    def get_overlay_selectors(self) -> List[Tuple[str, str]]:
        """Return SAP-specific overlay dismissal selectors."""
        return [
            # TrustArc cookie banner
            ('a.call[onclick*="truste"]', 'click'),
            ('.truste_overlay', 'remove'),
            ('#truste-consent-track', 'remove'),
            # SAP help tour overlay
            ('.help4-tour-overlay', 'remove'),
            ('[class*="help4"]', 'remove'),
            # SAP cookie consent
            ('.sapMMessageToast', 'remove'),
            # Generic cookie/consent overlays
            ('button:has-text("Accept")', 'click'),
            ('button:has-text("Accept All")', 'click'),
        ]

    async def post_login_setup(self, page: Page) -> None:
        """Dismiss SAP-specific overlays after login."""
        for selector, action in self.get_overlay_selectors():
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    if action == 'click':
                        await el.click(timeout=3000)
                    elif action == 'remove':
                        await el.evaluate("el => el.remove()")
                    elif action == 'hide':
                        await el.evaluate("el => el.style.display = 'none'")
                    logger.debug(f"[SAP] Dismissed overlay: {selector}")
                    await asyncio.sleep(0.3)
            except Exception:
                continue

    # ── Portal URL management ─────────────────────────────────────

    _portal_url: str = ""

    def set_portal_url(self, url: str) -> None:
        """Set the portal URL for cross-domain SAML login."""
        self._portal_url = url

    # ── Internal helpers ──────────────────────────────────────────

    async def _find_field(
        self,
        page: Page,
        selectors: List[str],
        field_name: str,
        timeout_ms: int = 5000,
    ) -> Optional[str]:
        """Find a form field by trying selectors in order."""
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    visible = await el.is_visible()
                    if not visible:
                        continue
                    if field_name in ("username", "password"):
                        tag = await el.evaluate("el => el.tagName.toLowerCase()")
                        if tag not in ("input", "textarea", "select"):
                            editable = await el.evaluate("el => el.isContentEditable")
                            if not editable:
                                continue
                    logger.debug(f"[SAP] Auto-detected {field_name}: {sel}")
                    return sel
            except Exception:
                continue

        # Last resort: wait for first selector
        try:
            primary = selectors[0] if selectors else None
            if primary:
                await page.wait_for_selector(primary, timeout=timeout_ms, state="visible")
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
        """Click 'Next'/'Continue' for multi-step login."""
        next_selectors = [
            '#idSIButton9',
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
                    logger.info(f"[SAP] Clicked multi-step: {sel}")
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_redirects(
        self, page: Page, timeout_s: float = 20.0
    ) -> None:
        """Wait for SAML redirect chains to settle (URL stable for 2s)."""
        deadline = _time.monotonic() + timeout_s
        prev_url = page.url
        stable_count = 0
        while _time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            cur_url = page.url
            if cur_url == prev_url:
                stable_count += 1
                if stable_count >= 4:
                    return
            else:
                stable_count = 0
                prev_url = cur_url

    async def _is_already_authenticated(self, page: Page) -> bool:
        """Check if we're already on an authenticated SAP page."""
        current_url = page.url.lower()

        for pattern in _SAP_SUCCESS_URL_PATTERNS:
            if pattern.lower() in current_url:
                logger.info(f"[SAP] Already authenticated: {pattern}")
                return True

        for sel in _SAP_SUCCESS_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    logger.info(f"[SAP] Authenticated UI found: {sel}")
                    return True
            except Exception:
                continue

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
            is_login_url = any(p in current_url for p in _SAP_LOGIN_URL_PATTERNS)
            if not is_login_url:
                logger.info("[SAP] No login form + not on login URL — likely authenticated")
                return True

        return False

    async def _wait_for_saml_success(self, page: Page) -> bool:
        """Wait for SAML login to complete and verify success."""
        # Wait for navigation after submit
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeout:
            pass

        # Wait for redirect chain to settle
        await self._wait_for_redirects(page, timeout_s=30.0)

        # Extra settle time
        await asyncio.sleep(3.0)

        # Diagnostics
        current_url = page.url
        try:
            page_title = await page.title()
        except Exception:
            page_title = "<unknown>"
        logger.info(f"[SAP] Post-login URL: {current_url[:120]}")
        logger.info(f"[SAP] Post-login title: {page_title[:80]}")

        # Check for errors
        for sel in _SAP_ERROR_SELECTORS:
            try:
                err_el = await page.query_selector(sel)
                if err_el and await err_el.is_visible():
                    err_text = (await err_el.inner_text()).strip()[:200]
                    if err_text:
                        logger.error(f"[SAP] Login error ({sel}): {err_text}")
                        return False
            except Exception:
                continue

        # Check authenticated page
        if await self._is_already_authenticated(page):
            return True

        # Check SAP auth cookies
        try:
            cookies = await page.context.cookies()
            auth_cookies = []
            for cookie in cookies:
                name = cookie.get("name", "")
                for pat in _SAP_COOKIE_PATTERNS:
                    if pat.lower() in name.lower():
                        auth_cookies.append(name)
                        break
            if auth_cookies:
                logger.info(f"[SAP] Auth cookies found: {auth_cookies[:5]}")
                return True
        except Exception:
            pass

        # Heuristic: left login page + no form
        current_lower = page.url.lower()
        on_login = any(p in current_lower for p in _SAP_LOGIN_URL_PATTERNS)
        has_pw = False
        try:
            pw = await page.query_selector('input[type="password"]')
            if pw and await pw.is_visible():
                has_pw = True
        except Exception:
            pass

        if not on_login and not has_pw:
            logger.info("[SAP] Heuristic: left login page — success")
            return True

        logger.warning(f"[SAP] Could not verify login (url={current_lower[:100]})")
        return False

    def _get_fallback_url(self, original_url: str) -> Optional[str]:
        """Derive a fallback from an IdP SSO endpoint URL."""
        parsed = urlparse(original_url)
        if not parsed.scheme or not parsed.netloc:
            return None

        idp_fragments = [
            '/saml2/idp/', '/saml/idp/', '/idp/sso',
            '/saml2/sso', '/saml/sso',
        ]
        path_lower = parsed.path.lower()
        for frag in idp_fragments:
            if frag in path_lower:
                return f"{parsed.scheme}://{parsed.netloc}"

        if parsed.path and parsed.path != '/':
            return f"{parsed.scheme}://{parsed.netloc}"
        return None

    async def _screenshot(self, page: Page, name: str) -> None:
        """Take a debug screenshot on failure."""
        try:
            path = f"debug_{name}.png"
            await page.screenshot(path=path, full_page=False)
            logger.info(f"[SAP] Debug screenshot saved: {path}")
        except Exception as e:
            logger.debug(f"[SAP] Screenshot failed: {e}")

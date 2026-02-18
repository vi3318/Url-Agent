"""
Login Manager
=============
Playwright-based login flow for enterprise portals.

Handles:
    - Standard username/password forms
    - SAML / SSO redirect chains (Azure AD, Okta, ADFS, PingFederate)
    - Hidden CSRF tokens (auto-submitted with the form)
    - JS-heavy login pages (delays, dynamic rendering)
    - Multi-step login flows (username first, then password)
    - Configurable field selectors and success detection

Enterprise portal compatibility:
    - SAP portal / Fiori launchpad (SAML to IdP)
    - ServiceNow instances (/login.do, /login_with_sso.do)
    - Microsoft SSO / Azure AD (login.microsoftonline.com)
    - Oracle Cloud (IDCS login)
    - Generic username+password forms

Security:
    - Credentials are never logged or printed.
    - Only the login URL and success/failure status appear in logs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auto-detection selector banks
# ---------------------------------------------------------------------------

# Username / email field selectors (tried in order)
_USERNAME_SELECTORS: List[str] = [
    # Explicit id / name patterns
    '#user_login',                     # WordPress
    '#user_name', '#username', '#userName', '#user',
    '#email', '#Email',
    'input#login',                     # Only match <input id="login">, not <div id="login">
    '#Login',
    'input[name="user_name"]', 'input[name="username"]',
    'input[name="email"]', 'input[name="login"]',
    'input[name="log"]',              # WordPress
    'input[name="loginfmt"]',          # Microsoft / Azure AD
    'input[name="j_username"]',        # SAP / Java EE SAML
    'input[name="sysparm_user"]',      # ServiceNow
    'input[name="ssousername"]',       # Oracle IDCS
    'input[name="userid"]',            # Generic
    # Type-based fallbacks
    'input[type="email"]',
    'input[type="text"][autocomplete="username"]',
    'input[type="text"][autocomplete="email"]',
    'input[type="text"]:not([type="hidden"]):not([style*="display:none"])',
]

# Password field selectors
_PASSWORD_SELECTORS: List[str] = [
    '#user_pass',                      # WordPress
    '#password', '#Password', '#pwd', '#Passwd',
    'input[name="password"]', 'input[name="pwd"]',
    'input[name="Passwd"]',            # Google
    'input[name="passwd"]',            # Microsoft
    'input[name="j_password"]',        # SAP / Java EE SAML
    'input[name="sysparm_password"]',  # ServiceNow
    'input[name="ssopassword"]',       # Oracle IDCS
    'input[type="password"]',
]

# Submit button selectors
_SUBMIT_SELECTORS: List[str] = [
    # Explicit id / class patterns
    '#wp-submit',                      # WordPress
    '#login_button', '#loginButton', '#submitButton',
    '#sysverb_login',                  # ServiceNow
    '#idSIButton9',                    # Microsoft "Sign in"
    '#btn-login', '#btnLogin',
    'input[type="submit"]',
    'button[type="submit"]',
    # Text-based (Playwright :has-text)
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
    'button:has-text("Login")',
    'button:has-text("Submit")',
    'button:has-text("Continue")',
    'button:has-text("Next")',         # Microsoft — first step
    'input[value="Sign in"]',
    'input[value="Log in"]',
    'input[value="Login"]',
    'input[value="Sign In"]',
    'input[value="Next"]',
    'input[value="Log In"]',           # WordPress
]


class LoginManager:
    """Performs Playwright-based login for enterprise portals.

    Usage::

        from crawler.auth.session_manager import AuthConfig
        from crawler.auth.login_manager import LoginManager

        cfg = AuthConfig(
            login_url="https://portal.example.com/login",
            username="admin",
            password="secret",
        )
        manager = LoginManager(cfg)
        page = await context.new_page()
        success = await manager.login(page)
    """

    def __init__(self, config):
        """
        Args:
            config: An ``AuthConfig`` instance with credentials and selectors.
        """
        self.config = config

    async def login(self, page: Page) -> bool:
        """Execute the full login flow.

        Steps:
            1. Navigate to ``login_url``
            2. Wait for login form to render (JS-heavy pages)
            3. Fill username field
            4. Fill password field (may be on a second step)
            5. Click submit
            6. Wait for post-login redirect / success indicator
            7. Verify authentication succeeded

        Returns:
            True if login was successful, False otherwise.
        """
        login_url = self.config.login_url
        logger.info(f"[AUTH] Navigating to login page: {login_url[:80]}")

        # ── Step 1: Navigate to login URL ────────────────────────────
        try:
            resp = await page.goto(
                login_url,
                timeout=self.config.login_timeout_ms,  # 60s — login pages can be slow
                wait_until="load",
            )
        except PlaywrightTimeout:
            logger.error("[AUTH] Timeout navigating to login page")
            return False

        if resp and resp.status >= 400:
            logger.error(f"[AUTH] Login page returned HTTP {resp.status}")
            return False

        # Wait for network + JS rendering
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeout:
            pass

        # Extra wait for JS-heavy login forms (SAML redirect, GWT, etc.)
        if self.config.pre_login_wait_ms > 0:
            await asyncio.sleep(self.config.pre_login_wait_ms / 1000)

        # If the login URL triggered SAML/SSO redirect, wait for it to settle
        if self.config.expect_redirects:
            await self._wait_for_redirects(page)

        # ── Step 2: Detect and fill username ─────────────────────────
        username_sel = await self._find_field(
            page,
            self.config.username_selector,
            _USERNAME_SELECTORS,
            "username",
        )
        if not username_sel:
            logger.error("[AUTH] Could not find username field")
            return False

        await self._safe_fill(page, username_sel, self.config.username)
        logger.info("[AUTH] Username filled")

        # ── Step 3: Check for multi-step login ───────────────────────
        # Some portals (Microsoft, Okta) show username first, then password
        # after clicking "Next".
        password_sel = await self._find_field(
            page,
            self.config.password_selector,
            _PASSWORD_SELECTORS,
            "password",
        )

        if not password_sel:
            # Password field not visible yet — try clicking "Next" first
            next_clicked = await self._click_next_step(page)
            if next_clicked:
                await asyncio.sleep(1.5)
                # Wait for password field to appear
                password_sel = await self._find_field(
                    page,
                    self.config.password_selector,
                    _PASSWORD_SELECTORS,
                    "password",
                    timeout_ms=8000,
                )

        if not password_sel:
            logger.error("[AUTH] Could not find password field")
            return False

        # ── Step 4: Fill password ────────────────────────────────────
        await self._safe_fill(page, password_sel, self.config.password)
        logger.info("[AUTH] Password filled")

        # ── Step 5: Submit ───────────────────────────────────────────
        submit_sel = await self._find_field(
            page,
            self.config.submit_selector,
            _SUBMIT_SELECTORS,
            "submit button",
        )
        if not submit_sel:
            # Fallback: press Enter on the password field
            logger.info("[AUTH] No submit button found — pressing Enter")
            try:
                await page.press(password_sel, "Enter", no_wait_after=True)
            except PlaywrightTimeout:
                pass
        else:
            # no_wait_after=True: the click triggers navigation — we
            # handle the navigation wait ourselves in _wait_for_login_success
            try:
                await page.click(submit_sel, timeout=10000, no_wait_after=True)
            except PlaywrightTimeout:
                pass
            logger.info("[AUTH] Submit clicked")

        # ── Step 6: Wait for post-login navigation ───────────────────
        success = await self._wait_for_login_success(page)

        if success:
            logger.info("[AUTH] ✅ Login successful")
        else:
            logger.error("[AUTH] ❌ Login verification failed")

        return success

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _find_field(
        self,
        page: Page,
        explicit_selector: str,
        fallback_selectors: List[str],
        field_name: str,
        timeout_ms: int = 5000,
    ) -> Optional[str]:
        """Find a form field using explicit selector or auto-detection.

        Args:
            explicit_selector: User-configured CSS selector. If set, use only this.
            fallback_selectors: List of selectors to try in order.
            field_name: Human-readable name for logging.
            timeout_ms: How long to wait for the field to appear.

        Returns:
            The CSS selector that matched, or None.
        """
        if explicit_selector:
            try:
                el = await page.wait_for_selector(
                    explicit_selector, timeout=timeout_ms, state="visible"
                )
                if el:
                    return explicit_selector
            except PlaywrightTimeout:
                logger.warning(
                    f"[AUTH] Configured {field_name} selector not found: "
                    f"{explicit_selector}"
                )
            return None

        # Auto-detect
        for sel in fallback_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    visible = await el.is_visible()
                    if not visible:
                        continue
                    # Safety: for username/password fields, verify the element
                    # is actually fillable (input/textarea/select/contenteditable)
                    # not a container div that happens to match the selector.
                    if field_name in ("username", "password"):
                        tag = await el.evaluate("el => el.tagName.toLowerCase()")
                        if tag not in ("input", "textarea", "select"):
                            editable = await el.evaluate(
                                "el => el.isContentEditable"
                            )
                            if not editable:
                                logger.debug(
                                    f"[AUTH] Skipping non-input {field_name} "
                                    f"match: <{tag}> via {sel}"
                                )
                                continue
                    logger.debug(f"[AUTH] Auto-detected {field_name}: {sel}")
                    return sel
            except Exception:
                continue

        # Last resort: wait briefly for any password/text input to appear
        try:
            primary = fallback_selectors[0] if fallback_selectors else None
            if primary:
                await page.wait_for_selector(primary, timeout=timeout_ms, state="visible")
                return primary
        except PlaywrightTimeout:
            pass

        return None

    async def _safe_fill(self, page: Page, selector: str, value: str) -> None:
        """Fill a field safely — click to focus, clear, then type.

        Some enterprise login forms (SAP, Oracle) have custom event
        handlers that require actual keystrokes rather than just setting
        the value attribute.
        """
        try:
            await page.click(selector, timeout=3000)
            await asyncio.sleep(0.2)
        except Exception:
            pass

        try:
            # Triple-click to select all, then type over
            await page.click(selector, click_count=3, timeout=2000)
            await page.keyboard.press("Backspace")
        except Exception:
            pass

        await page.fill(selector, value)

    async def _click_next_step(self, page: Page) -> bool:
        """Click a 'Next' button for multi-step login flows (Microsoft, Okta).

        Returns True if a button was found and clicked.
        """
        next_selectors = [
            '#idSIButton9',             # Microsoft "Next"
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
                    logger.info(f"[AUTH] Clicked 'Next' step: {sel}")
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_redirects(self, page: Page, timeout_s: float = 10.0) -> None:
        """Wait for SAML / SSO redirect chains to complete.

        Polls until the URL stabilises for 2 seconds or timeout is reached.
        """
        import time
        deadline = time.monotonic() + timeout_s
        prev_url = page.url
        stable_count = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            cur_url = page.url
            if cur_url == prev_url:
                stable_count += 1
                if stable_count >= 4:  # 2 seconds of stability
                    return
            else:
                stable_count = 0
                prev_url = cur_url

    async def _wait_for_login_success(self, page: Page) -> bool:
        """Wait for post-login success indicators.

        Checks (in priority order):
            1. ``failure_selector`` / built-in error detection → fail fast
            2. ``success_url_contains`` — URL changes to contain pattern
            3. ``success_selector`` — a known authenticated element appears
            4. Cookie-based detection — auth cookies set after login
            5. Heuristic: login form disappeared + URL changed

        Returns:
            True if login verified successful.
        """
        timeout_ms = self.config.login_timeout_ms
        post_wait_ms = self.config.post_login_wait_ms

        # ── Wait for navigation / redirect ──────────────────────────
        try:
            await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 30_000))
        except PlaywrightTimeout:
            pass

        # Extra settle time for SAML redirects
        if post_wait_ms > 0:
            await asyncio.sleep(post_wait_ms / 1000)

        # Wait for any remaining redirects
        if self.config.expect_redirects:
            await self._wait_for_redirects(page, timeout_s=15.0)

        # ── Diagnostics ─────────────────────────────────────────────
        current_url = page.url
        try:
            page_title = await page.title()
        except Exception:
            page_title = "<unknown>"
        logger.info(f"[AUTH] Post-login URL: {current_url[:120]}")
        logger.info(f"[AUTH] Post-login page title: {page_title[:80]}")

        # ── Check configured failure_selector ───────────────────────
        if self.config.failure_selector:
            try:
                err_el = await page.query_selector(self.config.failure_selector)
                if err_el and await err_el.is_visible():
                    err_text = (await err_el.inner_text()).strip()[:200]
                    logger.error(f"[AUTH] Login error detected: {err_text}")
                    return False
            except Exception:
                pass

        # ── Built-in login error detection ──────────────────────────
        # Check for common error indicators across enterprise login pages
        _ERROR_SELECTORS = [
            '#login_error',                     # WordPress
            '.login-error', '.login_error',
            '#error-message', '.error-message',
            '.message.error',                   # Generic
            '.alert-danger', '.alert-error',
            '#usernameError', '#passwordError',  # Microsoft / Azure AD
            '.error_message',                    # SAP
            '[data-testid="error-message"]',
            '.notice.error',                     # WordPress (alternate)
            '#notice', '.notice-error',
        ]
        for sel in _ERROR_SELECTORS:
            try:
                err_el = await page.query_selector(sel)
                if err_el and await err_el.is_visible():
                    err_text = (await err_el.inner_text()).strip()[:200]
                    if err_text:
                        logger.error(
                            f"[AUTH] Login error on page ({sel}): {err_text}"
                        )
                        return False
            except Exception:
                continue

        # ── Check success_url_contains ──────────────────────────────
        if self.config.success_url_contains:
            current_url = page.url
            if self.config.success_url_contains.lower() in current_url.lower():
                logger.info(
                    f"[AUTH] URL contains '{self.config.success_url_contains}' — "
                    f"login verified"
                )
                return True
            # Not in URL yet — wait a bit more
            try:
                await page.wait_for_url(
                    f"**/*{self.config.success_url_contains}*",
                    timeout=timeout_ms,
                )
                return True
            except PlaywrightTimeout:
                logger.warning(
                    f"[AUTH] URL never contained '{self.config.success_url_contains}'"
                )

        # ── Check success_selector ──────────────────────────────────
        if self.config.success_selector:
            try:
                await page.wait_for_selector(
                    self.config.success_selector,
                    timeout=min(timeout_ms, 15_000),
                    state="visible",
                )
                logger.info(
                    f"[AUTH] Success element appeared: {self.config.success_selector}"
                )
                return True
            except PlaywrightTimeout:
                logger.warning(
                    f"[AUTH] Success selector not found: {self.config.success_selector}"
                )

        # ── Cookie-based success detection ──────────────────────────
        # Many portals set auth cookies after login. Check for common ones.
        try:
            cookies = await page.context.cookies()
            _AUTH_COOKIE_PATTERNS = [
                'wordpress_logged_in',   # WordPress
                'wordpress_sec_',        # WordPress secure
                'JSESSIONID',            # SAP / Java
                'glide_user_route',      # ServiceNow
                'glide_session_store',   # ServiceNow
                'ESTSAUTH',              # Microsoft / Azure AD
                'ESTSAUTHPERSISTENT',    # Microsoft
                '.AspNet.Cookies',       # .NET
                'connect.sid',           # Express / Node.js
                'session_id',            # Generic
                'auth_token',            # Generic
                'id_token',              # OIDC
                'access_token',          # OAuth
            ]
            auth_cookies_found = []
            for cookie in cookies:
                cookie_name = cookie.get("name", "")
                for pat in _AUTH_COOKIE_PATTERNS:
                    if pat.lower() in cookie_name.lower():
                        auth_cookies_found.append(cookie_name)
                        break
            if auth_cookies_found:
                logger.info(
                    f"[AUTH] Auth cookies detected: {auth_cookies_found[:5]} — "
                    f"login verified via cookies"
                )
                return True
        except Exception:
            pass

        # ── Heuristic: login form gone + URL changed ────────────────
        login_url_lower = self.config.login_url.lower()
        current_url_lower = page.url.lower()

        # URL changed away from login page?
        url_changed = current_url_lower != login_url_lower

        # Login form gone?
        login_form_gone = True
        for sel in ['input[type="password"]', '#loginForm', 'form[action*="login"]']:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    login_form_gone = False
                    break
            except Exception:
                continue

        # No login indicators in URL?
        no_login_in_url = not any(
            ind in current_url_lower
            for ind in ['/login', '/signin', '/sso/', '/auth/']
        )

        # Log diagnostic state for debugging
        logger.debug(
            f"[AUTH] Heuristic state: url_changed={url_changed}, "
            f"form_gone={login_form_gone}, no_login_url={no_login_in_url}, "
            f"url={current_url_lower[:100]}"
        )

        if url_changed and login_form_gone and no_login_in_url:
            logger.info("[AUTH] Heuristic: URL changed + login form gone — success")
            return True

        if login_form_gone and no_login_in_url:
            logger.info("[AUTH] Heuristic: login form gone — likely success")
            return True

        # ── Final diagnostics ───────────────────────────────────────
        logger.warning(
            f"[AUTH] Could not verify login success "
            f"(url_changed={url_changed}, form_gone={login_form_gone}, "
            f"no_login_url={no_login_in_url})"
        )
        return False

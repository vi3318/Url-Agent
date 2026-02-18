"""
Session Bootstrap Utility
=========================
Launches a headed (visible) browser for manual login, including MFA.

Use cases:
    - SAP portals with TOTP / SMS-based MFA
    - Portals with CAPTCHA that can't be automated
    - First-time session establishment before headless crawling

Workflow:
    1. Launch headed Chromium browser
    2. Navigate to the portal URL
    3. User manually logs in (including MFA, CAPTCHA, etc.)
    4. Script waits for user to press Enter in the terminal
    5. Saves ``storage_state`` (cookies + localStorage) to ``auth_state.json``
    6. Closes browser

The saved session can then be loaded by the headless crawler via
``--auth-state-file auth_state.json`` (no ``--force-login``).

Usage::

    # From command line:
    python -m crawler --bootstrap https://myportal.sap.com/fiori

    # Programmatic:
    from crawler.auth.session_bootstrap import bootstrap_session
    asyncio.run(bootstrap_session(
        portal_url="https://myportal.sap.com/fiori",
        output_path="auth_state.json",
    ))
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


async def bootstrap_session(
    portal_url: str,
    output_path: str = "auth_state.json",
    timeout_minutes: int = 10,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
) -> bool:
    """Launch a headed browser for manual login and save the session.

    Args:
        portal_url: URL of the portal / login page.
        output_path: Path to save the ``storage_state`` JSON.
        timeout_minutes: Maximum wait time for user login (minutes).
        viewport_width: Browser window width.
        viewport_height: Browser window height.

    Returns:
        True if session was saved successfully, False otherwise.
    """
    print("\n" + "=" * 60)
    print("  SESSION BOOTSTRAP MODE")
    print("=" * 60)
    print(f"  Portal URL:   {portal_url}")
    print(f"  Output file:  {output_path}")
    print(f"  Timeout:      {timeout_minutes} minutes")
    print("=" * 60)
    print()
    print("  A browser window will open.")
    print("  Please log in manually (including MFA if required).")
    print("  Once you are fully logged in, press ENTER in this")
    print("  terminal to save the session.")
    print()
    print("=" * 60)

    pw = await async_playwright().start()
    browser = None
    context = None

    try:
        browser = await pw.chromium.launch(
            headless=False,  # headed — user needs to see and interact
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--start-maximized',
            ],
        )

        context = await browser.new_context(
            viewport={"width": viewport_width, "height": viewport_height},
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()
        print(f"\n  Navigating to: {portal_url[:100]}")
        print()

        try:
            await page.goto(portal_url, wait_until="load", timeout=60_000)
        except Exception as e:
            logger.warning(f"[BOOTSTRAP] Initial navigation issue: {e}")

        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        print(f"  Browser opened. Current URL: {page.url[:100]}")
        print()
        print("  ➡  Log in manually now.")
        print("  ➡  When fully logged in, come back here and press ENTER.")
        print()

        # Wait for user to press Enter (with timeout)
        loop = asyncio.get_event_loop()
        try:
            user_input = await asyncio.wait_for(
                loop.run_in_executor(None, _wait_for_enter),
                timeout=timeout_minutes * 60,
            )
        except asyncio.TimeoutError:
            print(f"\n  ⏰ Timeout ({timeout_minutes} min) — saving current state anyway.")

        # Check what we have
        current_url = page.url
        try:
            title = await page.title()
        except Exception:
            title = "<unknown>"

        print(f"\n  Current URL:   {current_url[:100]}")
        print(f"  Page title:    {title[:80]}")

        # Save storage state
        state_path = Path(output_path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(state_path))

        # Verify saved state
        import json
        data = json.loads(state_path.read_text(encoding="utf-8"))
        cookies = data.get("cookies", [])
        origins = data.get("origins", [])

        print(f"\n  Session saved: {state_path}")
        print(f"  Cookies:       {len(cookies)}")
        print(f"  Origins:       {len(origins)}")

        if cookies:
            # Show cookie domains (not values — security)
            domains = set(c.get("domain", "") for c in cookies)
            print(f"  Domains:       {', '.join(sorted(domains)[:10])}")
            print(f"\n  ✅ Session bootstrap complete!")
            print(f"\n  To use this session for crawling:")
            print(f"    python -m crawler {portal_url} \\")
            print(f"        --auth-state-file {output_path}")
            print()
            return True
        else:
            print(f"\n  ⚠  No cookies saved — login may not have completed.")
            print(f"  Try again with a longer timeout.\n")
            return False

    except KeyboardInterrupt:
        print("\n  Cancelled by user.")
        return False
    except Exception as e:
        logger.error(f"[BOOTSTRAP] Error: {e}")
        print(f"\n  ❌ Bootstrap failed: {e}")
        return False
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await pw.stop()
        except Exception:
            pass


def _wait_for_enter() -> str:
    """Block until the user presses Enter (runs in executor)."""
    try:
        return input("  Press ENTER when login is complete → ")
    except (EOFError, KeyboardInterrupt):
        return ""


def run_bootstrap_cli(portal_url: str, output_path: str = "auth_state.json"):
    """CLI entry point for session bootstrap."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(bootstrap_session(
        portal_url=portal_url,
        output_path=output_path,
    ))

"""Diagnostic script: inspect me.sap.com/home page structure."""
import asyncio
from playwright.async_api import async_playwright


async def diagnose():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)

    # Load saved session from accounts.sap.com
    ctx = await browser.new_context(storage_state="auth_state.json")
    page = await ctx.new_page()

    print("Navigating to me.sap.com/home...")
    resp = await page.goto(
        "https://me.sap.com/home", wait_until="load", timeout=30000
    )
    print(f"Status: {resp.status}")
    print(f"URL after goto: {page.url}")

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(5)

    print(f"Final URL: {page.url}")
    title = await page.title()
    print(f"Title: {title}")

    # Check for login redirect
    has_pw = await page.evaluate(
        "() => !!document.querySelector('input[type=password]')"
    )
    print(f"Has password field (login redirect?): {has_pw}")

    # Body text
    body_text = await page.evaluate(
        "() => (document.body?.innerText || '').substring(0, 1000)"
    )
    print(f"\nBody text (first 1000 chars):\n{body_text[:1000]}")

    wc = await page.evaluate(
        "() => (document.body?.innerText || '').trim().split(/\\s+/).length"
    )
    print(f"\nBody word count: {wc}")

    # Links
    links = await page.evaluate("""() => {
        const results = [];
        document.querySelectorAll('a[href]').forEach(a => {
            const t = (a.textContent || '').trim().substring(0, 50);
            const h = a.getAttribute('href') || '';
            if (t && h) results.push(t + ' -> ' + h.substring(0, 80));
        });
        return results;
    }""")
    print(f"\nLinks found: {len(links)}")
    for l in links[:25]:
        print(f"  {l}")

    # Shadow DOM
    shadow_hosts = await page.evaluate("""() => {
        let c = 0;
        document.querySelectorAll('*').forEach(el => { if(el.shadowRoot) c++; });
        return c;
    }""")
    total_els = await page.evaluate(
        "() => document.querySelectorAll('*').length"
    )
    print(f"\nTotal DOM elements: {total_els}")
    print(f"Shadow DOM hosts: {shadow_hosts}")

    # Content selectors check
    selectors_to_check = [
        "main", "article", "#content", ".content",
        "[role='main']", ".sapMPage", ".sapMShell",
        ".sapUiBody", "[data-sap-ui-area]",
        ".sapTntSideNavigation", ".sapMNav",
        "aside", "nav", ".sidebar",
    ]
    print("\nSelector check:")
    for sel in selectors_to_check:
        try:
            el = await page.query_selector(sel)
            if el:
                text_len = await el.evaluate(
                    "el => (el.innerText || '').trim().length"
                )
                print(f"  {sel}: {text_len} chars")
            else:
                print(f"  {sel}: NOT FOUND")
        except Exception as e:
            print(f"  {sel}: ERROR {e}")

    # Check iframes
    frames = page.frames
    print(f"\nFrames: {len(frames)}")
    for f in frames[:5]:
        print(f"  {f.url[:80]}")

    await page.screenshot(path="debug_me_sap_diag.png", full_page=False)
    print("\nScreenshot saved: debug_me_sap_diag.png")

    await browser.close()
    await pw.stop()


asyncio.run(diagnose())

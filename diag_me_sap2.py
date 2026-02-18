"""Diagnostic: inspect me.sap.com/home with valid session cookies."""
import asyncio
from playwright.async_api import async_playwright


async def diagnose():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(storage_state="auth_state.json")
    page = await ctx.new_page()

    print("Navigating to me.sap.com/home...")
    await page.goto("https://me.sap.com/home", wait_until="load", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    await asyncio.sleep(8)  # Wait for SPA to render

    print(f"URL: {page.url}")
    print(f"Title: {await page.title()}")

    # Close any welcome dialog
    try:
        close_btns = await page.query_selector_all("button")
        for btn in close_btns:
            txt = (await btn.inner_text()).strip().lower()
            if txt in ("close", "dismiss", "skip", "got it", "not now"):
                await btn.click()
                print(f"Closed overlay button: {txt}")
                await asyncio.sleep(1)
    except Exception as e:
        print(f"Overlay dismiss error: {e}")

    # Body text
    body = await page.evaluate("() => (document.body?.innerText || '').substring(0, 2000)")
    print(f"\nBody text ({len(body)} chars):\n{body}")

    wc = await page.evaluate("() => (document.body?.innerText || '').trim().split(/\\s+/).length")
    print(f"\nWord count: {wc}")

    # All links
    links = await page.evaluate("""() => {
        const results = [];
        document.querySelectorAll('a[href]').forEach(a => {
            const t = (a.textContent || '').trim().substring(0, 50);
            const h = a.getAttribute('href') || '';
            if (t) results.push({text: t, href: h});
        });
        return results;
    }""")
    print(f"\nAll <a> links: {len(links)}")
    for l in links[:30]:
        print(f"  [{l['text']}] -> {l['href'][:80]}")

    # Check for navigation items by role/aria
    nav_items = await page.evaluate("""() => {
        const results = [];
        document.querySelectorAll('[role="navigation"] a, nav a, [role="menuitem"], [role="treeitem"]').forEach(el => {
            const t = (el.textContent || '').trim().substring(0, 50);
            const h = el.getAttribute('href') || el.getAttribute('data-href') || '';
            if (t) results.push({text: t, href: h, tag: el.tagName});
        });
        return results;
    }""")
    print(f"\nNav/menu items: {len(nav_items)}")
    for n in nav_items[:20]:
        print(f"  [{n['text']}] -> {n['href'][:60]} ({n['tag']})")

    # Check for items with click handlers (SAP SPA navigation)
    clickables = await page.evaluate("""() => {
        const results = [];
        document.querySelectorAll('[class*="navigation"], [class*="Navigation"], [class*="sidebar"], [class*="Sidebar"], [class*="menu"], [class*="Menu"]').forEach(el => {
            const children = el.querySelectorAll('*');
            children.forEach(c => {
                const t = (c.textContent || '').trim();
                const tag = c.tagName;
                const cls = c.className || '';
                const href = c.getAttribute('href') || '';
                if (t && t.length < 40 && !t.includes('\\n')) {
                    results.push({text: t, tag: tag, cls: String(cls).substring(0, 50), href: href});
                }
            });
        });
        return results;
    }""")
    print(f"\nNavigation/sidebar elements: {len(clickables)}")
    for c in clickables[:30]:
        print(f"  [{c['text']}] {c['tag']} cls={c['cls'][:30]} href={c['href'][:60]}")

    # Check shadow DOM
    shadow = await page.evaluate("""() => {
        let count = 0;
        let text = '';
        document.querySelectorAll('*').forEach(el => {
            if (el.shadowRoot) {
                count++;
                text += (el.shadowRoot.textContent || '').substring(0, 200) + '\\n';
            }
        });
        return {count, text: text.substring(0, 500)};
    }""")
    print(f"\nShadow DOM hosts: {shadow['count']}")
    if shadow['text']:
        print(f"Shadow text: {shadow['text'][:300]}")

    # Full DOM element count
    total = await page.evaluate("() => document.querySelectorAll('*').length")
    print(f"\nTotal DOM elements: {total}")

    await page.screenshot(path="debug_me_sap_auth.png", full_page=False)
    print("\nScreenshot saved: debug_me_sap_auth.png")
    await browser.close()
    await pw.stop()


asyncio.run(diagnose())

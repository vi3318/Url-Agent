"""Examine the live DOM structure of me.sap.com pages."""
import asyncio
import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path('.env'))
except ImportError:
    pass


async def examine():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state="auth_state.json")
        page = await context.new_page()

        # Go to the Get Assistance page (from the screenshot)
        print("Navigating to me.sap.com/getassistance/overview ...")
        await page.goto(
            "https://me.sap.com/getassistance/overview",
            wait_until="load",
            timeout=30000,
        )
        await asyncio.sleep(5)

        # Wait for UI5
        try:
            await page.wait_for_function(
                "typeof sap !== 'undefined' && sap.ui && sap.ui.getCore",
                timeout=25000,
            )
            await asyncio.sleep(3)
        except Exception:
            print("UI5 not detected or timed out")

        print(f"\nPAGE URL: {page.url}\n")

        # 1. ALL <a> tags with href containing me.sap.com
        print("=" * 60)
        print("ALL <a href> LINKS ON PAGE")
        print("=" * 60)
        links = await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.href;
                const text = (a.textContent || '').trim().substring(0, 80);
                if (href.includes('me.sap.com') || href.startsWith('/')) {
                    results.push({href, text});
                }
            });
            return results;
        }""")
        for link in links:
            print(f"  {link['href']}  |  \"{link['text'][:50]}\"")
        print(f"  Total: {len(links)}")

        # 2. Sidebar nav containers
        print("\n" + "=" * 60)
        print("SIDEBAR / NAV CONTAINERS")
        print("=" * 60)
        nav_info = await page.evaluate("""() => {
            const results = [];
            const navEls = document.querySelectorAll(
                'nav, [role="navigation"], .sapTntSideNavigation, ' +
                '[class*="sidebar"], [class*="Sidebar"], .sapMNav'
            );
            for (const nav of navEls) {
                const entry = {
                    tag: nav.tagName,
                    cls: (nav.className || '').toString().substring(0, 120),
                    role: nav.getAttribute('role') || '',
                    items: []
                };
                // Get child items
                const items = nav.querySelectorAll(
                    'a, li, [role="treeitem"], [role="listitem"], .sapMLIB, .sapTntNavLI'
                );
                for (const item of items) {
                    entry.items.push({
                        tag: item.tagName,
                        href: item.getAttribute('href') || '',
                        dataHref: item.dataset.href || item.dataset.url || item.dataset.route || '',
                        text: (item.textContent || '').trim().substring(0, 60),
                        cls: (item.className || '').toString().substring(0, 80),
                    });
                }
                results.push(entry);
            }
            return results;
        }""")
        for nav in nav_info:
            print(f"  <{nav['tag']}> class={nav['cls'][:80]} role={nav['role']}")
            print(f"    {len(nav['items'])} items:")
            for it in nav['items'][:15]:
                print(f"      [{it['tag']}] href={it['href']} data={it['dataHref']} text=\"{it['text'][:50]}\"")

        # 3. Tab bar
        print("\n" + "=" * 60)
        print("TABS / ICON TAB BAR")
        print("=" * 60)
        tabs = await page.evaluate("""() => {
            const results = [];
            const tabItems = document.querySelectorAll(
                '[role="tab"], .sapMITBFilter, .sapMITBItem'
            );
            for (const tab of tabItems) {
                results.push({
                    tag: tab.tagName,
                    text: (tab.textContent || '').trim().substring(0, 60),
                    selected: tab.getAttribute('aria-selected') || '',
                    cls: (tab.className || '').toString().substring(0, 100),
                    id: tab.id || '',
                });
            }
            return results;
        }""")
        for tab in tabs:
            sel = " [SELECTED]" if tab['selected'] == 'true' else ""
            print(f"  {tab['text'][:40]}{sel}  id={tab['id'][:30]}  class={tab['cls'][:60]}")
        print(f"  Total tabs: {len(tabs)}")

        # 4. Content cards / clickable list items
        print("\n" + "=" * 60)
        print("CLICKABLE CARDS / LIST ITEMS IN CONTENT")
        print("=" * 60)
        cards = await page.evaluate("""() => {
            const results = [];
            const items = document.querySelectorAll(
                '.sapMLIB, .sapMSLI, .sapMFLI, .sapMGT, .sapFCard, ' +
                '[class*="Card"], [class*="card"], [class*="tile"]'
            );
            for (const item of items) {
                const role = item.getAttribute('role') || '';
                const tabindex = item.getAttribute('tabindex') || '';
                results.push({
                    tag: item.tagName,
                    text: (item.textContent || '').trim().substring(0, 120),
                    cls: (item.className || '').toString().substring(0, 120),
                    role,
                    tabindex,
                    clickable: tabindex === '0' || role === 'option' || role === 'listitem',
                });
            }
            return results;
        }""")
        for card in cards[:20]:
            click_mark = " [CLICKABLE]" if card['clickable'] else ""
            print(f"  <{card['tag']}> role={card['role']} {click_mark}")
            print(f"    text: \"{card['text'][:100]}\"")
            print(f"    class: {card['cls'][:80]}")
        print(f"  Total: {len(cards)}")

        # 5. innerText of the page (visible text)
        print("\n" + "=" * 60)
        print("VISIBLE TEXT (innerText of body)")
        print("=" * 60)
        visible = await page.evaluate("() => document.body.innerText.trim()")
        print(visible[:2000])
        print(f"\n  ... Total visible text: {len(visible)} chars, {len(visible.split())} words")

        # 6. Check sidebar routes by navigating
        print("\n" + "=" * 60)
        print("TESTING SIDEBAR ROUTES")
        print("=" * 60)
        test_routes = [
            "https://me.sap.com/home",
            "https://me.sap.com/calendar",
            "https://me.sap.com/reporting",
            "https://me.sap.com/dashboards",
            "https://me.sap.com/services-support",
            "https://me.sap.com/servicessupport",
            "https://me.sap.com/services&support",
        ]
        for route in test_routes:
            try:
                resp = await page.goto(route, wait_until="domcontentloaded", timeout=10000)
                status = resp.status if resp else "no response"
                final_url = page.url
                title = await page.title()
                print(f"  {route}")
                print(f"    -> status={status} final_url={final_url} title={title[:50]}")
            except Exception as e:
                print(f"  {route}")
                print(f"    -> ERROR: {e}")

        await browser.close()


asyncio.run(examine())

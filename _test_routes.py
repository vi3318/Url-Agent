"""Test SAP for Me URL patterns by clicking sidebar & tab items."""
import asyncio
import json
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path('.env'))
except ImportError:
    pass


async def test_routes():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state="auth_state.json")
        page = await context.new_page()

        # Navigate to Get Assistance
        print("=== NAVIGATING TO me.sap.com/getassistance/overview ===")
        await page.goto("https://me.sap.com/getassistance/overview", wait_until="load", timeout=30000)
        await asyncio.sleep(5)
        try:
            await page.wait_for_function("typeof sap !== 'undefined' && sap.ui && sap.ui.getCore", timeout=15000)
            await asyncio.sleep(3)
        except Exception:
            print("UI5 not detected")

        print(f"Current URL: {page.url}\n")

        # PART 1: Get sidebar item texts and try clicking each
        print("=" * 60)
        print("SIDEBAR ITEMS - CLICK & CAPTURE URL")
        print("=" * 60)
        
        sidebar_texts = await page.evaluate("""() => {
            const texts = [];
            document.querySelectorAll('.sapMeSidebarTextContent').forEach(el => {
                const t = (el.textContent || '').trim();
                if (t && !texts.includes(t)) texts.push(t);
            });
            return texts;
        }""")
        print(f"Sidebar items found: {sidebar_texts}")
        
        for text in sidebar_texts:
            try:
                # Navigate back to base page first
                await page.goto("https://me.sap.com/getassistance/overview", wait_until="load", timeout=15000)
                await asyncio.sleep(3)
                
                # Find and click the sidebar item
                el = await page.evaluate(f"""() => {{
                    const items = document.querySelectorAll('.sapMeSidebarTextContent');
                    for (const item of items) {{
                        if (item.textContent.trim() === {json.dumps(text)}) {{
                            // Click the parent sidebar item
                            let target = item.closest('.sapMeSidebarItem') || item.closest('.sapMeSidebarTab') || item;
                            target.click();
                            return true;
                        }}
                    }}
                    return false;
                }}""")
                
                if el:
                    await asyncio.sleep(3)
                    print(f"  '{text}' -> {page.url}")
                else:
                    print(f"  '{text}' -> [element not found]")
            except Exception as e:
                print(f"  '{text}' -> ERROR: {str(e)[:80]}")

        # PART 2: Tab items on Get Assistance page
        print("\n" + "=" * 60)
        print("TAB ITEMS - CLICK & CAPTURE URL")
        print("=" * 60)
        
        # Go back to Get Assistance
        await page.goto("https://me.sap.com/getassistance/overview", wait_until="load", timeout=15000)
        await asyncio.sleep(5)
        
        tab_texts = await page.evaluate("""() => {
            const texts = [];
            const tabs = document.querySelectorAll('.sapMITBFilter');
            for (const tab of tabs) {
                const text = tab.querySelector('.sapMITBText');
                if (text) {
                    const t = (text.textContent || '').trim();
                    if (t && !texts.includes(t) && t !== 'More') texts.push(t);
                }
            }
            return texts;
        }""")
        print(f"Tab items found: {tab_texts}")
        
        for text in tab_texts:
            try:
                # Navigate back first
                await page.goto("https://me.sap.com/getassistance/overview", wait_until="load", timeout=15000)
                await asyncio.sleep(3)
                
                clicked = await page.evaluate(f"""() => {{
                    const tabs = document.querySelectorAll('.sapMITBFilter');
                    for (const tab of tabs) {{
                        const textEl = tab.querySelector('.sapMITBText');
                        if (textEl && textEl.textContent.trim() === {json.dumps(text)}) {{
                            tab.click();
                            return true;
                        }}
                    }}
                    return false;
                }}""")
                
                if clicked:
                    await asyncio.sleep(3)
                    print(f"  '{text}' -> {page.url}")
                else:
                    print(f"  '{text}' -> [not found]")
            except Exception as e:
                print(f"  '{text}' -> ERROR: {str(e)[:80]}")

        # PART 3: Clickable list items
        print("\n" + "=" * 60)
        print("CLICKABLE LIST ITEMS - CLICK & CAPTURE URL")
        print("=" * 60)
        
        await page.goto("https://me.sap.com/getassistance/overview", wait_until="load", timeout=15000)
        await asyncio.sleep(5)
        
        list_items = await page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('.sapMLIBTypeActive, .sapMLIBTypeNavigation').forEach(el => {
                const title = el.querySelector('.sapMSLITitleOnly, .sapMSLITitle, .sapMDLITitle, .sapMFLITitle');
                if (title) {
                    const t = (title.textContent || '').trim();
                    if (t && !items.includes(t)) items.push(t);
                }
            });
            return items;
        }""")
        print(f"Clickable list items found: {list_items}")
        
        for text in list_items:
            try:
                await page.goto("https://me.sap.com/getassistance/overview", wait_until="load", timeout=15000)
                await asyncio.sleep(3)
                
                clicked = await page.evaluate(f"""() => {{
                    const items = document.querySelectorAll('.sapMLIBTypeActive, .sapMLIBTypeNavigation');
                    for (const item of items) {{
                        const title = item.querySelector('.sapMSLITitleOnly, .sapMSLITitle, .sapMDLITitle, .sapMFLITitle');
                        if (title && title.textContent.trim() === {json.dumps(text)}) {{
                            item.click();
                            return true;
                        }}
                    }}
                    return false;
                }}""")
                
                if clicked:
                    await asyncio.sleep(3)
                    print(f"  '{text}' -> {page.url}")
                else:
                    print(f"  '{text}' -> [not found]")
            except Exception as e:
                print(f"  '{text}' -> ERROR: {str(e)[:80]}")

        # PART 4: Direct URL pattern tests
        print("\n" + "=" * 60)
        print("DIRECT URL PATTERN TESTS")
        print("=" * 60)
        
        test_urls = [
            # Sidebar routes
            "https://me.sap.com/home",
            "https://me.sap.com/calendar",  
            "https://me.sap.com/reporting",
            "https://me.sap.com/dashboards",
            "https://me.sap.com/financelegal",
            "https://me.sap.com/finance-legal",
            "https://me.sap.com/financelegal",
            "https://me.sap.com/portfolioproducts",
            "https://me.sap.com/portfolio-products",
            "https://me.sap.com/servicessupport",
            "https://me.sap.com/services-support",
            "https://me.sap.com/systemsprovisioning",
            "https://me.sap.com/systems-provisioning",
            "https://me.sap.com/userscontacts",
            "https://me.sap.com/users-contacts",
            "https://me.sap.com/feedback",
            "https://me.sap.com/legal",
            # Tab routes under getassistance
            "https://me.sap.com/getassistance",
            "https://me.sap.com/getassistance/overview",
            "https://me.sap.com/getassistance/reporting",
            "https://me.sap.com/getassistance/financelegal",
            "https://me.sap.com/getassistance/finance-legal",
            "https://me.sap.com/getassistance/portfolioproducts",
            "https://me.sap.com/getassistance/portfolio-products",
            "https://me.sap.com/getassistance/servicessupport",
            "https://me.sap.com/getassistance/services-support",
            "https://me.sap.com/getassistance/systemsprovisioning",
            "https://me.sap.com/getassistance/systems-provisioning",
            "https://me.sap.com/getassistance/userscontacts",
            "https://me.sap.com/getassistance/users-contacts",
            "https://me.sap.com/getassistance/crosscapabilities",
            "https://me.sap.com/getassistance/cross-capabilities",
        ]
        
        for url in test_urls:
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                status = resp.status if resp else "no response"
                final_url = page.url
                # Check if we got redirected to an error page or login
                is_error = "error" in final_url.lower() or "login" in final_url.lower()
                title = await page.title()
                mark = " *** REDIRECTED ***" if final_url != url else ""
                err = " [ERROR PAGE]" if is_error else ""
                print(f"  {url}")
                print(f"    -> {status} final={final_url}{mark}{err} title={title[:40]}")
            except Exception as e:
                print(f"  {url}")
                print(f"    -> TIMEOUT/ERROR: {str(e)[:60]}")

        # PART 5: Try UI5 router to get all defined routes
        print("\n" + "=" * 60)
        print("UI5 ROUTER ROUTES")
        print("=" * 60)
        
        await page.goto("https://me.sap.com/getassistance/overview", wait_until="load", timeout=15000)
        await asyncio.sleep(5)
        
        routes = await page.evaluate("""() => {
            try {
                if (typeof sap !== 'undefined' && sap.ui && sap.ui.getCore) {
                    const core = sap.ui.getCore();
                    // Try to find components with routers
                    const results = [];
                    
                    // Method 1: Get all route targets from hash changer
                    try {
                        const hc = sap.ui.core.routing.HashChanger.getInstance();
                        if (hc) results.push({method: 'HashChanger', hash: hc.getHash()});
                    } catch(e) {}
                    
                    // Method 2: Try to find manifest routes
                    try {
                        const comps = core.getLoadedLibraries ? Object.keys(core.getLoadedLibraries()) : [];
                        results.push({method: 'Libraries', data: comps.join(', ')});
                    } catch(e) {}
                    
                    // Method 3: Check window location
                    results.push({method: 'location', hash: window.location.hash, path: window.location.pathname});
                    
                    // Method 4: Look for any route patterns in the DOM
                    try {
                        const scripts = document.querySelectorAll('script[type="application/json"]');
                        scripts.forEach(s => {
                            try {
                                const data = JSON.parse(s.textContent);
                                if (data.routes || data['sap.ui5']) {
                                    results.push({method: 'manifest', data: JSON.stringify(data).substring(0, 500)});
                                }
                            } catch(e) {}
                        });
                    } catch(e) {}
                    
                    return results;
                }
                return [{method: 'error', data: 'sap not found'}];
            } catch(e) {
                return [{method: 'error', data: e.toString()}];
            }
        }""")
        for r in routes:
            print(f"  {r.get('method', '?')}: {json.dumps(r)[:200]}")

        await browser.close()
        print("\n=== DONE ===")


asyncio.run(test_routes())

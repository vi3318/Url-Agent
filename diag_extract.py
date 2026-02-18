"""Diagnose content extraction on me.sap.com after SAP SAML login."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # Load saved auth state
        state_path = os.path.join(os.path.dirname(__file__), 'auth_state.json')
        if not os.path.exists(state_path):
            print("No auth_state.json — run crawler with --force-login first")
            return

        ctx = await browser.new_context(storage_state=state_path)
        page = await ctx.new_page()

        print("1. Navigating to me.sap.com/home...")
        resp = await page.goto('https://me.sap.com/home', wait_until='load', timeout=30000)
        print(f"   Status: {resp.status if resp else 'None'}")
        print(f"   URL: {page.url}")
        
        try:
            await page.wait_for_load_state('networkidle', timeout=10000)
        except:
            print("   (networkidle timeout — continuing)")

        print(f"   Title: {await page.title()}")

        # Check if redirected to login
        if 'accounts.sap.com' in page.url or 'Sign In' in (await page.title()):
            print("   ⚠️ Session expired — redirected to login page!")
            print("   Run crawler with --force-login to get fresh cookies")
            await browser.close()
            return

        # Wait for UI5
        print("\n2. Waiting for SAP UI5...")
        try:
            await page.wait_for_selector('[data-sap-ui-area]', timeout=15000)
            print("   ✅ UI5 area found")
        except:
            print("   ⚠️ No UI5 area selector")

        # Extra wait for rendering
        await asyncio.sleep(3)

        # Step 3: Body text BEFORE any modifications
        print("\n3. Body text BEFORE modifications:")
        body_text = await page.evaluate("() => document.body.innerText.trim()")
        word_count = len(body_text.split())
        print(f"   Word count: {word_count}")
        print(f"   First 300 chars: {body_text[:300]}")

        # Step 4: Check what elements exist
        print("\n4. Key elements on page:")
        checks = {
            'nav': 'nav',
            'header': 'header',
            'footer': 'footer',
            '.sidebar': '.sidebar',
            'body': 'body',
            '.sapMeSidebarItem': '.sapMeSidebarItem',
            '.sapMShellContent': '.sapMShellContent',
            '.sapFDynamicPageContent': '.sapFDynamicPageContent',
            '.sapMPage': '.sapMPage',
            '.sapMPageContent': '.sapMPageContent',
            '.sapUiBody': '.sapUiBody',
            '#consent_blackbar': '#consent_blackbar',
            '.help4': '.help4',
            '.sapMDialog': '.sapMDialog',
        }
        for name, sel in checks.items():
            try:
                els = await page.query_selector_all(sel)
                if els:
                    texts = []
                    for el in els[:2]:
                        t = await el.evaluate("el => (el.innerText || '').trim().substring(0, 100)")
                        texts.append(f"\"{t}\"")
                    print(f"   {name}: {len(els)} found — {', '.join(texts)}")
                else:
                    print(f"   {name}: NOT found")
            except Exception as e:
                print(f"   {name}: ERROR: {e}")

        # Step 5: Simulate overlay dismissal
        print("\n5. Simulating overlay dismissal...")
        removed = await page.evaluate("""
            () => {
                let count = 0;
                // TrustArc
                const trustArc = document.getElementById('consent_blackbar');
                if (trustArc) { trustArc.remove(); count++; }
                const trustOverlay = document.getElementById('trustarc-banner-overlay');
                if (trustOverlay) { trustOverlay.remove(); count++; }
                document.querySelectorAll('[id*="truste"], [class*="truste"], [id*="trustarc"]')
                    .forEach(el => { el.remove(); count++; });
                // help4
                document.querySelectorAll('.help4, .help4-tour, [class*="help4-adapter"]')
                    .forEach(el => { el.remove(); count++; });
                // SAP dialogs
                document.querySelectorAll('.sapMDialog, .sapMPopover, .sapUiBLy, .sapMDialogBLy')
                    .forEach(el => { el.remove(); count++; });
                return count;
            }
        """)
        print(f"   Removed {removed} overlay elements")

        body_text2 = await page.evaluate("() => document.body.innerText.trim()")
        wc2 = len(body_text2.split())
        print(f"   Body text after overlay removal: {wc2} words")
        print(f"   First 300 chars: {body_text2[:300]}")

        # Step 6: Simulate exclude_selectors
        print("\n6. Simulating exclude_selectors removal...")
        exclude_sels = ['nav', 'header', 'footer', '.sidebar', '.toc', '.breadcrumb']
        for sel in exclude_sels:
            try:
                removed_count = await page.evaluate(f"""
                    () => {{
                        const els = document.querySelectorAll('{sel}');
                        let c = 0;
                        els.forEach(el => {{ el.remove(); c++; }});
                        return c;
                    }}
                """)
                if removed_count > 0:
                    print(f"   Removed {removed_count} '{sel}' elements")
            except:
                pass

        body_text3 = await page.evaluate("() => document.body.innerText.trim()")
        wc3 = len(body_text3.split())
        print(f"   Body text after exclude removal: {wc3} words")
        print(f"   Full text: {body_text3[:500]}")

        # Step 7: Check sidebar items for SPA nav
        print("\n7. Checking sidebar items for SPA navigation...")
        # Need to re-navigate since we removed elements
        await page.goto('https://me.sap.com/home', wait_until='load', timeout=30000)
        try:
            await page.wait_for_load_state('networkidle', timeout=10000)
        except:
            pass
        await asyncio.sleep(3)

        sidebar_items = await page.evaluate("""
            () => {
                const items = [];
                document.querySelectorAll(
                    '.sapMeSidebarItem, .sapTntNavLI, [class*="SidebarItem"]'
                ).forEach((el, idx) => {
                    const text = (el.textContent || '').trim();
                    if (text && text.length < 80) {
                        items.push({
                            index: idx,
                            text: text.split('\\n')[0].trim(),
                            tag: el.tagName,
                            cls: (el.className || '').substring(0, 60),
                        });
                    }
                });
                return items;
            }
        """)
        print(f"   Found {len(sidebar_items)} sidebar items:")
        for item in sidebar_items[:15]:
            print(f"     [{item['text']}] {item['tag']} cls={item['cls']}")

        await browser.close()

asyncio.run(main())

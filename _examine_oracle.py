#!/usr/bin/env python3
"""Examine live DOM structure of Oracle docs pages to understand extraction issues."""
import asyncio
from playwright.async_api import async_playwright

URL = "https://docs.oracle.com/en/cloud/saas/human-resources/fawhs/index.html"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(URL, timeout=30000, wait_until='load')
        
        try:
            await page.wait_for_load_state('networkidle', timeout=10000)
        except:
            pass
        
        await asyncio.sleep(3)
        
        # 1. Dump the top-level structure
        print("=== TOP-LEVEL BODY CHILDREN ===")
        structure = await page.evaluate("""
            () => {
                function describe(el, depth) {
                    if (depth > 3) return '';
                    const tag = el.tagName?.toLowerCase() || '';
                    const id = el.id ? '#' + el.id : '';
                    const cls = el.className && typeof el.className === 'string' 
                        ? '.' + el.className.trim().split(/\\s+/).join('.') : '';
                    const role = el.getAttribute?.('role') ? '[role=' + el.getAttribute('role') + ']' : '';
                    const text = (el.innerText || '').trim().substring(0, 80);
                    const textLen = (el.innerText || '').trim().length;
                    const indent = '  '.repeat(depth);
                    let result = indent + tag + id + cls + role + ' (' + textLen + ' chars)';
                    if (textLen < 200 && text) {
                        result += ' "' + text.replace(/\\n/g, ' | ').substring(0, 60) + '"';
                    }
                    result += '\\n';
                    
                    for (const child of el.children) {
                        result += describe(child, depth + 1);
                    }
                    return result;
                }
                return describe(document.body, 0);
            }
        """)
        print(structure)
        
        # 2. Check which content selectors match
        print("\n=== CONTENT SELECTOR MATCHES ===")
        selectors = [
            'main', 'article', '.content', '.main-content', '#content',
            '[role="main"]', '.documentation', '.doc-content',
            '.ohc-main-content', '.topic-content',
            '.ohc-book-content', '.ohc-topic-body',
            '#ohc-main-content', '.ohc-content',
            '.o-main-content', '.o-hcw-content-body',
            'body > .content-container',
        ]
        for sel in selectors:
            result = await page.evaluate(f"""
                () => {{
                    const el = document.querySelector('{sel}');
                    if (!el) return null;
                    const tag = el.tagName?.toLowerCase() || '';
                    const id = el.id ? '#' + el.id : '';
                    const cls = el.className && typeof el.className === 'string'
                        ? '.' + el.className.trim().split(/\\s+/).slice(0, 3).join('.') : '';
                    const textLen = (el.innerText || '').trim().length;
                    return tag + id + cls + ' (' + textLen + ' chars)';
                }}
            """)
            if result:
                print(f"  {sel:30s} → {result}")
                
        # 3. What elements have the social share / junk text?
        print("\n=== JUNK TEXT LOCATIONS ===")
        junk = await page.evaluate("""
            () => {
                const results = [];
                const texts = ['Share on LinkedIn', 'Share on X', 'Skip to Content', 
                    'No matching results', 'Search Unavailable', 'Was this page helpful',
                    '© Oracle', 'About Oracle', 'Terms of Use'];
                
                const walk = (node) => {
                    if (node.nodeType === 3) {
                        const t = node.textContent.trim();
                        for (const jt of texts) {
                            if (t.includes(jt)) {
                                let parent = node.parentElement;
                                let path = [];
                                while (parent && parent !== document.body) {
                                    const tag = parent.tagName?.toLowerCase() || '';
                                    const id = parent.id ? '#' + parent.id : '';
                                    const cls = parent.className && typeof parent.className === 'string'
                                        ? '.' + parent.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
                                    path.unshift(tag + id + cls);
                                    parent = parent.parentElement;
                                }
                                results.push({
                                    text: jt,
                                    path: path.join(' > '),
                                    parentTag: node.parentElement?.tagName?.toLowerCase(),
                                    parentClass: node.parentElement?.className?.trim()?.split(/\\s+/)?.[0] || '',
                                });
                                break;
                            }
                        }
                    }
                    for (const child of node.childNodes) walk(child);
                };
                walk(document.body);
                return results;
            }
        """)
        for item in junk:
            print(f"  '{item['text']}' → {item['path']}")
            
        # 4. What does the actual content area look like in Oracle docs?
        print("\n=== ORACLE DOC CONTENT AREA ===")
        content_area = await page.evaluate("""
            () => {
                // Try OHC selectors
                const candidates = [
                    '.ohc-main-content',
                    '.ohc-book-content', 
                    '.o-hcw-content-body',
                    '#bookcontent',
                    '#bookcontainer',
                    '.book-content',
                    '.topic-content',
                    '[class*="ohc"]',
                    '[class*="book"]',
                    '[class*="topic"]',
                    '[id*="book"]',
                    '[id*="content"]',
                    '.o-main-content',
                ];
                const results = [];
                for (const sel of candidates) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const tag = el.tagName?.toLowerCase() || '';
                        const id = el.id ? '#' + el.id : '';
                        const cls = el.className && typeof el.className === 'string'
                            ? '.' + el.className.trim().split(/\\s+/).slice(0, 3).join('.') : '';
                        const textLen = (el.innerText || '').trim().length;
                        results.push(sel + ' → ' + tag + id + cls + ' (' + textLen + ' chars)');
                    }
                }
                return results;
            }
        """)
        for r in content_area:
            print(f"  {r}")
            
        # 5. Also look at the deep article page
        print("\n\n=== TRYING DEEP ARTICLE PAGE ===")
        await page.goto(
            "https://docs.oracle.com/en/cloud/saas/human-resources/rwhcm/how-do-i-get-started-adopting-redwood.html",
            timeout=30000, wait_until='load'
        )
        try:
            await page.wait_for_load_state('networkidle', timeout=10000)
        except:
            pass
        await asyncio.sleep(3)
        
        # Check selectors on article page
        print("Content selector matches on article page:")
        for sel in selectors:
            result = await page.evaluate(f"""
                () => {{
                    const el = document.querySelector('{sel}');
                    if (!el) return null;
                    const tag = el.tagName?.toLowerCase() || '';
                    const id = el.id ? '#' + el.id : '';
                    const cls = el.className && typeof el.className === 'string'
                        ? '.' + el.className.trim().split(/\\s+/).slice(0, 3).join('.') : '';
                    const textLen = (el.innerText || '').trim().length;
                    return tag + id + cls + ' (' + textLen + ' chars)';
                }}
            """)
            if result:
                print(f"  {sel:30s} → {result}")
        
        # Check junk on article page
        print("\nJunk text on article page:")
        junk2 = await page.evaluate("""
            () => {
                const results = [];
                const texts = ['Share on LinkedIn', 'Skip to Content', 
                    'Was this page helpful', '© Oracle', 'About Oracle'];
                const walk = (node) => {
                    if (node.nodeType === 3) {
                        const t = node.textContent.trim();
                        for (const jt of texts) {
                            if (t.includes(jt)) {
                                let el = node.parentElement;
                                const tag = el?.tagName?.toLowerCase() || '';
                                const cls = el?.className?.trim()?.split(/\\s+/)?.[0] || '';
                                const id = el?.id || '';
                                let ancestor = el;
                                while (ancestor && ancestor.tagName !== 'BODY') {
                                    if (ancestor.tagName === 'NAV' || ancestor.tagName === 'FOOTER' ||
                                        ancestor.tagName === 'HEADER' || 
                                        ancestor.getAttribute?.('role') === 'navigation') {
                                        break;
                                    }
                                    ancestor = ancestor.parentElement;
                                }
                                const inSemantic = ancestor && ancestor.tagName !== 'BODY';
                                results.push({
                                    text: jt,
                                    tag: tag,
                                    cls: cls,
                                    id: id,
                                    inNav: inSemantic,
                                    ancestorTag: ancestor?.tagName?.toLowerCase() || 'body',
                                });
                                break;
                            }
                        }
                    }
                    for (const child of node.childNodes) walk(child);
                };
                walk(document.body);
                return results;
            }
        """)
        for item in junk2:
            nav_status = 'IN-NAV' if item.get('inNav') else 'NOT-IN-NAV'
            print(f"  '{item['text']}' → {item['tag']}.{item['cls']}#{item.get('id','')} [{nav_status}] ancestor={item.get('ancestorTag','?')}")
        
        await browser.close()

asyncio.run(main())

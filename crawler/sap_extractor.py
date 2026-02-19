"""
SAP / UI5 Extractor Module
===========================
Handles SAP-specific page rendering and content extraction.

Features:
    - UI5/Fiori dynamic rendering detection
    - Virtual scrolling table support (sap.m.Table, sap.ui.table.Table)
    - Smart waits (wait_for_selector, networkidle) instead of static delays
    - Incremental scroll-to-load for virtualized tables
    - SAP-specific content selectors
    - CSRF token extraction
    - Session expiry detection via URL redirect patterns

SAP UI5 uses virtual tables that only render rows visible in the viewport.
This module scrolls the table container incrementally and waits for new
rows to render, collecting all data before extraction.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SAP UI5 detection selectors
# ---------------------------------------------------------------------------

# Selectors that indicate SAP UI5 / Fiori content
_UI5_INDICATORS: List[str] = [
    '[data-sap-ui-area]',          # UI5 rendering area
    '.sapUiBody',                   # UI5 body class
    '.sapMShellContent',            # Mobile shell content
    '.sapUshellShellHead',          # Fiori launchpad header
    '#shell-header',                # Fiori shell header
    '.sapFDynamicPage',             # Fiori dynamic page
    '.sapUiLocalBusyIndicator',     # UI5 busy indicator
    'script[src*="sap-ui-core"]',   # UI5 bootstrap script
    'script[src*="openui5"]',       # OpenUI5
    'script[src*="sapui5"]',        # SAPUI5
    'link[href*="sap-ui"]',         # UI5 theme CSS
]

# SAP table containers
_SAP_TABLE_SELECTORS: List[str] = [
    '.sapMList',                    # sap.m.List
    '.sapMTable',                   # sap.m.Table
    '.sapUiTable',                  # sap.ui.table.Table
    '.sapUiTableCCnt',              # Table content container
    '.sapMListItems',               # List items area
    '.sapMGrowingList',             # Growing list (auto-scroll)
    '.sapUiTableCtrlScr',           # Table scroll container
    '.sapMListTbl',                 # Table in list mode
    'table.sapMListTbl',            # Explicit table
    '.sapUiAnalyticalTable',        # Analytical table
    '.sapUiTreeTable',              # Tree table
]

# SAP table row selectors
_SAP_ROW_SELECTORS: List[str] = [
    '.sapMLIB',                     # List item base
    '.sapMListTblRow',              # Table row
    '.sapUiTableRow',               # ui.table row
    '.sapMListItem',                # List item
    'tr.sapUiTableTr',              # Table tr
    '.sapMGrowingListItem',         # Growing list item
]

# Content selectors specific to SAP portals
_SAP_CONTENT_SELECTORS: List[str] = [
    '.sapFDynamicPageContent',       # Fiori dynamic page content
    '.sapUiComponentContainer',      # Component container
    '.sapMPage',                     # Mobile page
    '.sapMPageContent',              # Mobile page content
    '#content',                      # Generic content
    '.sapContainerContent',          # Container content
    '[role="main"]',                 # Main content area
    '.sapUiBody',                    # Full body
    '.sapMShellContent',             # Shell content
    '.sapMPanel',                    # Panel
    '.sapMObjectHeader',             # Object header
    '.sapMObjectListItem',           # Object list item
    '.sapMFeedListItem',             # Feed list item
    '.sapUiRichTextEditor',          # Rich text editor content
]

# SAP login redirect patterns for session expiry detection
_SAP_SESSION_EXPIRY_PATTERNS: List[str] = [
    'accounts.sap.com',
    '/saml2/idp/sso',
    '/saml/',
    '/nidp/',
    '/adfs/',
    '/idp/',
    '/login',
    'login.microsoftonline.com',
    'authn/SSORedirect',
    'SAMLRequest=',
]


async def detect_sap_ui5(page: Page) -> bool:
    """Detect if the current page is an SAP UI5 / Fiori application.

    Returns:
        True if SAP UI5 indicators are found.
    """
    for sel in _UI5_INDICATORS:
        try:
            el = await page.query_selector(sel)
            if el:
                logger.debug(f"[SAP-UI5] Detected indicator: {sel}")
                return True
        except Exception:
            continue
    return False


async def wait_for_ui5_ready(page: Page, timeout_ms: int = 30_000) -> bool:
    """Wait for SAP UI5 framework to finish rendering.

    Checks:
        1. ``sap.ui.getCore().isReady()`` — UI5 core initialized
        2. No busy indicators visible
        3. Network is idle

    Args:
        timeout_ms: Maximum wait time in milliseconds.

    Returns:
        True if UI5 is ready, False on timeout.
    """
    logger.info("[SAP-UI5] Waiting for UI5 framework ready...")

    # Wait for networkidle first
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15_000))
    except PlaywrightTimeout:
        pass

    # Wait for UI5 core ready via JavaScript
    try:
        is_ready = await page.evaluate("""
            () => {
                return new Promise((resolve) => {
                    const check = () => {
                        try {
                            if (typeof sap !== 'undefined' &&
                                sap.ui && sap.ui.getCore &&
                                sap.ui.getCore().isReady &&
                                sap.ui.getCore().isReady()) {
                                resolve(true);
                                return;
                            }
                        } catch (e) {}
                        resolve(false);
                    };
                    // Give UI5 a moment to bootstrap
                    setTimeout(check, 2000);
                });
            }
        """)
        if is_ready:
            logger.info("[SAP-UI5] UI5 core reports ready")
    except Exception as e:
        logger.debug(f"[SAP-UI5] Could not check UI5 ready state: {e}")

    # Wait for busy indicators to disappear
    busy_selectors = [
        '.sapUiLocalBusyIndicator',
        '.sapUiBusy',
        '.sapMBusyIndicator',
        '.sapMBusyDialog',
    ]
    for sel in busy_selectors:
        try:
            await page.wait_for_selector(
                sel, state="hidden", timeout=min(timeout_ms, 10_000)
            )
        except PlaywrightTimeout:
            pass
        except Exception:
            continue

    # Extra wait for late-rendering components
    await asyncio.sleep(1.0)

    # Final networkidle check
    try:
        await page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightTimeout:
        pass

    logger.info("[SAP-UI5] UI5 ready — proceeding with extraction")
    return True


async def scroll_virtual_table(
    page: Page,
    container_selector: str = "",
    max_scroll_iterations: int = 50,
    scroll_pause_ms: int = 800,
    scroll_amount: int = 500,
) -> int:
    """Scroll a virtualized SAP table to render all rows.

    SAP UI5 tables use virtual scrolling — only visible rows are in the DOM.
    This function scrolls incrementally and waits for new rows to render.

    Args:
        page: Playwright page.
        container_selector: CSS selector for the scroll container.
            If empty, auto-detects SAP table containers.
        max_scroll_iterations: Max number of scroll steps.
        scroll_pause_ms: Pause between scrolls (ms) for rendering.
        scroll_amount: Pixels to scroll per step.

    Returns:
        Total number of unique rows found after scrolling.
    """
    # Auto-detect table container if not specified
    if not container_selector:
        container_selector = await _find_sap_table(page)
        if not container_selector:
            logger.debug("[SAP-UI5] No SAP table found on page")
            return 0

    logger.info(
        f"[SAP-UI5] Scrolling virtual table: {container_selector}"
    )

    # Get initial row count
    prev_row_count = await _count_table_rows(page, container_selector)
    total_stable = 0

    for i in range(max_scroll_iterations):
        # Scroll the container
        try:
            scrolled = await page.evaluate(f"""
                (args) => {{
                    const container = document.querySelector(args.sel);
                    if (!container) return false;
                    const before = container.scrollTop;
                    container.scrollTop += args.amount;
                    return container.scrollTop > before;
                }}
            """, {"sel": container_selector, "amount": scroll_amount})
        except Exception as e:
            logger.debug(f"[SAP-UI5] Scroll error: {e}")
            break

        if not scrolled:
            # Also try scrolling the page itself
            try:
                await page.evaluate(f"""
                    window.scrollBy(0, {scroll_amount});
                """)
            except Exception:
                pass

        # Wait for rendering
        await asyncio.sleep(scroll_pause_ms / 1000)

        # Wait for any loading indicators
        try:
            await page.wait_for_selector(
                '.sapUiLocalBusyIndicator, .sapMBusyIndicator',
                state="hidden",
                timeout=3000,
            )
        except PlaywrightTimeout:
            pass

        # Check row count
        current_rows = await _count_table_rows(page, container_selector)

        if current_rows == prev_row_count:
            total_stable += 1
            if total_stable >= 3:
                # No new rows for 3 consecutive scrolls — done
                logger.info(
                    f"[SAP-UI5] Table scroll complete: {current_rows} rows "
                    f"after {i + 1} scrolls"
                )
                break
        else:
            total_stable = 0
            logger.debug(
                f"[SAP-UI5] Scroll {i + 1}: {prev_row_count} → {current_rows} rows"
            )
            prev_row_count = current_rows

    return prev_row_count


async def scroll_page_for_content(
    page: Page,
    max_scrolls: int = 30,
    scroll_pause_ms: int = 600,
    scroll_amount: int = 800,
) -> int:
    """Scroll the entire page to trigger lazy-loaded content.

    Useful for SAP pages that use infinite scroll or lazy loading
    outside of formal table controls.

    Returns:
        Total scroll height reached.
    """
    prev_height = 0
    stable_count = 0

    for i in range(max_scrolls):
        # Scroll down
        try:
            current_height = await page.evaluate("""
                () => {
                    window.scrollBy(0, arguments[0] || 800);
                    return document.body.scrollHeight;
                }
            """)
        except Exception:
            # Alternative scroll method
            try:
                await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
                current_height = await page.evaluate(
                    "document.body.scrollHeight"
                )
            except Exception:
                break

        await asyncio.sleep(scroll_pause_ms / 1000)

        if current_height == prev_height:
            stable_count += 1
            if stable_count >= 3:
                break
        else:
            stable_count = 0
            prev_height = current_height

    # Scroll back to top
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass

    return prev_height


async def extract_sap_tables(page: Page) -> List[Dict]:
    """Extract data from all SAP tables on the page.

    Handles both sap.m.Table and sap.ui.table.Table controls.

    Returns:
        List of table dicts, each with 'headers' and 'rows' keys.
    """
    tables = []

    # Find all SAP table containers
    for sel in _SAP_TABLE_SELECTORS:
        try:
            elements = await page.query_selector_all(sel)
            for el in elements:
                table_data = await _extract_single_table(page, el)
                if table_data and table_data.get("rows"):
                    tables.append(table_data)
        except Exception:
            continue

    # Also try standard HTML tables within SAP pages
    try:
        html_tables = await page.query_selector_all("table")
        for tbl in html_tables:
            table_data = await _extract_html_table(tbl)
            if table_data and table_data.get("rows"):
                tables.append(table_data)
    except Exception:
        pass

    if tables:
        total_rows = sum(len(t.get("rows", [])) for t in tables)
        logger.info(
            f"[SAP-UI5] Extracted {len(tables)} tables, {total_rows} total rows"
        )

    return tables


async def detect_sap_session_expiry(page: Page, intended_url: str = "") -> bool:
    """Detect if SAP session has expired (redirected to login).

    Args:
        page: The current page.
        intended_url: The URL we intended to navigate to.

    Returns:
        True if session expiry detected.
    """
    current_url = page.url.lower()
    intended_lower = intended_url.lower() if intended_url else ""

    # If we intentionally went to a login page, skip
    if intended_lower:
        for pattern in _SAP_SESSION_EXPIRY_PATTERNS:
            if pattern.lower() in intended_lower:
                return False

    # Check if redirected to a login/SSO page
    for pattern in _SAP_SESSION_EXPIRY_PATTERNS:
        pat_lower = pattern.lower()
        if pat_lower in current_url and pat_lower not in intended_lower:
            logger.warning(
                f"[SAP-UI5] Session expired — redirected to: "
                f"{page.url[:100]}"
            )
            return True

    # Check for SAP-specific session timeout messages
    timeout_selectors = [
        '.sapMMessagePage',            # UI5 message page
        ':text("session expired")',
        ':text("session timed out")',
        ':text("please log in again")',
        ':text("re-authenticate")',
    ]
    for sel in timeout_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                logger.warning(f"[SAP-UI5] Session timeout indicator: {sel}")
                return True
        except Exception:
            continue

    return False


async def discover_sap_tile_routes(
    page: Page,
    base_origin: str,
    current_url: str,
) -> List[str]:
    """Discover additional routes by clicking SAP GenericTiles and Cards.

    SAP for Me uses `.sapMGT` (GenericTile) and `.sapFCard` (Integration
    Card) components for dashboard navigation.  These tiles often use JS
    press handlers instead of `<a href>` so normal link extraction misses
    them.

    Strategy:
        1. Gather all tile/card elements
        2. For each: click → wait for URL change → record new URL → go back
        3. Skip tiles that open external sites or overlays

    Returns:
        List of newly discovered same-origin URLs.
    """
    discovered: List[str] = []
    try:
        # Get total count of clickable tiles/cards
        tile_count = await page.evaluate("""
            () => {
                const tiles = document.querySelectorAll(
                    '.sapMGT, .sapFCard, .sapMGenericTile, ' +
                    '.sapMTile, .sapFCardHeader'
                );
                // Filter to visible tiles only
                return Array.from(tiles).filter(t => {
                    const r = t.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }).length;
            }
        """)
        if not tile_count:
            return discovered

        logger.info(f"[SAP-TILES] Found {tile_count} clickable tiles/cards")

        original_url = page.url
        seen = set()

        for i in range(min(tile_count, 20)):  # Cap at 20 to avoid infinite loops
            try:
                # Re-query each iteration (DOM may have changed after navigation)
                tile = await page.evaluate("""
                    (index) => {
                        const tiles = Array.from(document.querySelectorAll(
                            '.sapMGT, .sapFCard, .sapMGenericTile, ' +
                            '.sapMTile, .sapFCardHeader'
                        )).filter(t => {
                            const r = t.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        });
                        if (index < tiles.length) {
                            const t = tiles[index];
                            return {
                                id: t.id || '',
                                text: (t.textContent || '').trim().substring(0, 80),
                                tag: t.tagName,
                            };
                        }
                        return null;
                    }
                """, i)

                if not tile:
                    break

                tile_text = tile.get('text', '')
                # Skip tiles we've already processed
                if tile_text in seen:
                    continue
                seen.add(tile_text)

                # Click the tile
                try:
                    await page.evaluate("""
                        (index) => {
                            const tiles = Array.from(document.querySelectorAll(
                                '.sapMGT, .sapFCard, .sapMGenericTile, ' +
                                '.sapMTile, .sapFCardHeader'
                            )).filter(t => {
                                const r = t.getBoundingClientRect();
                                return r.width > 0 && r.height > 0;
                            });
                            if (index < tiles.length) {
                                tiles[index].click();
                            }
                        }
                    """, i)

                    # Wait for potential navigation
                    await asyncio.sleep(1.5)

                    new_url = page.url
                    if (new_url != original_url
                            and new_url != current_url
                            and base_origin in new_url
                            and new_url not in discovered):
                        discovered.append(new_url)
                        logger.debug(
                            f"[SAP-TILES] Tile '{tile_text[:30]}' → {new_url}"
                        )

                    # Navigate back to the original page
                    if page.url != original_url:
                        await page.goto(
                            original_url,
                            wait_until='domcontentloaded',
                            timeout=15000,
                        )
                        await asyncio.sleep(2.0)

                except PlaywrightTimeout:
                    # Navigation timed out — go back
                    if page.url != original_url:
                        await page.goto(
                            original_url,
                            wait_until='domcontentloaded',
                            timeout=15000,
                        )
                        await asyncio.sleep(1.5)
                except Exception as e:
                    logger.debug(f"[SAP-TILES] Tile click error (#{i}): {e}")
                    if page.url != original_url:
                        try:
                            await page.goto(
                                original_url,
                                wait_until='domcontentloaded',
                                timeout=15000,
                            )
                            await asyncio.sleep(1.5)
                        except Exception:
                            break

            except Exception:
                continue

        if discovered:
            logger.info(
                f"[SAP-TILES] Discovered {len(discovered)} new routes: "
                + ', '.join(d.split('/')[-1] or d for d in discovered[:8])
            )
    except Exception as e:
        logger.debug(f"[SAP-TILES] Discovery error: {e}")

    return discovered


async def extract_sap_card_content(page: Page) -> str:
    """Extract text content from SAP UI5 Cards and Tiles.

    SAP for Me dashboard pages use Integration Cards (`.sapFCard`) and
    GenericTiles (`.sapMGT`) which may not be reached by standard
    innerText extraction if they're inside shadow roots or custom
    rendering containers.

    Returns:
        Combined text from all visible cards/tiles.
    """
    try:
        return await page.evaluate("""
            () => {
                const parts = [];
                const seen = new Set();

                // Card headers and content
                document.querySelectorAll(
                    '.sapFCard, .sapMGT, .sapMGenericTile'
                ).forEach(card => {
                    const text = (card.innerText || '').trim();
                    if (text && text.length > 3 && !seen.has(text)) {
                        seen.add(text);
                        parts.push(text);
                    }
                });

                // Object page sections
                document.querySelectorAll(
                    '.sapUxAPObjectPageSection, ' +
                    '.sapUxAPObjectPageSubSection'
                ).forEach(section => {
                    const text = (section.innerText || '').trim();
                    if (text && text.length > 10 && !seen.has(text)) {
                        seen.add(text);
                        parts.push(text);
                    }
                });

                // Dynamic page content sections
                document.querySelectorAll(
                    '.sapFDynamicPageContent .sapMFlexBox, ' +
                    '.sapFDynamicPageContent .sapMVBox'
                ).forEach(box => {
                    const text = (box.innerText || '').trim();
                    if (text && text.length > 10 && !seen.has(text)) {
                        seen.add(text);
                        parts.push(text);
                    }
                });

                return parts.join('\\n\\n');
            }
        """)
    except Exception as e:
        logger.debug(f"[SAP-CARDS] Content extraction error: {e}")
        return ''


def get_sap_content_selectors() -> List[str]:
    """Return SAP-specific content selectors for extraction."""
    return list(_SAP_CONTENT_SELECTORS)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

async def _find_sap_table(page: Page) -> str:
    """Auto-detect the primary SAP table container on the page."""
    for sel in _SAP_TABLE_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return sel
        except Exception:
            continue
    return ""


async def _count_table_rows(page: Page, container_selector: str) -> int:
    """Count visible rows in a SAP table container."""
    total = 0
    for row_sel in _SAP_ROW_SELECTORS:
        try:
            count = await page.evaluate(f"""
                () => {{
                    const container = document.querySelector('{container_selector}');
                    if (!container) return 0;
                    return container.querySelectorAll('{row_sel}').length;
                }}
            """)
            total = max(total, count)
        except Exception:
            continue
    return total


async def _extract_single_table(page: Page, element) -> Dict:
    """Extract data from a single SAP UI5 table element."""
    try:
        data = await element.evaluate("""
            (el) => {
                const result = { headers: [], rows: [] };

                // Extract headers
                const headerCells = el.querySelectorAll(
                    '.sapMListTblHeaderCell, .sapUiTableColHdr th, ' +
                    '.sapMColumnHeader, th'
                );
                headerCells.forEach(cell => {
                    const text = cell.textContent?.trim() || '';
                    if (text) result.headers.push(text);
                });

                // Extract rows
                const rows = el.querySelectorAll(
                    '.sapMLIB, .sapUiTableRow, .sapMListTblRow, tr'
                );
                rows.forEach(row => {
                    const cells = row.querySelectorAll(
                        '.sapMListTblCell, .sapUiTableCell, td'
                    );
                    if (cells.length > 0) {
                        const rowData = [];
                        cells.forEach(cell => {
                            rowData.push(cell.textContent?.trim() || '');
                        });
                        // Filter out empty rows
                        if (rowData.some(c => c)) {
                            result.rows.push(rowData);
                        }
                    }
                });

                return result;
            }
        """)
        return data
    except Exception as e:
        logger.debug(f"[SAP-UI5] Table extraction error: {e}")
        return {}


async def _extract_html_table(element) -> Dict:
    """Extract data from a standard HTML table element."""
    try:
        data = await element.evaluate("""
            (el) => {
                const result = { headers: [], rows: [] };

                // Headers from thead > tr > th
                const ths = el.querySelectorAll('thead th, tr:first-child th');
                ths.forEach(th => {
                    const text = th.textContent?.trim() || '';
                    if (text) result.headers.push(text);
                });

                // Rows from tbody > tr > td
                const trs = el.querySelectorAll('tbody tr, tr');
                trs.forEach(tr => {
                    const tds = tr.querySelectorAll('td');
                    if (tds.length > 0) {
                        const row = [];
                        tds.forEach(td => {
                            row.push(td.textContent?.trim() || '');
                        });
                        if (row.some(c => c)) {
                            result.rows.push(row);
                        }
                    }
                });

                return result;
            }
        """)
        return data
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# SAP Help Center (help.sap.com) — static HTML extraction
# ---------------------------------------------------------------------------
# SAP Help pages are traditional server-rendered HTML, NOT UI5 SPAs.
# They use `javascript:call_link('filename.htm')` for navigation,
# `span.qtextgrey`/`span.qtext` for code blocks, and `span.h2` for titles.
# BeautifulSoup handles these pages much better than Playwright.
# ---------------------------------------------------------------------------

def is_sap_help_url(url: str) -> bool:
    """Check if a URL is an SAP Help Center page (static HTML docs)."""
    return 'help.sap.com' in url.lower()


def extract_sap_help_links(html: str, base_url: str) -> List[str]:
    """Extract links from SAP Help pages, including javascript:call_link() refs.

    SAP Help Center uses `javascript:call_link('filename.htm')` instead
    of normal anchor hrefs. This function extracts those filenames and
    converts them to absolute URLs.
    """
    links: List[str] = []
    seen: set = set()

    # Pattern 1: javascript:call_link('filename.htm')
    js_pattern = re.compile(
        r"javascript:call_link\(['\"]([^'\"]+\.htm)['\"]", re.IGNORECASE
    )
    for match in js_pattern.finditer(html):
        filename = match.group(1)
        abs_url = urljoin(base_url, filename)
        if abs_url not in seen:
            seen.add(abs_url)
            links.append(abs_url)

    # Pattern 2: Regular href links to .htm files
    href_pattern = re.compile(r'href=["\']([^"\']*\.htm(?:l)?)["\']', re.IGNORECASE)
    for match in href_pattern.finditer(html):
        href = match.group(1)
        if href.startswith('javascript:'):
            continue
        abs_url = urljoin(base_url, href)
        if abs_url not in seen:
            seen.add(abs_url)
            links.append(abs_url)

    return links


def extract_sap_help_content(html: str, page_url: str) -> Dict:
    """Extract content from SAP Help static HTML pages using BeautifulSoup.

    Handles SAP Help page structure:
    - Title from span.h2 or h1
    - ABAP code blocks from span.qtextgrey / span.qtext / pre / code
    - Documentation text from the page body
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("[SAP-HELP] BeautifulSoup not available")
        return {}

    soup = BeautifulSoup(html, 'lxml' if _has_lxml() else 'html.parser')

    # ── Title extraction ──
    title = ""
    # Try <title> first (most reliable on SAP Help)
    t = soup.find('title')
    if t:
        title = t.get_text(strip=True)
    # Then try h1
    if not title:
        h1 = soup.find('h1')
        if h1:
            title = h1.get_text(strip=True)
    # Then try span.h2 (SAP Help uses these for section headings)
    h2_spans = soup.find_all('span', class_='h2')
    if not title and h2_spans:
        for h2 in reversed(h2_spans):
            text = h2.get_text(strip=True)
            # Skip single-char alphabet section headers and boilerplate
            if text and len(text) > 3 and text not in (
                'Description', 'Note', 'Source Code', 'Example'
            ):
                title = text
                break

    # ── Code block extraction ──
    code_blocks: List[str] = []

    for class_name in ('qtextgrey', 'qtext'):
        for span in soup.find_all('span', class_=class_name):
            code_html = str(span)
            code_html = re.sub(r'<br\s*/?>', '\n', code_html)
            code_html = code_html.replace('&nbsp;', ' ')
            code_html = code_html.replace('\xa0', ' ')  # non-breaking space
            code_html = code_html.replace('&lt;', '<')
            code_html = code_html.replace('&gt;', '>')
            code_html = code_html.replace('&amp;', '&')
            code_html = code_html.replace('&quot;', '"')
            code_html = re.sub(r'<[^>]+>', '', code_html)
            code = code_html.strip()
            if code and len(code) > 30:
                code_blocks.append(code)

    if not code_blocks:
        for pre in soup.find_all('pre'):
            code = pre.get_text()
            if code.strip() and len(code.strip()) > 30:
                code_blocks.append(code.strip())
        for code_tag in soup.find_all('code'):
            code = code_tag.get_text()
            if code.strip() and len(code.strip()) > 30:
                code_blocks.append(code.strip())

    # ── Text content extraction ──
    for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'noscript']):
        tag.decompose()

    main = None
    for sel in ['#main-content', '.content-area', 'main', 'article',
                '#content', 'body']:
        main = soup.select_one(sel)
        if main:
            break

    text_content = ""
    if main:
        text_content = main.get_text(separator='\n', strip=True)
        text_content = re.sub(r'\n{3,}', '\n\n', text_content)
        text_content = re.sub(r' {2,}', ' ', text_content)

    # ── Headings ──
    headings: Dict[str, List[str]] = {}
    for lvl in range(1, 7):
        hs = soup.find_all(f'h{lvl}')
        if hs:
            headings[f'h{lvl}'] = [
                h.get_text(strip=True) for h in hs if h.get_text(strip=True)
            ]
    if h2_spans:
        h2_texts = [h.get_text(strip=True) for h in h2_spans
                     if h.get_text(strip=True)]
        if 'h2' in headings:
            headings['h2'].extend(h2_texts)
        else:
            headings['h2'] = h2_texts

    # Append code blocks to text_content
    if code_blocks:
        code_section = "\n\n--- Code ---\n\n" + "\n\n".join(code_blocks)
        text_content = (text_content + code_section).strip()

    return {
        'title': title,
        'text': text_content,
        'code_blocks': code_blocks,
        'headings': headings,
    }


def _has_lxml() -> bool:
    """Check if lxml is available."""
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False

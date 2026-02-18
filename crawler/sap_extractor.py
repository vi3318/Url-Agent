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

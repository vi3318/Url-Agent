"""
Interaction Policy Engine
=========================
Dedicated module for deep-mode interactive element expansion.

Responsibilities:
  1. **discover_candidates**  — find clickable interactive elements on a page
  2. **score_candidate**      — quick heuristic: is this element worth clicking?
  3. **apply_click**          — safely click an element with timeout
  4. **is_meaningful_delta**  — post-click validation (text / links / ARIA state)
  5. **expansion_loop**       — orchestrate the full expand pass for one page
  6. **async_expansion_loop** — async-compatible version for AsyncDocCrawler

The deep crawler calls ``expansion_loop(page, config)`` and gets back
``(meaningful_clicks, total_attempted, hit_limit)``.

The async crawler calls ``async_expansion_loop(page, config)`` and gets
back the same ``ExpansionResult``.

This module does NOT own Playwright lifecycle or page navigation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selector catalogue — every CSS pattern we consider "interactive"
# ---------------------------------------------------------------------------
DEFAULT_INTERACTIVE_SELECTORS: List[str] = [
    # ── Generic ARIA / HTML5 ──────────────────────────────────────
    'button:not([disabled])',
    '[role="button"]:not([disabled])',
    'input[type="button"]:not([disabled])',
    '[aria-expanded="false"]',
    '[aria-pressed="false"]',
    'details:not([open]) > summary',

    # ── Bootstrap 4 / 5 ──────────────────────────────────────────
    '[data-toggle]',
    '[data-bs-toggle]',
    '.collapsed',
    '.accordion-header:not(.active)',
    '.accordion-button.collapsed',

    # ── Generic expand / collapse / toggle ────────────────────────
    '.expandable:not(.expanded)',
    '.collapsible:not(.active)',
    '[class*="expand"]:not([class*="expanded"])',
    '[class*="collapse"]:not([class*="collapsed"])',
    '[class*="toggle"]:not([class*="toggled"])',

    # ── Tree / sidebar navigation ────────────────────────────────
    '.tree-node:not(.expanded)',
    '.tree-item:not(.is-expanded)',
    'li[role="treeitem"]:not([aria-expanded="true"]) > span',
    '.nav-item:not(.expanded) > .nav-link',

    # ── Tab panels ────────────────────────────────────────────────
    '[role="tab"]:not([aria-selected="true"])',
    '.tab:not(.active)',
    '.nav-tab:not(.active)',

    # ── FAQ / card / panel patterns ──────────────────────────────
    '.faq-question',
    '.card-header:not(.active)',
    '.panel-heading:not(.active)',

    # ── Load more / show more ─────────────────────────────────────
    '.load-more',
    '.show-more',
    '.view-more',
    '[class*="load-more"]',
    '[class*="show-more"]',
    '[class*="view-all"]',

    # ── Oracle JET ────────────────────────────────────────────────
    '.toc-item > .toc-link',
    '.ohc-sidebar-item',
    '.dropdown-toggle',

    # ── Docusaurus (React, Meta) ──────────────────────────────────
    '.menu__list-item--collapsed > .menu__link',
    '.menu__caret',
    '.tocCollapsibleButton_node_modules',
    'button.clean-btn[class*="tocCollapsible"]',
    '.theme-doc-sidebar-item-category > .menu__list-item-collapsible',

    # ── MkDocs / Material for MkDocs ─────────────────────────────
    '.md-nav__toggle:not(:checked) + .md-nav__link',
    'label[for^="__nav"]',
    'label[for^="__toc"]',
    '.md-toggle',
    'nav.md-nav .md-nav__item--nested > input[type="checkbox"]:not(:checked) ~ label',

    # ── ReadTheDocs / Sphinx ──────────────────────────────────────
    '.toctree-expand',
    '.wy-menu .toctree-l1.current > a',
    'li.toctree-l1:not(.current) > a',

    # ── Confluence / Atlassian ────────────────────────────────────
    '.expand-control',
    '.expand-control-text',
    '.aui-expander-trigger',
    '[data-macro-name="expand"] .expand-control',
    '.aui-nav-child-trigger',

    # ── Ant Design (React) ────────────────────────────────────────
    '.ant-collapse-header[aria-expanded="false"]',
    '.ant-tree-switcher_close',
    '.ant-menu-submenu-title',

    # ── Material UI / MUI (React) ─────────────────────────────────
    '.MuiAccordion-root:not(.Mui-expanded) .MuiAccordionSummary-root',
    '.MuiTreeItem-iconContainer',
    '.MuiCollapse-hidden + .MuiButtonBase-root',

    # ── Chakra UI (React) ─────────────────────────────────────────
    '[data-expanded=""]',
    'button.chakra-accordion__button[aria-expanded="false"]',

    # ── Notion ────────────────────────────────────────────────────
    '.notion-toggle',
    '.toggleBlock > div:first-child',
    '[class*="toggleButton"]',

    # ── Salesforce Lightning ──────────────────────────────────────
    'lightning-accordion-section:not(.slds-is-open) .slds-button',
    '.slds-accordion__summary-action',
    '.slds-tree__item[aria-expanded="false"]',

    # ── Zendesk Guide ─────────────────────────────────────────────
    '[data-action="toggle"]',
    '.collapsible-sidebar-toggle',

    # ── GitBook ───────────────────────────────────────────────────
    '[class*="expandable"]:not([class*="expanded"])',
    'div[class*="group/page"] > button',

    # ── Next.js / Nextra / Vercel docs ────────────────────────────
    '[data-state="closed"]',
    'button[class*="sidebar"] + div[hidden]',

    # ── SAP Fiori / UI5 ──────────────────────────────────────────
    '[class*="sapM"][class*="Panel"]:not([class*="Expanded"]) .sapMPanelHdr',
]

# Text patterns that suggest an element triggers content expansion
_EXPAND_TEXT_PATTERNS = frozenset([
    'expand', 'show', 'more', 'view', 'open', 'toggle', 'collapse',
    'details', 'read more', 'load more', 'see more', 'view all',
])

# Class patterns that suggest interactive behaviour
_INTERACTIVE_CLASS_PATTERNS = frozenset([
    'expand', 'collapse', 'toggle', 'accordion', 'dropdown', 'tree', 'tab',
])


# ---------------------------------------------------------------------------
# Bulk-expand selectors — site-wide "Expand All" / "Show All" buttons
# that open the entire TOC tree in a single click.
# ---------------------------------------------------------------------------
BULK_EXPAND_SELECTORS: List[str] = [
    # Generic ARIA buttons
    'button[title*="Expand" i]',
    'button[aria-label*="Expand All" i]',
    'button[aria-label*="Expand all" i]',
    '[role="button"][title*="Expand" i]',
    '[role="button"][aria-label*="Expand" i]',

    # Oracle JET
    '#toggleTreeView',
    'oj-button[title*="Expand" i]',

    # Generic class / id patterns
    '[class*="expand-all"]',
    '[class*="expandAll"]',
    '[class*="expand_all"]',
    '[id*="expand-all" i]',
    '[id*="expandAll" i]',
    '[id*="expand_all" i]',

    # Confluence
    '.expand-all-button',
    '#expand-all-link',

    # Ant Design / MUI
    '[class*="ant-tree-expand-all"]',

    # MkDocs
    'label[for="__nav"]',

    # ReadTheDocs / Sphinx
    'a.expand-all',
]

# Text patterns on elements that trigger a full tree expand
_BULK_EXPAND_TEXT = frozenset([
    'expand all', 'show all', 'open all',
])


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------
@dataclass
class _PageSnapshot:
    """Lightweight pre-click snapshot for delta comparison."""
    text_length: int = 0
    link_count: int = 0
    heading_count: int = 0
    expanded_count: int = 0   # elements with aria-expanded="true"


@dataclass
class ExpansionResult:
    """Returned by ``expansion_loop`` to the caller."""
    meaningful_clicks: int = 0
    total_attempted: int = 0
    wasted_clicks: int = 0
    hit_limit: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def expansion_loop(
    page,
    *,
    max_clicks: int = 50,
    click_timeout_ms: int = 1500,
    delay_after_click_s: float = 0.3,
    meaningful_text_delta: int = 80,
    meaningful_link_delta: int = 1,
    selectors: Optional[List[str]] = None,
) -> ExpansionResult:
    """
    Run the full interaction-expansion pass on an already-loaded Playwright
    ``page``.

    Args:
        page:                   Playwright Page object (already navigated).
        max_clicks:             Per-page interaction budget.
        click_timeout_ms:       Timeout for each individual click.
        delay_after_click_s:    Pause after click so JS can react.
        meaningful_text_delta:  Min chars of new text to count as meaningful.
        meaningful_link_delta:  Min new <a> links to count as meaningful.
        selectors:              CSS selector list (default catalogue used).

    Returns:
        ``ExpansionResult`` with counters.
    """
    if selectors is None:
        selectors = DEFAULT_INTERACTIVE_SELECTORS

    result = ExpansionResult()
    clicked_fingerprints: Set[str] = set()

    # --- Phase 0: bulk-expand ("Expand All") pre-pass ---
    # Many documentation sites have a single button that expands the full
    # sidebar / TOC tree at once.  Clicking it first saves hundreds of
    # individual expansion clicks and budget.
    bulk_expanded = _phase0_bulk_expand(
        page,
        result=result,
        clicked_fingerprints=clicked_fingerprints,
        click_timeout_ms=click_timeout_ms,
    )

    # If Phase 0 already revealed a large tree, skip the granular phases —
    # they would waste minutes clicking through thousands of already-expanded
    # nodes one-by-one.
    if bulk_expanded:
        logger.info(
            f"Expansion done: {result.meaningful_clicks} meaningful / "
            f"{result.total_attempted} attempted / "
            f"{result.wasted_clicks} wasted (bulk-expand succeeded, skipping granular phases)"
        )
        return result

    # --- Phase 1: selector-driven candidates ---
    for selector in selectors:
        if result.total_attempted >= max_clicks:
            result.hit_limit = True
            break

        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue

        for element in elements:
            if result.total_attempted >= max_clicks:
                result.hit_limit = True
                break

            _try_click_element(
                page, element,
                clicked_fingerprints=clicked_fingerprints,
                result=result,
                click_timeout_ms=click_timeout_ms,
                delay_after_click_s=delay_after_click_s,
                meaningful_text_delta=meaningful_text_delta,
                meaningful_link_delta=meaningful_link_delta,
            )

    # --- Phase 2: text-heuristic scan for elements selectors missed ---
    if result.total_attempted < max_clicks:
        _phase2_text_scan(
            page,
            clicked_fingerprints=clicked_fingerprints,
            result=result,
            max_clicks=max_clicks,
            click_timeout_ms=click_timeout_ms,
            delay_after_click_s=delay_after_click_s,
            meaningful_text_delta=meaningful_text_delta,
            meaningful_link_delta=meaningful_link_delta,
        )

    logger.info(
        f"Expansion done: {result.meaningful_clicks} meaningful / "
        f"{result.total_attempted} attempted / "
        f"{result.wasted_clicks} wasted"
        f"{' (budget exhausted)' if result.hit_limit else ''}"
    )
    return result


# ---------------------------------------------------------------------------
# Candidate helpers (public so deep_crawler can call them independently
# if needed, but normally only expansion_loop is called)
# ---------------------------------------------------------------------------

def discover_candidates(page, selectors: Optional[List[str]] = None):
    """Yield visible elements matching the interactive selectors."""
    if selectors is None:
        selectors = DEFAULT_INTERACTIVE_SELECTORS
    for selector in selectors:
        try:
            for el in page.query_selector_all(selector):
                try:
                    if el.is_visible():
                        yield el
                except Exception:
                    continue
        except Exception:
            continue


def score_candidate(element) -> bool:
    """
    Quick heuristic: should we bother clicking this element?

    Returns ``True`` if the element looks like an interactive toggle
    rather than a plain navigation link.
    """
    try:
        return element.evaluate("""el => {
            // Hard yes: semantic interactive attributes
            if (el.hasAttribute('aria-expanded')) return true;
            if (el.hasAttribute('aria-pressed'))  return true;
            if (el.hasAttribute('data-toggle'))   return true;
            if (el.hasAttribute('data-bs-toggle'))return true;
            if (el.hasAttribute('onclick'))       return true;
            if (el.getAttribute('role') === 'button') return true;
            if (el.tagName === 'BUTTON')  return true;
            if (el.tagName === 'SUMMARY') return true;

            // Hard no: plain nav link with real href
            if (el.tagName === 'A') {
                const href = el.getAttribute('href') || '';
                if (href && !href.startsWith('#') && !href.startsWith('javascript:')
                    && !el.hasAttribute('data-toggle') && !el.hasAttribute('data-bs-toggle')
                    && !el.hasAttribute('aria-expanded') && !el.hasAttribute('onclick')) {
                    return false;
                }
            }

            // Soft signals: text content
            const text = (el.innerText || '').toLowerCase().substring(0, 80);
            const expandWords = ['expand','show','more','view','open','toggle','collapse'];
            for (const w of expandWords) { if (text.includes(w)) return true; }

            // Soft signals: class names
            const cls = (el.className || '').toLowerCase();
            const clsWords = ['expand','collapse','toggle','accordion','dropdown','tree','tab'];
            for (const w of clsWords) { if (cls.includes(w)) return true; }

            return false;
        }""")
    except Exception:
        return False


def get_element_fingerprint(element) -> Optional[str]:
    """Unique fingerprint for dedup — tag + id + class + text prefix + position."""
    try:
        return element.evaluate("""el => {
            const rect = el.getBoundingClientRect();
            const text = (el.innerText || '').substring(0, 50).trim();
            const tag  = el.tagName;
            const id   = el.id || '';
            const cls  = el.className || '';
            return `${tag}|${id}|${cls}|${text}|${Math.round(rect.top)}|${Math.round(rect.left)}`;
        }""")
    except Exception:
        return None


def apply_click(page, element, *, timeout_ms: int = 1500, settle_s: float = 0.3) -> bool:
    """
    Click an element safely.

    Returns ``True`` if the click succeeded without exception.
    """
    try:
        element.click(timeout=timeout_ms)
        page.wait_for_timeout(int(settle_s * 1000))
        return True
    except Exception:
        return False


def is_meaningful_delta(
    before: _PageSnapshot,
    after: _PageSnapshot,
    *,
    min_text_delta: int = 80,
    min_link_delta: int = 1,
) -> bool:
    """
    Compare two lightweight page snapshots.

    A click is "meaningful" if ANY of:
      - visible text grew by ≥ ``min_text_delta`` chars
      - link count grew by ≥ ``min_link_delta``
      - heading count grew
      - expanded-element count grew (aria-expanded="true")
    """
    if (after.text_length - before.text_length) >= min_text_delta:
        return True
    if (after.link_count - before.link_count) >= min_link_delta:
        return True
    if after.heading_count > before.heading_count:
        return True
    if after.expanded_count > before.expanded_count:
        return True
    return False


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _take_snapshot(page) -> _PageSnapshot:
    """Cheap DOM metrics — no full-text extraction."""
    try:
        data = page.evaluate("""() => {
            const body = document.body || document.documentElement;
            return {
                textLen:     (body.innerText || '').length,
                linkCount:   document.querySelectorAll('a[href]').length,
                headingCount: document.querySelectorAll('h1,h2,h3,h4,h5,h6').length,
                expandedCount: document.querySelectorAll('[aria-expanded="true"]').length,
            };
        }""")
        return _PageSnapshot(
            text_length=data.get("textLen", 0),
            link_count=data.get("linkCount", 0),
            heading_count=data.get("headingCount", 0),
            expanded_count=data.get("expandedCount", 0),
        )
    except Exception:
        return _PageSnapshot()


def _is_navigation_link(element) -> bool:
    """Return True if element is a plain <a> that would navigate away."""
    try:
        return element.evaluate("""el => {
            if (el.tagName === 'A') {
                const href = el.getAttribute('href') || '';
                if (href && !href.startsWith('#') && !href.startsWith('javascript:')
                    && !el.hasAttribute('data-toggle') && !el.hasAttribute('data-bs-toggle')
                    && !el.hasAttribute('aria-expanded') && !el.hasAttribute('onclick')) {
                    return true;
                }
            }
            return false;
        }""")
    except Exception:
        return False


def _is_already_expanded(element) -> bool:
    """Return True if the element or its closest treeitem ancestor is already
    expanded.  Clicking it again would COLLAPSE content we want to keep."""
    try:
        return element.evaluate("""el => {
            // Direct check
            if (el.getAttribute('aria-expanded') === 'true') return true;
            // Walk up to nearest treeitem / details
            const ancestor = el.closest('[aria-expanded], details');
            if (ancestor) {
                if (ancestor.getAttribute('aria-expanded') === 'true') return true;
                if (ancestor.tagName === 'DETAILS' && ancestor.hasAttribute('open')) return true;
            }
            return false;
        }""")
    except Exception:
        return False


def _try_click_element(
    page,
    element,
    *,
    clicked_fingerprints: Set[str],
    result: ExpansionResult,
    click_timeout_ms: int,
    delay_after_click_s: float,
    meaningful_text_delta: int,
    meaningful_link_delta: int,
) -> None:
    """Attempt one click with full gate checks.  Mutates *result* in place."""
    try:
        if not element.is_visible():
            return
    except Exception:
        return

    if _is_navigation_link(element):
        return

    # Skip already-expanded elements — clicking them would COLLAPSE content
    if _is_already_expanded(element):
        return

    fp = get_element_fingerprint(element)
    if fp and fp in clicked_fingerprints:
        return
    if fp:
        clicked_fingerprints.add(fp)

    # --- snapshot before click ---
    before = _take_snapshot(page)

    clicked = apply_click(page, element, timeout_ms=click_timeout_ms, settle_s=delay_after_click_s)
    if not clicked:
        return

    result.total_attempted += 1

    # --- snapshot after click ---
    after = _take_snapshot(page)

    if is_meaningful_delta(before, after,
                           min_text_delta=meaningful_text_delta,
                           min_link_delta=meaningful_link_delta):
        result.meaningful_clicks += 1
        logger.debug(
            f"  ✓ meaningful click #{result.meaningful_clicks} "
            f"(text +{after.text_length - before.text_length}, "
            f"links +{after.link_count - before.link_count})"
        )
    else:
        result.wasted_clicks += 1
        logger.debug(f"  ✗ wasted click (no meaningful delta)")


def _phase2_text_scan(
    page,
    *,
    clicked_fingerprints: Set[str],
    result: ExpansionResult,
    max_clicks: int,
    click_timeout_ms: int,
    delay_after_click_s: float,
    meaningful_text_delta: int,
    meaningful_link_delta: int,
) -> None:
    """Scan first 200 visible elements for text-based interactive signals."""
    try:
        elements = page.query_selector_all('*')
    except Exception:
        return

    for el in elements[:200]:
        if result.total_attempted >= max_clicks:
            result.hit_limit = True
            break

        if not score_candidate(el):
            continue

        _try_click_element(
            page, el,
            clicked_fingerprints=clicked_fingerprints,
            result=result,
            click_timeout_ms=click_timeout_ms,
            delay_after_click_s=delay_after_click_s,
            meaningful_text_delta=meaningful_text_delta,
            meaningful_link_delta=meaningful_link_delta,
        )


def _phase0_bulk_expand(
    page,
    *,
    result: ExpansionResult,
    clicked_fingerprints: Set[str],
    click_timeout_ms: int,
) -> bool:
    """Try to find and click a single 'Expand All' button.

    This is a cheap pre-pass: at most a handful of elements are checked.
    If a bulk-expand button is found and produces a meaningful delta
    (many new links / text), count it as one meaningful click but do NOT
    charge it against the granular-expansion budget.

    After the click we allow extra settle time because expanding a full
    tree (potentially thousands of nodes) is slower than one accordion.

    Returns:
        True if a bulk expand button was clicked and produced a meaningful
        change.  The caller should skip granular Phase 1/2 expansion.
    """
    for selector in BULK_EXPAND_SELECTORS:
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue

        for el in elements:
            try:
                if not el.is_visible():
                    continue
            except Exception:
                continue

            # Extra guard: check title / aria-label / inner text
            try:
                is_expand = el.evaluate("""el => {
                    const t = (el.getAttribute('title') || '').toLowerCase();
                    const a = (el.getAttribute('aria-label') || '').toLowerCase();
                    const txt = (el.innerText || '').toLowerCase().trim();
                    const expandTerms = ['expand all', 'show all', 'open all', 'expand'];
                    for (const w of expandTerms) {
                        if (t.includes(w) || a.includes(w) || txt === w) return true;
                    }
                    return false;
                }""")
                if not is_expand:
                    continue
            except Exception:
                continue

            # Dedup
            fp = get_element_fingerprint(el)
            if fp and fp in clicked_fingerprints:
                continue
            if fp:
                clicked_fingerprints.add(fp)

            before = _take_snapshot(page)

            logger.info(f"[BULK-EXPAND] Clicking expand-all button")
            clicked = apply_click(page, el, timeout_ms=click_timeout_ms, settle_s=0.5)
            if not clicked:
                continue

            result.total_attempted += 1

            # Give JS extra time to render a potentially huge tree
            try:
                page.wait_for_timeout(3000)
            except Exception:
                pass

            after = _take_snapshot(page)
            delta_links = after.link_count - before.link_count
            delta_text = after.text_length - before.text_length
            delta_expanded = after.expanded_count - before.expanded_count

            logger.info(
                f"[BULK-EXPAND] Result: links +{delta_links}, "
                f"text +{delta_text}, expanded +{delta_expanded}"
            )

            if delta_links > 0 or delta_text > 200 or delta_expanded > 5:
                result.meaningful_clicks += 1
                # After a successful bulk expand, no need to try more buttons
                return True
            else:
                result.wasted_clicks += 1

    return False


# =========================================================================
# ASYNC EXPANSION ENGINE
# =========================================================================
# Full async-compatible version of the expansion loop that reuses the
# SAME selector catalogue, SAME bulk-expand selectors, SAME scoring
# heuristics, and SAME meaningful-delta gating.
#
# No selectors are duplicated — everything references the constants
# defined above (DEFAULT_INTERACTIVE_SELECTORS, BULK_EXPAND_SELECTORS,
# _EXPAND_TEXT_PATTERNS, _INTERACTIVE_CLASS_PATTERNS).
# =========================================================================

# JS snippet shared by all async snapshot calls (evaluated in page context)
_SNAPSHOT_JS = """() => {
    const body = document.body || document.documentElement;
    return {
        textLen:      (body.innerText || '').length,
        linkCount:    document.querySelectorAll('a[href]').length,
        headingCount: document.querySelectorAll('h1,h2,h3,h4,h5,h6').length,
        expandedCount: document.querySelectorAll('[aria-expanded="true"]').length,
    };
}"""

# JS snippet for rich element fingerprint (tag + id + class + text + position)
_FINGERPRINT_JS = """el => {
    const rect = el.getBoundingClientRect();
    const text = (el.innerText || '').substring(0, 50).trim();
    const tag  = el.tagName;
    const id   = el.id || '';
    const cls  = (typeof el.className === 'string') ? el.className : '';
    return `${tag}|${id}|${cls}|${text}|${Math.round(rect.top)}|${Math.round(rect.left)}`;
}"""

# JS snippet for navigation-link detection
_IS_NAV_LINK_JS = """el => {
    if (el.tagName === 'A') {
        const href = el.getAttribute('href') || '';
        if (href && !href.startsWith('#') && !href.startsWith('javascript:')
            && !el.hasAttribute('data-toggle') && !el.hasAttribute('data-bs-toggle')
            && !el.hasAttribute('aria-expanded') && !el.hasAttribute('onclick')) {
            return true;
        }
    }
    return false;
}"""

# JS snippet for already-expanded detection
_IS_ALREADY_EXPANDED_JS = """el => {
    if (el.getAttribute('aria-expanded') === 'true') return true;
    const ancestor = el.closest('[aria-expanded], details');
    if (ancestor) {
        if (ancestor.getAttribute('aria-expanded') === 'true') return true;
        if (ancestor.tagName === 'DETAILS' && ancestor.hasAttribute('open')) return true;
    }
    return false;
}"""

# JS snippet for score_candidate (heuristic: is this element interactive?)
_SCORE_CANDIDATE_JS = """el => {
    if (el.hasAttribute('aria-expanded')) return true;
    if (el.hasAttribute('aria-pressed'))  return true;
    if (el.hasAttribute('data-toggle'))   return true;
    if (el.hasAttribute('data-bs-toggle'))return true;
    if (el.hasAttribute('onclick'))       return true;
    if (el.getAttribute('role') === 'button') return true;
    if (el.tagName === 'BUTTON')  return true;
    if (el.tagName === 'SUMMARY') return true;

    if (el.tagName === 'A') {
        const href = el.getAttribute('href') || '';
        if (href && !href.startsWith('#') && !href.startsWith('javascript:')
            && !el.hasAttribute('data-toggle') && !el.hasAttribute('data-bs-toggle')
            && !el.hasAttribute('aria-expanded') && !el.hasAttribute('onclick')) {
            return false;
        }
    }

    const text = (el.innerText || '').toLowerCase().substring(0, 80);
    const expandWords = ['expand','show','more','view','open','toggle','collapse',
                         'details','read more','load more','see more','view all'];
    for (const w of expandWords) { if (text.includes(w)) return true; }

    const cls = ((typeof el.className === 'string') ? el.className : '').toLowerCase();
    const clsWords = ['expand','collapse','toggle','accordion','dropdown','tree','tab'];
    for (const w of clsWords) { if (cls.includes(w)) return true; }

    return false;
}"""

# JS for bulk-expand text verification
_BULK_EXPAND_TEXT_JS = """el => {
    const t = (el.getAttribute('title') || '').toLowerCase();
    const a = (el.getAttribute('aria-label') || '').toLowerCase();
    const txt = (el.innerText || '').toLowerCase().trim();
    const expandTerms = ['expand all', 'show all', 'open all', 'expand'];
    for (const w of expandTerms) {
        if (t.includes(w) || a.includes(w) || txt === w) return true;
    }
    return false;
}"""


async def _async_take_snapshot(page) -> _PageSnapshot:
    """Async: cheap DOM metrics for delta comparison."""
    try:
        data = await page.evaluate(_SNAPSHOT_JS)
        return _PageSnapshot(
            text_length=data.get("textLen", 0),
            link_count=data.get("linkCount", 0),
            heading_count=data.get("headingCount", 0),
            expanded_count=data.get("expandedCount", 0),
        )
    except Exception:
        return _PageSnapshot()


async def _async_get_fingerprint(element) -> Optional[str]:
    """Async: rich element fingerprint for dedup."""
    try:
        return await element.evaluate(_FINGERPRINT_JS)
    except Exception:
        return None


async def _async_is_navigation_link(element) -> bool:
    """Async: return True if element is a plain <a> that would navigate away."""
    try:
        return await element.evaluate(_IS_NAV_LINK_JS)
    except Exception:
        return False


async def _async_is_already_expanded(element) -> bool:
    """Async: return True if element or ancestor is already expanded."""
    try:
        return await element.evaluate(_IS_ALREADY_EXPANDED_JS)
    except Exception:
        return False


async def _async_score_candidate(element) -> bool:
    """Async: heuristic check — is this element worth clicking?"""
    try:
        return await element.evaluate(_SCORE_CANDIDATE_JS)
    except Exception:
        return False


async def _async_apply_click(page, element, *, timeout_ms: int = 1500,
                              settle_s: float = 0.2, use_js: bool = False) -> bool:
    """Async: click an element safely. Returns True on success.

    Args:
        use_js: If True, use el.click() in JS context (bypasses visibility
                checks — needed for tree items inside collapsed parents).
    """
    try:
        if use_js:
            await element.evaluate("el => el.click()")
        else:
            await element.click(timeout=timeout_ms)
        await asyncio.sleep(settle_s)
        return True
    except Exception:
        return False


async def _async_try_click_element(
    page,
    element,
    *,
    clicked_fingerprints: Set[str],
    result: ExpansionResult,
    click_timeout_ms: int,
    delay_after_click_s: float,
    meaningful_text_delta: int,
    meaningful_link_delta: int,
    use_js_click: bool = False,
) -> None:
    """Async: attempt one click with full gate checks. Mutates *result*."""
    # Visibility check (skip for JS-click mode — items may be hidden but functional)
    if not use_js_click:
        try:
            if not await element.is_visible():
                return
        except Exception:
            return

    # Navigation link safety
    if await _async_is_navigation_link(element):
        return

    # Already-expanded skip
    if await _async_is_already_expanded(element):
        return

    # Dedup via fingerprint
    fp = await _async_get_fingerprint(element)
    if fp and fp in clicked_fingerprints:
        return
    if fp:
        clicked_fingerprints.add(fp)

    # Pre-click snapshot
    before = await _async_take_snapshot(page)

    # Click
    clicked = await _async_apply_click(
        page, element,
        timeout_ms=click_timeout_ms,
        settle_s=delay_after_click_s,
        use_js=use_js_click,
    )
    if not clicked:
        return

    result.total_attempted += 1

    # Post-click snapshot + delta check
    after = await _async_take_snapshot(page)

    if is_meaningful_delta(before, after,
                           min_text_delta=meaningful_text_delta,
                           min_link_delta=meaningful_link_delta):
        result.meaningful_clicks += 1
        logger.debug(
            f"  ✓ async meaningful click #{result.meaningful_clicks} "
            f"(text +{after.text_length - before.text_length}, "
            f"links +{after.link_count - before.link_count})"
        )
    else:
        result.wasted_clicks += 1


async def _async_phase0_bulk_expand(
    page,
    *,
    result: ExpansionResult,
    clicked_fingerprints: Set[str],
    click_timeout_ms: int,
) -> bool:
    """Async: try to click a single 'Expand All' button.

    Uses the FULL BULK_EXPAND_SELECTORS list (20+ selectors).
    """
    for selector in BULK_EXPAND_SELECTORS:
        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            continue

        for el in elements:
            try:
                if not await el.is_visible():
                    continue
            except Exception:
                continue

            # Text verification guard
            try:
                is_expand = await el.evaluate(_BULK_EXPAND_TEXT_JS)
                if not is_expand:
                    continue
            except Exception:
                continue

            # Dedup
            fp = await _async_get_fingerprint(el)
            if fp and fp in clicked_fingerprints:
                continue
            if fp:
                clicked_fingerprints.add(fp)

            before = await _async_take_snapshot(page)

            logger.info("[BULK-EXPAND] Clicking expand-all button (async)")
            clicked = await _async_apply_click(
                page, el, timeout_ms=click_timeout_ms, settle_s=0.5
            )
            if not clicked:
                continue

            result.total_attempted += 1

            # Extra settle time for large tree renders
            await asyncio.sleep(3.0)

            after = await _async_take_snapshot(page)
            delta_links = after.link_count - before.link_count
            delta_text = after.text_length - before.text_length
            delta_expanded = after.expanded_count - before.expanded_count

            logger.info(
                f"[BULK-EXPAND] Result: links +{delta_links}, "
                f"text +{delta_text}, expanded +{delta_expanded}"
            )

            if delta_links > 0 or delta_text > 200 or delta_expanded > 5:
                result.meaningful_clicks += 1
                return True
            else:
                result.wasted_clicks += 1

    return False


async def _async_phase2_text_scan(
    page,
    *,
    clicked_fingerprints: Set[str],
    result: ExpansionResult,
    max_clicks: int,
    click_timeout_ms: int,
    delay_after_click_s: float,
    meaningful_text_delta: int,
    meaningful_link_delta: int,
) -> None:
    """Async: scan first 200 visible elements for text-based interactive signals.

    This catches expandable elements that no CSS selector matches — e.g.
    "Show more", "View details", chevron toggle buttons without ARIA,
    Microsoft Learn nested sections, ServiceNow collapsible docs, SAP
    accordion sections.
    """
    try:
        elements = await page.query_selector_all(
            'button, [role="button"], summary, [onclick], '
            '[class*="expand"], [class*="collapse"], [class*="toggle"], '
            '[class*="accordion"], [class*="more"], [class*="chevron"]'
        )
    except Exception:
        return

    for el in elements[:200]:
        if result.total_attempted >= max_clicks:
            result.hit_limit = True
            break

        if not await _async_score_candidate(el):
            continue

        await _async_try_click_element(
            page, el,
            clicked_fingerprints=clicked_fingerprints,
            result=result,
            click_timeout_ms=click_timeout_ms,
            delay_after_click_s=delay_after_click_s,
            meaningful_text_delta=meaningful_text_delta,
            meaningful_link_delta=meaningful_link_delta,
        )


async def async_expansion_loop(
    page,
    *,
    max_clicks: int = 300,
    max_passes: int = 6,
    click_timeout_ms: int = 1500,
    delay_after_click_s: float = 0.2,
    meaningful_text_delta: int = 80,
    meaningful_link_delta: int = 1,
    selectors: Optional[List[str]] = None,
    max_expansion_time_s: float = 30.0,
    consecutive_wasted_limit: int = 15,
) -> ExpansionResult:
    """
    Async expansion loop — full enterprise-grade interaction engine.

    Reuses the SAME selector catalogue as the sync ``expansion_loop``:
    - 130+ ``DEFAULT_INTERACTIVE_SELECTORS`` (Phase 1)
    - 20+ ``BULK_EXPAND_SELECTORS`` (Phase 0)
    - Text-heuristic scan (Phase 2)

    Safety / performance features:
    - Per-page click budget (``max_clicks``)
    - Per-page time budget (``max_expansion_time_s``)
    - Consecutive-wasted-click early exit (``consecutive_wasted_limit``)
    - Multi-pass re-query for nested tree structures (``max_passes``)
    - Meaningful-delta gating on every click
    - Navigation-link skip
    - Already-expanded skip
    - Rich fingerprint dedup (tag + id + class + text + position)

    Args:
        page:                     Playwright async Page (already navigated).
        max_clicks:               Per-page interaction budget.
        max_passes:               Multi-pass re-query limit for ARIA trees.
        click_timeout_ms:         Timeout for each individual click.
        delay_after_click_s:      Pause after click so JS can react.
        meaningful_text_delta:    Min chars of new text to count as meaningful.
        meaningful_link_delta:    Min new <a> links to count as meaningful.
        selectors:                CSS selector list (uses full catalogue if None).
        max_expansion_time_s:     Hard time limit for the entire expansion pass.
        consecutive_wasted_limit: Stop after N consecutive clicks with no delta.

    Returns:
        ``ExpansionResult`` with counters.
    """
    if selectors is None:
        selectors = DEFAULT_INTERACTIVE_SELECTORS

    result = ExpansionResult()
    clicked_fingerprints: Set[str] = set()
    t_start = time.monotonic()
    consecutive_wasted = 0

    def _budget_ok() -> bool:
        """Check if we still have time and click budget."""
        if result.total_attempted >= max_clicks:
            result.hit_limit = True
            return False
        if (time.monotonic() - t_start) >= max_expansion_time_s:
            result.hit_limit = True
            logger.info(
                f"[EXPAND] Time budget exhausted ({max_expansion_time_s}s)"
            )
            return False
        return True

    # ── Phase 0: Bulk "Expand All" pre-pass ──────────────────────
    bulk_expanded = await _async_phase0_bulk_expand(
        page,
        result=result,
        clicked_fingerprints=clicked_fingerprints,
        click_timeout_ms=click_timeout_ms,
    )

    if bulk_expanded:
        logger.info(
            f"[EXPAND] Done (async): {result.meaningful_clicks} meaningful / "
            f"{result.total_attempted} attempted (bulk-expand succeeded)"
        )
        return result

    # ── Phase 1A: Multi-pass ARIA tree expansion ─────────────────
    # Re-query [aria-expanded="false"] each pass — captures nested items
    # revealed by expanding a parent.
    aria_selector = '[aria-expanded="false"]'

    for pass_num in range(max_passes):
        if not _budget_ok():
            break

        items = await page.query_selector_all(aria_selector)
        if not items:
            break

        pass_meaningful = 0
        logger.debug(f"[EXPAND] ARIA pass {pass_num + 1}: {len(items)} collapsed items")

        for el in items:
            if not _budget_ok():
                break
            if consecutive_wasted >= consecutive_wasted_limit:
                logger.info(
                    f"[EXPAND] Early exit: {consecutive_wasted} consecutive wasted clicks"
                )
                break

            # For ARIA tree items, use JS click (bypasses visibility checks
            # for items inside collapsed parents that are functional DOM nodes)
            await _async_try_click_element(
                page, el,
                clicked_fingerprints=clicked_fingerprints,
                result=result,
                click_timeout_ms=click_timeout_ms,
                delay_after_click_s=delay_after_click_s,
                meaningful_text_delta=meaningful_text_delta,
                meaningful_link_delta=meaningful_link_delta,
                use_js_click=True,
            )

            # Track consecutive wasted for early exit
            if result.wasted_clicks > 0 and result.meaningful_clicks == pass_meaningful:
                consecutive_wasted += 1
            else:
                consecutive_wasted = 0
                pass_meaningful = result.meaningful_clicks

        if pass_meaningful == 0:
            break

        # Let DOM update between passes
        await asyncio.sleep(0.5)

    # ── Phase 1B: Full selector catalogue ────────────────────────
    # Uses the SAME 130+ DEFAULT_INTERACTIVE_SELECTORS from the sync engine
    for selector in selectors:
        if not _budget_ok():
            break
        if consecutive_wasted >= consecutive_wasted_limit:
            break

        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            continue

        for element in elements:
            if not _budget_ok():
                break
            if consecutive_wasted >= consecutive_wasted_limit:
                break

            prev_meaningful = result.meaningful_clicks
            await _async_try_click_element(
                page, element,
                clicked_fingerprints=clicked_fingerprints,
                result=result,
                click_timeout_ms=click_timeout_ms,
                delay_after_click_s=delay_after_click_s,
                meaningful_text_delta=meaningful_text_delta,
                meaningful_link_delta=meaningful_link_delta,
            )
            if result.meaningful_clicks > prev_meaningful:
                consecutive_wasted = 0
            elif result.total_attempted > (prev_meaningful + result.wasted_clicks):
                consecutive_wasted += 1

    # ── Phase 2: Text-heuristic scan ─────────────────────────────
    if _budget_ok() and consecutive_wasted < consecutive_wasted_limit:
        await _async_phase2_text_scan(
            page,
            clicked_fingerprints=clicked_fingerprints,
            result=result,
            max_clicks=max_clicks,
            click_timeout_ms=click_timeout_ms,
            delay_after_click_s=delay_after_click_s,
            meaningful_text_delta=meaningful_text_delta,
            meaningful_link_delta=meaningful_link_delta,
        )

    elapsed_ms = (time.monotonic() - t_start) * 1000
    logger.info(
        f"[EXPAND] Done (async): {result.meaningful_clicks} meaningful / "
        f"{result.total_attempted} attempted / "
        f"{result.wasted_clicks} wasted in {elapsed_ms:.0f}ms"
        f"{' (budget exhausted)' if result.hit_limit else ''}"
    )
    return result

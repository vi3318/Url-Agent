"""
Async Crawler Engine
=====================
High-performance async Playwright crawler with worker pool.

Architecture:
- Single browser instance, single BrowserContext (shared cookies/session)
- asyncio.Queue for BFS frontier
- asyncio.Semaphore for concurrency control (5-8 workers)
- Resource blocking (images, fonts, media, analytics)
- Per-page timing via PerformanceMonitor
- RAG pipeline integration (transform on-the-fly)

Performance targets:
- 300 pages in <10 minutes (was 50 min with sync)
- Scalable to 5,000+ pages
- ~2-4 seconds/page effective (vs ~10s sync)

All existing features preserved:
- BFS crawling with strict subtree enforcement
- Shadow DOM extraction
- API response interception (FluidTopics)
- Cookie consent dismissal
- SPA content wait
- Auto-detect page complexity (JS vs HTML)
- Interactive element expansion
- Content quality gate
- Static fallback on timeout
- Scope widening on redirect
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urljoin
from pathlib import Path

from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .scope_filter import ScopeFilter
from .utils import URLNormalizer, ensure_joinable_base
from .monitor import PerformanceMonitor, PageTiming, CrawlMetrics
from .rag_model import RAGCorpus, RAGDocument
from .pipeline import transform_page, PipelineConfig
from . import interaction_policy

logger = logging.getLogger(__name__)

# Best-available HTML parser for BeautifulSoup (static fallback path)
try:
    from bs4 import BeautifulSoup
    try:
        import lxml  # noqa: F401
        _BS_PARSER = "lxml"
    except ImportError:
        _BS_PARSER = "html.parser"
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False
    _BS_PARSER = "html.parser"

# Resource types to block for speed
_BLOCKED_RESOURCE_TYPES = frozenset([
    "image", "media", "font",
])

# URL patterns for analytics/tracking scripts to block
_BLOCKED_URL_PATTERNS = [
    re.compile(r"google[-_]?analytics", re.IGNORECASE),
    re.compile(r"googletagmanager", re.IGNORECASE),
    re.compile(r"facebook\.net", re.IGNORECASE),
    re.compile(r"doubleclick\.net", re.IGNORECASE),
    re.compile(r"hotjar\.", re.IGNORECASE),
    re.compile(r"optimizely\.", re.IGNORECASE),
    re.compile(r"segment\.(com|io)", re.IGNORECASE),
    re.compile(r"mixpanel\.", re.IGNORECASE),
    re.compile(r"amplitude\.", re.IGNORECASE),
    re.compile(r"fullstory\.", re.IGNORECASE),
    re.compile(r"newrelic\.", re.IGNORECASE),
    re.compile(r"sentry\.io", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class AsyncCrawlConfig:
    """Configuration for the async crawler."""
    # Crawl limits
    max_pages: int = 300
    max_depth: int = 5
    timeout: int = 15000          # 15s per page (ms) — tighter than sync

    # Concurrency
    max_workers: int = 6          # simultaneous pages
    queue_maxsize: int = 10000    # prevent unbounded memory

    # Rate limiting
    delay_between_pages: float = 0.3   # seconds — much lower with async
    delay_after_click: float = 0.2

    # Per-page expansion limits
    max_clicks_per_page: int = 300
    max_expansion_passes: int = 6
    max_expansion_time_s: float = 30.0       # hard time limit per page expansion
    consecutive_wasted_limit: int = 15       # stop after N wasted clicks in a row

    # Browser
    headless: bool = True
    viewport_width: int = 1920
    viewport_height: int = 1080
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    # Resource blocking
    block_images: bool = True
    block_fonts: bool = True
    block_media: bool = True
    block_analytics: bool = True

    # Static fallback
    enable_static_fallback: bool = True

    # Content selectors (inherited from deep_crawler)
    content_selectors: List[str] = field(default_factory=lambda: [
        'main', 'article', '.content', '.main-content', '#content',
        '[role="main"]', '.documentation', '.doc-content',
        '.ohc-main-content', '.topic-content', '.theme-doc-markdown',
        '.markdown-section', '.md-content', '.rst-content', '.document',
        '.wiki-content', '.article-body', '.MuiContainer-root',
        '.chakra-container', '.slds-template__container',
        # Oracle / JS-heavy tree sites — the tree IS the content after
        # expanding all [aria-expanded="false"] items.
        '[role="tree"]',
        # FluidTopics (ServiceNow docs) content containers
        'ft-reader', 'ft-designed-page', 'ft-homepage', 'ft-search',
        '.FT-content', '.FT-reader', '[class*="FT-"]',
    ])
    exclude_selectors: List[str] = field(default_factory=lambda: [
        'nav', 'header', 'footer', '.sidebar', '.toc',
        '.breadcrumb', 'script', 'style', 'noscript',
    ])
    link_selectors: List[str] = field(default_factory=lambda: [
        'a[href]', '.toc-link[href]', '.nav-link[href]',
        '[role="treeitem"] a', '.menu__link[href]',
        '.toctree-l1 a[href]', '.toctree-l2 a[href]',
        '.md-nav__link[href]', '.expand-content a[href]', 'nav a[href]',
        # Oracle Help Center tree navigation
        '.ohc-tree a[href]', '.toc-tree a[href]', '.tree-node a[href]',
        '.ohc-sidebar a[href]', '.tree-item a[href]',
        # Generic tree / sidebar patterns
        '[role="tree"] a[href]', '[role="treeitem"] [href]',
        '.sidebar a[href]', '.left-nav a[href]', '.side-nav a[href]',
    ])

    # Scope filtering
    deny_patterns: List[str] = field(default_factory=list)
    strip_all_queries: bool = False

    # Built-in junk URL patterns
    builtin_deny_patterns: List[str] = field(default_factory=lambda: [
        r'/viewer/attachment/',
        r'/viewer/',
        r'/(de-DE|fr-FR|ko-KR|ja-JP|zh-CN|zh-TW|pt-BR|es-ES|it-IT|nl-NL|ru-RU|pl-PL|sv-SE|da-DK|fi-FI|nb-NO|cs-CZ|hu-HU|ro-RO|tr-TR|th-TH|he-IL|ar-SA|id-ID|ms-MY|vi-VN|uk-UA|el-GR|bg-BG|hr-HR|sk-SK|sl-SI|lt-LT|lv-LV|et-EE)/',
    ])

    # Content quality
    min_word_count: int = 10

    # Interactive selectors (None = use interaction_policy defaults)
    interactive_selectors: Optional[List[str]] = None

    # RAG pipeline
    pipeline_config: PipelineConfig = field(default_factory=PipelineConfig)


# ---------------------------------------------------------------------------
# Page data (internal)
# ---------------------------------------------------------------------------

@dataclass
class _PageResult:
    """Internal result from crawling a single page."""
    url: str = ""
    title: str = ""
    breadcrumb: List[str] = field(default_factory=list)
    section_path: List[str] = field(default_factory=list)
    headings: Dict[str, List[str]] = field(default_factory=dict)
    text_content: str = ""
    tables: List[Dict] = field(default_factory=list)
    code_blocks: List[str] = field(default_factory=list)
    internal_links: List[str] = field(default_factory=list)
    parent_url: str = ""
    depth: int = 0
    word_count: int = 0
    skipped: bool = False

    def to_dict(self) -> dict:
        return {
            'url': self.url, 'title': self.title,
            'breadcrumb': self.breadcrumb, 'section_path': self.section_path,
            'headings': self.headings, 'text_content': self.text_content,
            'tables': self.tables, 'code_blocks': self.code_blocks,
            'internal_links': self.internal_links, 'parent_url': self.parent_url,
            'depth': self.depth, 'word_count': self.word_count,
        }

    def to_flat_dict(self) -> dict:
        return {
            'url': self.url, 'title': self.title,
            'breadcrumb': ' > '.join(self.breadcrumb),
            'section_path': ' > '.join(self.section_path),
            'h1': ' | '.join(self.headings.get('h1', [])),
            'h2': ' | '.join(self.headings.get('h2', [])),
            'h3': ' | '.join(self.headings.get('h3', [])),
            'text_content': self.text_content[:15000],
            'tables_count': len(self.tables),
            'code_blocks_count': len(self.code_blocks),
            'internal_links_count': len(self.internal_links),
            'depth': self.depth, 'word_count': self.word_count,
        }


@dataclass
class AsyncCrawlResult:
    """Result of an async crawl operation."""
    pages: List[_PageResult] = field(default_factory=list)
    rag_corpus: Optional[RAGCorpus] = None
    stats: Dict = field(default_factory=dict)
    errors: List[Dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Main Async Crawler
# ---------------------------------------------------------------------------

class AsyncDocCrawler:
    """
    High-performance async crawler with worker pool.

    Usage::

        config = AsyncCrawlConfig(max_pages=300, max_workers=6)
        crawler = AsyncDocCrawler(config)
        result = await crawler.crawl("https://docs.example.com/guide")

        # Or from sync code:
        result = crawler.run("https://docs.example.com/guide")
    """

    def __init__(self, config: AsyncCrawlConfig = None):
        self.config = config or AsyncCrawlConfig()
        self.url_normalizer = URLNormalizer()
        self.monitor = PerformanceMonitor(max_workers=self.config.max_workers)

        # State (reset per crawl)
        self._scope_filter: Optional[ScopeFilter] = None
        self._visited: Set[str] = set()
        self._queued: Set[str] = set()
        self._pages: List[_PageResult] = []
        self._errors: List[Dict] = []
        self._scope_rejected_buffer: Set[str] = set()
        self._stop_requested = False

        # Playwright handles
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

        # FluidTopics resolver
        self._ft_resolver: Dict[str, Tuple[str, str]] = {}
        self._ft_base_url: str = ''

        # Async primitives
        self._queue: Optional[asyncio.Queue] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._pages_lock = asyncio.Lock()
        self._visited_lock = asyncio.Lock()
        self._ft_lock = asyncio.Lock()

        # Progress
        self._progress_callback: Optional[Callable] = None
        self._expandables_clicked = 0

    def set_progress_callback(self, callback: Callable) -> None:
        """Set callback: callback(pages_crawled, current_url, stats_dict)"""
        self._progress_callback = callback

    def stop(self) -> None:
        """Request graceful stop."""
        self._stop_requested = True
        logger.info("Stop requested")

    # ------------------------------------------------------------------
    # Sync entry point
    # ------------------------------------------------------------------

    def run(self, start_url: str) -> AsyncCrawlResult:
        """Sync wrapper — run the async crawl from synchronous code."""
        return asyncio.run(self.crawl(start_url))

    # ------------------------------------------------------------------
    # Main async crawl
    # ------------------------------------------------------------------

    async def crawl(self, start_url: str) -> AsyncCrawlResult:
        """
        Async BFS crawl with worker pool.

        1. Initialize browser + context
        2. Seed queue with start_url
        3. Spawn N worker coroutines
        4. Workers pull URLs from queue, crawl, enqueue discovered links
        5. Wait until queue is empty or limits reached
        6. Transform all pages through RAG pipeline
        7. Return results
        """
        # Reset state
        self._visited.clear()
        self._queued.clear()
        self._pages.clear()
        self._errors.clear()
        self._scope_rejected_buffer.clear()
        self._stop_requested = False
        self._expandables_clicked = 0
        self._ft_resolver.clear()
        self._ft_base_url = ''

        # Initialize scope filter
        self._scope_filter = ScopeFilter(
            root_url=start_url,
            deny_patterns=self.config.deny_patterns,
            strip_all_queries=self.config.strip_all_queries,
        )
        self._scope_filter.log_scope()
        scope_desc = self._scope_filter.scope_description

        logger.info("=" * 65)
        logger.info("ASYNC CRAWL STARTED")
        logger.info(f"Start URL: {start_url}")
        logger.info(f"Scope: {scope_desc}")
        logger.info(f"Workers: {self.config.max_workers}")
        logger.info(f"Limits: max_pages={self.config.max_pages}, max_depth={self.config.max_depth}")
        logger.info(f"Timeout: {self.config.timeout}ms/page")
        logger.info("=" * 65)

        stop_reason = "completed"

        # Initialize async primitives
        self._queue = asyncio.Queue(maxsize=self.config.queue_maxsize)
        self._semaphore = asyncio.Semaphore(self.config.max_workers)

        # Start browser
        await self._init_browser()
        await self.monitor.start()

        try:
            # Seed queue
            await self._queue.put((start_url, 0, "", []))
            self._queued.add(start_url)
            await self.monitor.record_enqueue(1)

            # Spawn workers
            workers = [
                asyncio.create_task(self._worker(i))
                for i in range(self.config.max_workers)
            ]

            # Wait for queue to drain (with periodic limit checks)
            await self._wait_for_completion(workers)

            # Determine stop reason
            if self._stop_requested:
                stop_reason = "User requested stop"
            else:
                good_count = sum(1 for p in self._pages if not p.skipped)
                if good_count >= self.config.max_pages:
                    stop_reason = f"MAX_PAGES limit reached ({self.config.max_pages})"
                else:
                    stop_reason = "Queue exhausted (all reachable pages crawled)"

        except Exception as e:
            stop_reason = f"Error: {e}"
            logger.error(f"Crawl error: {e}", exc_info=True)
        finally:
            # Cancel workers
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

            await self.monitor.stop(stop_reason)
            await self._close_browser()

        # Filter skipped pages
        good_pages = [p for p in self._pages if not p.skipped]
        skipped_count = len(self._pages) - len(good_pages)

        # Get final metrics
        metrics = await self.monitor.snapshot()

        # Build stats dict (compatible with legacy format)
        stats = {
            'pages_crawled': len(good_pages),
            'pages_skipped': skipped_count,
            'pages_failed': metrics.pages_failed,
            'pages_retried': metrics.pages_retried,
            'expandables_clicked': self._expandables_clicked,
            'links_discovered': metrics.total_links_discovered,
            'elapsed_time': round(metrics.elapsed_sec, 2),
            'elapsed_sec': round(metrics.elapsed_sec, 2),
            'pages_per_second': metrics.pages_per_sec_overall,
            'pages_per_sec_overall': metrics.pages_per_sec_overall,
            'pages_per_sec_rolling': metrics.pages_per_sec_rolling,
            'avg_page_ms': metrics.avg_page_ms,
            'p95_page_ms': metrics.p95_page_ms,
            'queue_peak': metrics.queue_peak,
            'total_words': metrics.total_words,
            'avg_words_per_page': metrics.avg_words_per_page,
            'workers': self.config.max_workers,
            'stop_reason': stop_reason,
            'scope': scope_desc,
        }

        # Log summary
        logger.info("\n" + self.monitor.format_summary(metrics))

        # Transform through RAG pipeline
        logger.info(f"[PIPELINE] Transforming {len(good_pages)} pages into RAG documents...")
        page_dicts = [p.to_dict() for p in good_pages]
        from .pipeline import transform_batch
        rag_docs = transform_batch(page_dicts, self.config.pipeline_config)

        corpus = RAGCorpus(
            documents=rag_docs,
            crawl_config={
                'max_pages': self.config.max_pages,
                'max_depth': self.config.max_depth,
                'max_workers': self.config.max_workers,
                'timeout_ms': self.config.timeout,
                'scope': scope_desc,
            },
            crawl_stats=stats,
        )

        logger.info(
            f"[PIPELINE] Done: {corpus.total_documents} docs, "
            f"{corpus.total_chunks} chunks, {corpus.total_words:,} words"
        )

        return AsyncCrawlResult(
            pages=good_pages,
            rag_corpus=corpus,
            stats=stats,
            errors=self._errors.copy(),
        )

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    async def _worker(self, worker_id: int) -> None:
        """Worker coroutine — pulls URLs from queue and crawls them."""
        consecutive_empty = 0
        while not self._stop_requested:
            try:
                # Check page limit
                async with self._pages_lock:
                    good_count = sum(1 for p in self._pages if not p.skipped)
                if good_count >= self.config.max_pages:
                    break

                # Get next URL from queue (timeout to allow periodic checks)
                try:
                    url, depth, parent_url, section_path = await asyncio.wait_for(
                        self._queue.get(), timeout=5.0
                    )
                    consecutive_empty = 0
                except asyncio.TimeoutError:
                    # Don't exit immediately — another worker may still be
                    # crawling a page that will enqueue new links (e.g. the
                    # first page of a FluidTopics site takes 30-60s).
                    consecutive_empty += 1
                    metrics = await self.monitor.snapshot()
                    has_active = metrics.active_workers > 0
                    if self._queue.empty() and not has_active:
                        # Nobody is working and queue is empty — truly done
                        break
                    if consecutive_empty >= 12 and not has_active:
                        # 60s of empty queue with no active work → bail
                        break
                    continue

                await self.monitor.update_queue_size(self._queue.qsize())

                # Depth check
                if depth > self.config.max_depth:
                    self._queue.task_done()
                    continue

                # Acquire semaphore slot
                async with self._semaphore:
                    await self.monitor.worker_started()
                    try:
                        page_result = await self._crawl_page(
                            url, depth, parent_url, section_path
                        )
                    finally:
                        await self.monitor.worker_finished()

                if page_result:
                    async with self._pages_lock:
                        self._pages.append(page_result)

                    # Enqueue discovered links
                    if depth < self.config.max_depth and not page_result.skipped:
                        await self._enqueue_links(page_result, depth)

                    # Rate limit
                    if self.config.delay_between_pages > 0:
                        await asyncio.sleep(self.config.delay_between_pages)

                self._queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                # TargetClosedError is expected during shutdown when browser
                # closes while workers are still mid-navigation.
                err_name = type(e).__name__
                if 'TargetClosedError' in err_name or 'closed' in str(e).lower():
                    logger.debug(f"[WORKER-{worker_id}] Browser closed during work")
                    break
                logger.error(f"[WORKER-{worker_id}] Unexpected error: {e}", exc_info=True)

    async def _wait_for_completion(self, workers: List[asyncio.Task]) -> None:
        """Wait until the queue is drained or limits are reached."""
        while True:
            await asyncio.sleep(1.0)

            # Check if all workers are done
            all_done = all(w.done() for w in workers)
            if all_done:
                break

            # Check if queue is empty and no workers are active
            metrics = await self.monitor.snapshot()
            if self._queue.empty() and metrics.active_workers == 0:
                # Give a brief grace period for late enqueues
                await asyncio.sleep(2.0)
                if self._queue.empty() and metrics.active_workers == 0:
                    break

            # Check page limit
            async with self._pages_lock:
                good_count = sum(1 for p in self._pages if not p.skipped)
            if good_count >= self.config.max_pages:
                break

            # Check stop request
            if self._stop_requested:
                break

    # ------------------------------------------------------------------
    # Browser management
    # ------------------------------------------------------------------

    async def _init_browser(self) -> None:
        """Initialize async Playwright browser with resource blocking."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            args=[
                '--disable-gpu',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-web-security',
                '--disable-background-networking',
                '--disable-default-apps',
                '--disable-extensions',
                '--disable-sync',
                '--disable-translate',
                '--metrics-recording-only',
                '--no-first-run',
            ]
        )
        self._context = await self._browser.new_context(
            user_agent=self.config.user_agent,
            viewport={
                'width': self.config.viewport_width,
                'height': self.config.viewport_height,
            },
            locale='en-US',
            timezone_id='America/New_York',
        )

        # Set up route-based resource blocking
        if self.config.block_images or self.config.block_fonts or self.config.block_media:
            await self._context.route("**/*", self._route_handler)

        logger.info(
            f"Async Playwright browser initialized "
            f"(workers={self.config.max_workers}, "
            f"blocking={'images,fonts,media,analytics' if self.config.block_images else 'none'})"
        )

    async def _route_handler(self, route) -> None:
        """Block unnecessary resources for speed."""
        request = route.request
        resource_type = request.resource_type
        url = request.url

        # Block by resource type
        if resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return

        # Block analytics/tracking scripts
        if self.config.block_analytics and resource_type == "script":
            for pattern in _BLOCKED_URL_PATTERNS:
                if pattern.search(url):
                    await route.abort()
                    return

        await route.continue_()

    async def _close_browser(self) -> None:
        """Close browser and Playwright."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    # ------------------------------------------------------------------
    # Page crawling
    # ------------------------------------------------------------------

    async def _crawl_page(
        self,
        url: str,
        depth: int,
        parent_url: str,
        section_path: List[str],
    ) -> Optional[_PageResult]:
        """Crawl a single page asynchronously."""
        timing = PageTiming(url=url)
        t_start = time.monotonic()

        # Normalize URL
        normalized = self.url_normalizer.normalize(url)
        if not normalized:
            return None

        # Already visited?
        async with self._visited_lock:
            if normalized in self._visited:
                return None
            # Scope check
            if self._scope_filter and not self._scope_filter.accept(normalized):
                return None
            self._visited.add(normalized)

        logger.info(f"[{depth}] Crawling: {normalized[:80]}")

        page = None
        try:
            page = await self._context.new_page()

            # Response interception for SPA content APIs
            intercepted = {'html': '', 'size': 0}
            self_ref = self

            async def on_response(resp):
                try:
                    ct = resp.headers.get('content-type', '')
                    rurl = resp.url
                    url_lower = rurl.lower()

                    # Capture largest HTML response from APIs
                    if ('text/html' in ct and '/api/' in url_lower
                            and resp.status == 200):
                        body = await resp.text()
                        if len(body) > intercepted['size']:
                            intercepted['html'] = body
                            intercepted['size'] = len(body)

                    # FluidTopics pages API
                    if ('application/json' in ct and resp.status == 200
                            and '/api/khub/maps/' in rurl
                            and '/pages' in rurl):
                        async with self_ref._ft_lock:
                            if not self_ref._ft_resolver:
                                m = re.search(r'/api/khub/maps/([^/]+)/pages', rurl)
                                if m:
                                    map_id = m.group(1)
                                    parsed_origin = urlparse(rurl)
                                    ft_base = f"{parsed_origin.scheme}://{parsed_origin.netloc}/docs"
                                    try:
                                        body = await resp.json()
                                        self_ref._ft_build_resolver(body, map_id, ft_base)
                                    except Exception:
                                        pass
                except Exception:
                    pass

            page.on('response', on_response)

            # Navigate
            t_nav_start = time.monotonic()
            response = await page.goto(
                normalized,
                timeout=self.config.timeout,
                wait_until='load',
            )

            if response is None or response.status >= 400:
                timing.status = "failed"
                timing.total_ms = (time.monotonic() - t_start) * 1000
                await self.monitor.record_page(timing)
                self._errors.append({
                    'url': normalized,
                    'error': f"HTTP {response.status if response else 'No response'}",
                    'depth': depth,
                })
                return None

            # Wait for network to settle — many doc sites (Oracle, etc.)
            # load navigation trees via RequireJS *after* DOMContentLoaded.
            try:
                await page.wait_for_load_state('networkidle', timeout=5000)
            except PlaywrightTimeout:
                pass

            # ── FluidTopics (GWT SPA) detection & wait ──────────────
            # ServiceNow docs, etc. use FluidTopics which needs extra
            # time for the GWT bootstrap to fetch + render content.
            is_ft = await self._detect_fluidtopics(page)
            if is_ft:
                await self._wait_for_fluidtopics(page)

            # Link stabilization: poll until <a href> count stops changing.
            # Oracle, ServiceNow, and other JS-heavy sites render nav links
            # asynchronously; without this we discover only 1-3 links.
            await self._wait_for_links_stable(page)

            timing.navigate_ms = (time.monotonic() - t_nav_start) * 1000

            # Record bytes from response
            try:
                body_bytes = len(await response.body())
                await self.monitor.record_bytes(body_bytes)
            except Exception:
                pass

            # Cookie consent dismissal
            await self._dismiss_cookie_consent(page)

            # Dismiss WalkMe / survey overlays that block clicks
            await self._dismiss_overlays(page)

            # SPA content wait (non-FT sites; FT already waited above)
            if not is_ft:
                await self._wait_for_spa_content(page)

            # Redirect detection → scope widening
            landing_url = page.url
            if landing_url and landing_url != normalized:
                if self._scope_filter:
                    widened = self._scope_filter.widen_scope(landing_url)
                    if widened:
                        logger.info(
                            f"[SCOPE] Redirect: {normalized[:50]} → {landing_url[:50]}"
                        )
                        self._scope_filter.log_scope()

            # Auto-detect page complexity
            page_type = await self._detect_page_complexity(page)

            # Skip expansion when queue is large
            queue_size = self._queue.qsize()
            if queue_size >= self.config.max_pages:
                expanded, hit_limit = 0, False
            elif page_type == 'html':
                expanded, hit_limit = 0, False
            else:
                expanded, hit_limit = await self._expand_all_elements(page)

            # Extract content
            t_extract_start = time.monotonic()
            content = await self._extract_content(
                page,
                intercepted_html=intercepted['html'],
                page_url=normalized,
            )

            # Extract links
            if self._queue.qsize() >= self.config.max_pages:
                links = []
            else:
                links = await self._extract_links(page, normalized)

                # Supplement with shadow DOM links (FluidTopics, Web Components)
                if is_ft or len(links) < 3:
                    shadow_links = await self._extract_shadow_dom_links(
                        page, normalized
                    )
                    existing = set(links)
                    for sl in shadow_links:
                        if sl not in existing:
                            links.append(sl)
                            existing.add(sl)

                # Supplement with FluidTopics resolver links
                if is_ft and self._ft_resolver:
                    ft_links = self._build_ft_links()
                    existing = set(links)
                    for fl in ft_links:
                        n = self.url_normalizer.normalize(fl)
                        if n and n not in existing:
                            links.append(n)
                            existing.add(n)

            # Breadcrumb and section path
            breadcrumb = await self._extract_breadcrumb(page)
            current_section = await self._extract_section_path(page) or section_path

            timing.extract_ms = (time.monotonic() - t_extract_start) * 1000

            # Build page result
            result = _PageResult(
                url=normalized,
                title=content['title'],
                breadcrumb=breadcrumb,
                section_path=current_section,
                headings=content['headings'],
                text_content=content['text'],
                tables=content['tables'],
                code_blocks=content['code_blocks'],
                internal_links=links,
                parent_url=parent_url,
                depth=depth,
                word_count=len(content['text'].split()),
            )

            # Content quality gate
            if result.word_count < self.config.min_word_count:
                text_lower = result.text_content.lower()
                is_junk = (
                    'loading application' in text_lower
                    or ('cookie' in text_lower and result.word_count < 100)
                    or result.word_count == 0
                )
                if is_junk:
                    logger.warning(
                        f"[SKIP] {normalized[:70]} — "
                        f"empty/loading/cookie ({result.word_count} words)"
                    )
                    result.text_content = ""
                    result.word_count = 0
                    result.skipped = True
                    timing.status = "skipped"
                    timing.word_count = 0
                    timing.link_count = len(links)
                    timing.total_ms = (time.monotonic() - t_start) * 1000
                    await self.monitor.record_page(timing)
                    return result

            # Success
            timing.status = "ok"
            timing.word_count = result.word_count
            timing.link_count = len(links)
            timing.total_ms = (time.monotonic() - t_start) * 1000
            await self.monitor.record_page(timing)

            logger.info(
                f"[OK] {normalized[:60]} — "
                f"{result.word_count:,} words, {len(links)} links, "
                f"{timing.total_ms:.0f}ms"
            )

            # Progress callback
            if self._progress_callback:
                try:
                    async with self._pages_lock:
                        count = len(self._pages) + 1
                    stats = {
                        'pages_crawled': count,
                        'expandables_clicked': self._expandables_clicked,
                        'links_discovered': timing.link_count,
                    }
                    self._progress_callback(count, normalized, stats)
                except Exception:
                    pass

            return result

        except PlaywrightTimeout:
            logger.warning(f"[TIMEOUT] {normalized[:70]}")
            timing.status = "timeout"
            timing.total_ms = (time.monotonic() - t_start) * 1000
            await self.monitor.record_page(timing)

            if self.config.enable_static_fallback:
                return await self._crawl_page_static(
                    normalized, depth, parent_url, section_path
                )
            self._errors.append({
                'url': normalized, 'error': 'Timeout', 'depth': depth,
            })
            return None

        except Exception as e:
            logger.warning(f"[ERROR] {normalized[:70]}: {e}")
            timing.status = "failed"
            timing.total_ms = (time.monotonic() - t_start) * 1000
            await self.monitor.record_page(timing)

            if self.config.enable_static_fallback:
                return await self._crawl_page_static(
                    normalized, depth, parent_url, section_path
                )
            self._errors.append({
                'url': normalized, 'error': str(e), 'depth': depth,
            })
            return None

        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Link enqueuing
    # ------------------------------------------------------------------

    async def _enqueue_links(
        self,
        page_result: _PageResult,
        depth: int,
    ) -> None:
        """Enqueue discovered links into the BFS queue."""
        new_count = 0
        for link in page_result.internal_links:
            async with self._visited_lock:
                if link in self._visited or link in self._queued:
                    continue
                if self._scope_filter and not self._scope_filter.accept(link):
                    continue
                self._queued.add(link)

            try:
                self._queue.put_nowait((
                    link,
                    depth + 1,
                    page_result.url,
                    page_result.section_path.copy(),
                ))
                new_count += 1
            except asyncio.QueueFull:
                logger.warning("[QUEUE] Queue full — dropping link")
                break

        if new_count > 0:
            await self.monitor.record_enqueue(new_count)
            await self.monitor.update_queue_size(self._queue.qsize())

        # Scope widening for first page
        async with self._pages_lock:
            page_count = len(self._pages)
        if (page_count <= 1
                and new_count == 0
                and self._scope_rejected_buffer
                and self._scope_filter
                and self._scope_filter._scope_path != "/"):
            logger.warning(
                f"[SCOPE] First page: 0 in-scope links, "
                f"{len(self._scope_rejected_buffer)} rejected — widening"
            )
            self._scope_filter.widen_to_domain()
            self._scope_filter.log_scope()
            recovered = 0
            for link in self._scope_rejected_buffer:
                async with self._visited_lock:
                    if link in self._visited or link in self._queued:
                        continue
                    if self._scope_filter.accept(link):
                        self._queued.add(link)
                        try:
                            self._queue.put_nowait((
                                link, depth + 1,
                                page_result.url,
                                page_result.section_path.copy(),
                            ))
                            recovered += 1
                        except asyncio.QueueFull:
                            break
            if recovered:
                await self.monitor.record_enqueue(recovered)
                logger.info(f"[SCOPE] Recovered {recovered} links after widening")

    # ------------------------------------------------------------------
    # Content extraction (async wrappers around sync Playwright evaluate)
    # ------------------------------------------------------------------

    async def _extract_content(
        self,
        page: Page,
        intercepted_html: str = '',
        page_url: str = '',
    ) -> Dict[str, Any]:
        """Extract main content from page (mirrors sync deep_crawler logic)."""
        content: Dict[str, Any] = {
            'title': '', 'headings': {}, 'text': '',
            'tables': [], 'code_blocks': [],
        }

        # Title
        try:
            title_el = await page.query_selector('h1')
            if not title_el:
                title_el = await page.query_selector('title')
            if title_el:
                content['title'] = (await title_el.inner_text()).strip()
        except Exception:
            pass

        # Main content area — pick the first selector with real content
        main_content = None
        best_content = None
        best_content_len = 0
        for selector in self.config.content_selectors:
            try:
                el = await page.query_selector(selector)
                if el:
                    text_len = await el.evaluate(
                        "el => (el.innerText || '').trim().length"
                    )
                    # Track best candidate
                    if text_len > best_content_len:
                        best_content = el
                        best_content_len = text_len
                    # Use immediately if it has substantial content
                    if text_len >= 500:
                        main_content = el
                        break
            except Exception:
                continue
        # Fall back to best candidate if nothing reached 500 chars
        if not main_content and best_content and best_content_len >= 100:
            main_content = best_content
        if not main_content:
            main_content = await page.query_selector('body')

        if main_content:
            # Headings
            for level in range(1, 7):
                try:
                    headings = await main_content.query_selector_all(f'h{level}')
                    if headings:
                        texts = []
                        for h in headings:
                            t = (await h.inner_text()).strip()
                            if t:
                                texts.append(t)
                        if texts:
                            content['headings'][f'h{level}'] = texts
                except Exception:
                    pass

            # Tables
            try:
                tables = await main_content.query_selector_all('table')
                for table in tables[:20]:  # limit
                    try:
                        table_data = {'headers': [], 'rows': []}
                        headers = await table.query_selector_all('th')
                        table_data['headers'] = [
                            (await h.inner_text()).strip() for h in headers
                        ]
                        rows = await table.query_selector_all('tr')
                        for row in rows:
                            cells = await row.query_selector_all('td')
                            if cells:
                                table_data['rows'].append([
                                    (await c.inner_text()).strip() for c in cells
                                ])
                        if table_data['headers'] or table_data['rows']:
                            content['tables'].append(table_data)
                    except Exception:
                        pass
            except Exception:
                pass

            # Code blocks
            try:
                code_blocks = await main_content.query_selector_all('pre, code')
                for block in code_blocks[:20]:
                    try:
                        code_text = (await block.inner_text()).strip()
                        if code_text and len(code_text) > 10:
                            content['code_blocks'].append(code_text)
                    except Exception:
                        pass
            except Exception:
                pass

            # Text content
            try:
                for selector in self.config.exclude_selectors:
                    try:
                        els = await main_content.query_selector_all(selector)
                        for el in els:
                            await el.evaluate("el => el.remove()")
                    except Exception:
                        pass

                text = (await main_content.inner_text()).strip()
                text = re.sub(r'\n{3,}', '\n\n', text)
                text = re.sub(r' {2,}', ' ', text)
                content['text'] = text
            except Exception:
                pass

        # Fallback 1: Shadow DOM
        dom_wc = len(content['text'].split())
        if dom_wc < self.config.min_word_count:
            shadow_text = await self._extract_shadow_dom_text(page)
            shadow_wc = len(shadow_text.split())
            if shadow_wc > dom_wc and shadow_wc >= self.config.min_word_count:
                logger.info(f"[SHADOW-DOM] {shadow_wc} words (light DOM had {dom_wc})")
                content['text'] = shadow_text

        # Fallback 2: Intercepted API HTML
        current_wc = len(content['text'].split())
        if intercepted_html and _HAS_BS4:
            api_text = self._parse_html(intercepted_html)
            api_wc = len(api_text.split())
            use_api = (
                (current_wc < self.config.min_word_count and api_wc >= self.config.min_word_count)
                or (api_wc >= 3 * max(current_wc, 1))
            )
            if use_api:
                logger.info(f"[API-CONTENT] {api_wc} words (DOM/shadow had {current_wc})")
                content['text'] = api_text
                self._enrich_from_html(content, intercepted_html, page_url)

        # Fallback 3: FluidTopics
        current_wc = len(content['text'].split())
        if self._ft_resolver and current_wc < 300:
            ft_html = await self._ft_fetch_content(page_url or page.url)
            if ft_html and _HAS_BS4:
                ft_text = self._parse_html(ft_html)
                ft_wc = len(ft_text.split())
                if ft_wc >= self.config.min_word_count and ft_wc > current_wc:
                    logger.info(f"[FT-FETCH] {ft_wc} words (previous had {current_wc})")
                    content['text'] = ft_text
                    self._enrich_from_html(content, ft_html, page_url)

        return content

    async def _extract_shadow_dom_text(self, page: Page) -> str:
        """Recursively extract text from shadow DOM."""
        try:
            text = await page.evaluate(r"""
                () => {
                    const SKIP = new Set(['STYLE', 'SCRIPT', 'NOSCRIPT', 'SVG', 'IMG', 'BR', 'HR']);
                    function collect(root, depth) {
                        if (depth > 12) return '';
                        let out = '';
                        const nodes = root.childNodes || [];
                        for (const n of nodes) {
                            if (n.nodeType === Node.TEXT_NODE) {
                                const t = n.textContent?.trim();
                                if (t) out += t + ' ';
                            } else if (n.nodeType === Node.ELEMENT_NODE) {
                                if (SKIP.has(n.tagName)) continue;
                                if (n.shadowRoot) out += collect(n.shadowRoot, depth + 1);
                                out += collect(n, depth + 1);
                            }
                        }
                        return out;
                    }
                    let result = '';
                    document.querySelectorAll('*').forEach(el => {
                        if (el.shadowRoot) result += collect(el.shadowRoot, 0) + '\n';
                    });
                    return result.trim();
                }
            """)
            return text or ''
        except Exception:
            return ''

    async def _extract_links(self, page: Page, base_url: str) -> List[str]:
        """Extract in-scope links from the page.

        Uses a single JS evaluation to collect all hrefs at once, which is
        orders of magnitude faster than iterating ElementHandles individually
        (5,600 links on Oracle took ~90s with the old approach).
        """
        links: Set[str] = set()
        base_domain = urlparse(base_url).netloc.lower()

        # Compile junk patterns
        junk_patterns = []
        for pat in self.config.builtin_deny_patterns:
            try:
                junk_patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error:
                pass

        # Build a CSS selector list for the JS query
        selectors_css = ', '.join(self.config.link_selectors)

        # Batch-extract all hrefs in one JS call
        try:
            raw_hrefs: List[str] = await page.evaluate(f"""
                () => {{
                    const sels = {repr(self.config.link_selectors)};
                    const seen = new Set();
                    const result = [];
                    for (const sel of sels) {{
                        try {{
                            document.querySelectorAll(sel).forEach(el => {{
                                const href = el.getAttribute('href');
                                if (href && !seen.has(href)) {{
                                    seen.add(href);
                                    result.push(href);
                                }}
                            }});
                        }} catch(e) {{}}
                    }}
                    return result;
                }}
            """)
        except Exception:
            raw_hrefs = []

        page_url = page.url
        for href in raw_hrefs:
            try:
                absolute = urljoin(ensure_joinable_base(page_url), href)
                if not absolute.startswith(('http://', 'https://')):
                    continue

                url_domain = urlparse(absolute).netloc.lower()
                if url_domain != base_domain:
                    continue

                if '#' in absolute:
                    absolute = absolute.split('#')[0]

                normalized = self.url_normalizer.normalize(absolute)
                if not normalized:
                    continue

                if any(rx.search(normalized) for rx in junk_patterns):
                    continue

                if self._scope_filter and not self._scope_filter.accept(normalized):
                    self._scope_rejected_buffer.add(normalized)
                    continue

                links.add(normalized)
            except Exception:
                continue

        return list(links)

    async def _extract_breadcrumb(self, page: Page) -> List[str]:
        """Extract breadcrumb navigation."""
        selectors = [
            '.breadcrumb a', '.breadcrumb li',
            '[aria-label="breadcrumb"] a', '.ohc-breadcrumb a',
            'nav[aria-label*="breadcrumb"] a',
        ]
        for selector in selectors:
            try:
                elements = await page.query_selector_all(selector)
                if elements:
                    bc = []
                    for el in elements:
                        text = (await el.inner_text()).strip()
                        if text and text not in bc:
                            bc.append(text)
                    if bc:
                        return bc
            except Exception:
                continue
        return []

    async def _extract_section_path(self, page: Page) -> List[str]:
        """Extract section path from sidebar/TOC."""
        selectors = [
            '.toc-item.active', '.nav-item.active', '.tree-item.selected',
            '[aria-current="page"]', '.ohc-sidebar-item.active', '.is-selected',
        ]
        for selector in selectors:
            try:
                active = await page.query_selector(selector)
                if active:
                    path = []
                    current = active
                    while current:
                        text = (await current.inner_text()).strip().split('\n')[0][:100]
                        if text:
                            path.insert(0, text)
                        has_parent = await current.evaluate("""el => {
                            const p = el.closest('li, .toc-item, .tree-item, .nav-item');
                            return p?.parentElement?.closest('li, .toc-item, .tree-item, .nav-item') ? true : false;
                        }""")
                        if not has_parent:
                            break
                        handle = await current.evaluate_handle("""el => {
                            const p = el.closest('li, .toc-item, .tree-item, .nav-item');
                            return p?.parentElement?.closest('li, .toc-item, .tree-item, .nav-item');
                        }""")
                        current = handle.as_element()
                        if not current:
                            break
                    if path:
                        return path
            except Exception:
                continue
        return []

    async def _detect_page_complexity(self, page: Page) -> str:
        """Auto-detect JS vs HTML page."""
        try:
            expandable_count = await page.evaluate("""
                () => {
                    const sels = [
                        '[aria-expanded="false"]',
                        'details:not([open]) > summary',
                        '.collapsed', '.accordion-button.collapsed',
                        '[data-toggle]', '[data-bs-toggle]',
                    ];
                    let c = 0;
                    for (const s of sels) {
                        try { c += document.querySelectorAll(s).length; } catch(e) {}
                    }
                    return c;
                }
            """)
            if expandable_count >= 3:
                return 'js'

            link_count = await page.evaluate(
                "() => document.querySelectorAll('a[href]').length"
            )
            if link_count < 5:
                body_len = await page.evaluate(
                    "() => (document.body?.innerText || '').trim().length"
                )
                if body_len < 200:
                    return 'js'
            return 'html'
        except Exception:
            return 'js'

    # ------------------------------------------------------------------
    # FluidTopics (GWT SPA) support
    # ------------------------------------------------------------------

    async def _detect_fluidtopics(self, page: Page) -> bool:
        """Detect if the current page is a FluidTopics-powered site.

        FluidTopics (used by ServiceNow, etc.) is a GWT-based SPA that
        renders all content via Web Components.  The initial HTML is just
        a loading spinner; real content appears only after GWT bootstrap.
        """
        try:
            return await page.evaluate("""
                () => {
                    if (window['FluidTopicsClientConfiguration']) return true;
                    if (document.getElementById('fluidtopicsclient')) return true;
                    if (document.body?.className?.includes('FT-version')) return true;
                    if (document.getElementById('FT-application-loader')) return true;
                    const scripts = document.querySelectorAll('script[src]');
                    for (const s of scripts) {
                        if (s.src && s.src.includes('fluidtopics')) return true;
                    }
                    return false;
                }
            """)
        except Exception:
            return False

    async def _wait_for_fluidtopics(self, page: Page) -> None:
        """Wait for FluidTopics GWT SPA to finish rendering.

        GWT bootstrap sequence:
        1. Load initial HTML (just #FT-application-loader spinner)
        2. Download fluidtopicsclient.nocache.js (GWT bootstrap)
        3. Download the compiled GWT permutation JS
        4. GWT initializes, makes API calls (/api/khub/maps/{id}/pages)
        5. GWT renders content into Web Components

        We wait for step 5 by polling for the loader to disappear and
        for real content to appear.
        """
        logger.info("[FT-WAIT] Detected FluidTopics site — waiting for GWT render...")
        t0 = time.monotonic()

        # Phase 1: Wait for the loader to disappear (GWT bootstrap)
        try:
            await page.wait_for_function(
                """
                () => {
                    const loader = document.getElementById('FT-application-loader');
                    if (!loader) return true;
                    const style = window.getComputedStyle(loader);
                    return style.display === 'none' || style.visibility === 'hidden'
                           || style.opacity === '0' || loader.offsetParent === null;
                }
                """,
                timeout=15000,
            )
            logger.debug("[FT-WAIT] Loader disappeared")
        except PlaywrightTimeout:
            logger.warning("[FT-WAIT] Loader still visible after 15s")

        # Phase 2: Wait for network to settle (GWT API calls)
        try:
            await page.wait_for_load_state('networkidle', timeout=8000)
        except PlaywrightTimeout:
            pass

        # Phase 3: Quick check if FT resolver already populated
        async with self._ft_lock:
            has_resolver = bool(self._ft_resolver)
        if not has_resolver:
            # Poll briefly — API response may arrive any moment
            poll_end = time.monotonic() + 4.0
            while time.monotonic() < poll_end:
                await asyncio.sleep(0.5)
                async with self._ft_lock:
                    if self._ft_resolver:
                        has_resolver = True
                        logger.info(
                            f"[FT-WAIT] Resolver ready: {len(self._ft_resolver)} topics"
                        )
                        break

        # Phase 4: Wait for body text to exceed loading-placeholder level.
        # Once we see >300 chars of text OR resolver is ready, we're done.
        prev_len = 0
        stable = 0
        text_ok = False
        poll_end = time.monotonic() + 6.0
        while time.monotonic() < poll_end:
            try:
                cur_len = await page.evaluate(
                    "() => (document.body?.innerText || '').trim().length"
                )
            except Exception:
                break
            if cur_len > 300:
                text_ok = True
                break
            if cur_len > 100 and cur_len == prev_len:
                stable += 1
                if stable >= 2:
                    text_ok = True
                    break
            else:
                stable = 0
                prev_len = cur_len
            await asyncio.sleep(0.5)

        elapsed = time.monotonic() - t0
        logger.info(f"[FT-WAIT] Done in {elapsed:.1f}s (resolver={'yes' if has_resolver else 'no'}, text={'ok' if text_ok else 'sparse'})")

        # Brief settle
        await asyncio.sleep(0.5)

    def _build_ft_links(self) -> List[str]:
        """Build absolute URLs from the FluidTopics resolver map.

        Called after the FT resolver is populated from the intercepted
        /api/khub/maps/{mapId}/pages API response.  Returns a list of
        full URLs for all topics in the documentation set.
        """
        if not self._ft_resolver or not self._ft_base_url:
            return []

        links = []
        base = self._ft_base_url.rstrip('/')
        for pretty_url in self._ft_resolver:
            # prettyUrl is like /bundle/release-product/page/topic.html
            # or /r/something/page.html — prepend the base
            if pretty_url.startswith('/'):
                full_url = base + pretty_url
            else:
                full_url = base + '/' + pretty_url
            links.append(full_url)

        logger.info(f"[FT-LINKS] Built {len(links)} topic URLs from resolver")
        return links

    async def _extract_shadow_dom_links(
        self, page: Page, base_url: str
    ) -> List[str]:
        """Extract links from inside shadow DOM trees.

        FluidTopics and other Web Component-based sites render their
        navigation links inside shadow roots, invisible to normal
        querySelectorAll('a[href]').  This helper recursively walks
        all shadow roots and collects anchor hrefs.
        """
        try:
            raw_hrefs: List[str] = await page.evaluate("""
                () => {
                    const SKIP = new Set(['STYLE', 'SCRIPT', 'NOSCRIPT']);
                    const hrefs = new Set();
                    function walk(root, depth) {
                        if (depth > 12) return;
                        const nodes = root.querySelectorAll
                            ? root.querySelectorAll('a[href]')
                            : [];
                        for (const a of nodes) {
                            const h = a.getAttribute('href');
                            if (h && !h.startsWith('javascript:') && !h.startsWith('#'))
                                hrefs.add(h);
                        }
                        // Recurse into shadow roots
                        const allEls = root.querySelectorAll
                            ? root.querySelectorAll('*')
                            : [];
                        for (const el of allEls) {
                            if (SKIP.has(el.tagName)) continue;
                            if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
                        }
                    }
                    // Walk all shadow roots from document
                    document.querySelectorAll('*').forEach(el => {
                        if (el.shadowRoot) walk(el.shadowRoot, 0);
                    });
                    return [...hrefs];
                }
            """)
        except Exception:
            raw_hrefs = []

        if not raw_hrefs:
            return []

        base_domain = urlparse(base_url).netloc.lower()
        page_url = page.url
        links: List[str] = []

        for href in raw_hrefs:
            try:
                absolute = urljoin(ensure_joinable_base(page_url), href)
                if not absolute.startswith(('http://', 'https://')):
                    continue
                if urlparse(absolute).netloc.lower() != base_domain:
                    continue
                if '#' in absolute:
                    absolute = absolute.split('#')[0]
                normalized = self.url_normalizer.normalize(absolute)
                if normalized and normalized not in links:
                    links.append(normalized)
            except Exception:
                continue

        if links:
            logger.info(f"[SHADOW-LINKS] Found {len(links)} links in shadow DOM")
        return links

    async def _expand_all_elements(self, page: Page) -> Tuple[int, bool]:
        """Enterprise-grade async expansion via interaction_policy engine.

        Delegates to ``interaction_policy.async_expansion_loop`` which provides:
        - Full 130+ selector catalogue (DEFAULT_INTERACTIVE_SELECTORS)
        - Full 20+ bulk-expand selectors (BULK_EXPAND_SELECTORS)
        - Meaningful-delta gating (pre/post DOM snapshot on every click)
        - Navigation-link safety (skips plain <a> that would navigate)
        - Already-expanded detection (prevents collapsing open content)
        - Rich fingerprint dedup (tag + id + class + text + position)
        - Text-heuristic scan for elements selectors miss
        - Multi-pass ARIA tree re-query for nested structures
        - Time budget + consecutive-wasted-click early exit

        Enterprise site coverage:
        - Oracle Docs (JET, OHC tree, RequireJS)
        - Microsoft Learn (details, tabs)
        - ServiceNow (FluidTopics, Web Components)
        - SAP Fiori / UI5 (sapMPanel, sapMPanelHdr)
        - Confluence / Atlassian (expand-control, aui-expander)
        - Ant Design, MUI, Chakra UI, Notion, GitBook
        - Docusaurus, MkDocs Material, ReadTheDocs / Sphinx
        - Salesforce Lightning, Zendesk Guide
        - Bootstrap 4/5 accordions
        - Next.js / Nextra / Vercel docs
        """
        exp_result = await interaction_policy.async_expansion_loop(
            page,
            max_clicks=self.config.max_clicks_per_page,
            max_passes=self.config.max_expansion_passes,
            click_timeout_ms=1500,
            delay_after_click_s=self.config.delay_after_click,
            meaningful_text_delta=80,
            meaningful_link_delta=1,
            selectors=self.config.interactive_selectors,  # None = full catalogue
            max_expansion_time_s=self.config.max_expansion_time_s,
            consecutive_wasted_limit=self.config.consecutive_wasted_limit,
        )

        self._expandables_clicked += exp_result.meaningful_clicks
        return exp_result.meaningful_clicks, exp_result.hit_limit

    async def _wait_for_links_stable(self, page: Page, timeout_s: float = 4.0) -> None:
        """Poll until <a href> count stabilizes — handles Oracle/RequireJS tree nav.

        Many documentation sites (Oracle OHC, ServiceNow, etc.) render their
        navigation trees asynchronously after 'load'.  Without this wait, we
        see only 1-3 links instead of dozens.
        """
        try:
            prev_count = await page.evaluate(
                "() => document.querySelectorAll('a[href]').length"
            )
            stable_ticks = 0
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                cur_count = await page.evaluate(
                    "() => document.querySelectorAll('a[href]').length"
                )
                if cur_count == prev_count:
                    stable_ticks += 1
                    if stable_ticks >= 2:
                        # Link count stable for 1 second — JS done
                        break
                else:
                    stable_ticks = 0
                    prev_count = cur_count
            logger.debug(
                f"[LINKS-STABLE] Settled at {prev_count} links "
                f"after {time.monotonic() - (deadline - timeout_s):.1f}s"
            )
        except Exception:
            pass

    async def _dismiss_cookie_consent(self, page: Page) -> None:
        """Dismiss cookie consent banners.

        Runs once per page. Uses safe timeout. Does not block expansion.
        Covers OneTrust, TrustArc, CookieBot, generic patterns (25+ selectors).
        """
        selectors = [
            # Common button text patterns
            'button:has-text("Accept")',
            'button:has-text("Accept and Proceed")',
            'button:has-text("Accept All")',
            'button:has-text("Accept Cookies")',
            'button:has-text("I Accept")',
            'button:has-text("Agree")',
            'button:has-text("OK")',
            'button:has-text("Got it")',
            'button:has-text("Allow All")',
            'button:has-text("I Understand")',
            'button:has-text("Close")',
            # OneTrust
            '#onetrust-accept-btn-handler',
            # TrustArc (ServiceNow, etc.)
            '#truste-consent-button',
            '.trustarc-agree-btn',
            'a.call:has-text("Agree and Proceed")',
            '#consent_agree_button',
            '.pdynamicbutton:has-text("Continue")',
            # CookieBot
            '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
            '#CybotCookiebotDialogBodyButtonAccept',
            # Common id / class patterns
            '.cookie-accept',
            '.cc-accept',
            '[data-testid="cookie-accept"]',
            '.consent-accept',
            '#cookie-accept',
            '#accept-cookies',
            '.js-accept-cookies',
        ]
        for selector in selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click(timeout=2000)
                    logger.debug(f"[COOKIE] Dismissed via: {selector}")
                    await asyncio.sleep(0.3)
                    return
            except Exception:
                continue

        # Fallback: look inside iframes for consent dialogs (TrustArc truste-banner)
        try:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                frame_url = frame.url or ''
                if any(k in frame_url.lower() for k in ['truste', 'consent', 'cookie', 'trustarc']):
                    for sel in [
                        'a.call:has-text("Agree and Proceed")',
                        'button:has-text("Accept")',
                        '#consent_agree_button',
                        '.pdynamicbutton',
                        'a.call',
                    ]:
                        try:
                            btn = await frame.query_selector(sel)
                            if btn and await btn.is_visible():
                                await btn.click(timeout=2000)
                                logger.info(f"[COOKIE] Dismissed TrustArc in iframe via: {sel}")
                                await asyncio.sleep(0.5)
                                return
                        except Exception:
                            continue
        except Exception:
            pass

    async def _dismiss_overlays(self, page: Page) -> None:
        """Remove WalkMe, survey, and promotional overlays that block clicks.

        ServiceNow and other enterprise sites inject WalkMe guided tours
        and similar overlays that sit on top of all content, intercepting
        pointer events and preventing expansion clicks.
        """
        try:
            removed = await page.evaluate("""
                () => {
                    let count = 0;
                    // WalkMe overlay
                    const walkmeIds = [
                        'walkme-popup-background', 'walkme-overlay',
                        'walkme-player', 'walkme-balloon',
                    ];
                    for (const id of walkmeIds) {
                        const el = document.getElementById(id);
                        if (el) { el.remove(); count++; }
                    }
                    // WalkMe classes
                    document.querySelectorAll(
                        '.walkme-override, .wm-outer-overlay, [class*="walkme"]'
                    ).forEach(el => { el.remove(); count++; });
                    // Generic overlays / modals that intercept pointer events
                    document.querySelectorAll(
                        '.modal-backdrop, .overlay-backdrop'
                    ).forEach(el => {
                        const s = window.getComputedStyle(el);
                        if (s.position === 'fixed' || s.position === 'absolute') {
                            el.remove(); count++;
                        }
                    });
                    return count;
                }
            """)
            if removed:
                logger.debug(f"[OVERLAY] Removed {removed} overlay elements")
        except Exception:
            pass

    async def _wait_for_spa_content(self, page: Page) -> None:
        """Wait if SPA content is loading (non-FluidTopics sites)."""
        try:
            body_text = await page.evaluate(
                "() => (document.body?.innerText || '').trim().substring(0, 500)"
            )
            loading_patterns = ['loading application', 'loading...', 'please wait',
                                'initializing', 'just a moment']
            is_loading = any(p in body_text.lower() for p in loading_patterns)
            is_cookie = ('cookie' in body_text.lower() and len(body_text) < 600
                         and 'accept' in body_text.lower())

            if is_loading or is_cookie:
                logger.info("[SPA-WAIT] Detected loading/placeholder...")
                wait_sels = [
                    'main', 'article', '.content', '#content', '[role="main"]',
                    '.documentation', '.doc-content', '.markdown-section',
                    '.md-content', '.rst-content',
                ]
                for sel in wait_sels:
                    try:
                        await page.wait_for_selector(sel, timeout=8000, state='attached')
                        logger.debug(f"[SPA-WAIT] Content appeared via: {sel}")
                        await asyncio.sleep(1.5)
                        return
                    except PlaywrightTimeout:
                        continue
                # Fallback: just wait
                await asyncio.sleep(5.0)
                logger.debug("[SPA-WAIT] No content selector appeared, waited 5s")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # FluidTopics support
    # ------------------------------------------------------------------

    def _ft_build_resolver(self, pages_json: dict, map_id: str, base_url: str) -> None:
        """Build URL→(mapId, contentId) mapping from FluidTopics pages API."""
        self._ft_base_url = base_url.rstrip('/')

        def walk(node):
            pretty = node.get('prettyUrl', '')
            content_id = node.get('contentId', '')
            if pretty and content_id:
                self._ft_resolver[pretty] = (map_id, content_id)
            for child in node.get('children', []):
                walk(child)
            for child in node.get('pageToc', []):
                walk(child)

        for root in pages_json.get('paginatedToc', []):
            walk(root)

        logger.info(
            f"[FT-RESOLVER] Built map: {len(self._ft_resolver)} topics from {map_id}"
        )

    async def _ft_fetch_content(self, url: str) -> str:
        """Fetch topic content from FluidTopics API."""
        if not self._ft_resolver:
            return ''

        parsed = urlparse(url)
        path = parsed.path

        for candidate in [path, re.sub(r'^/docs', '', path)]:
            if candidate in self._ft_resolver:
                map_id, content_id = self._ft_resolver[candidate]
                api_url = (
                    f"{self._ft_base_url}/api/khub/maps/{map_id}"
                    f"/topics/{content_id}/content"
                )
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                text = await resp.text()
                                if len(text) > 100:
                                    return text
                except ImportError:
                    # Fallback to requests in executor
                    import requests as req
                    loop = asyncio.get_event_loop()

                    def _sync_fetch():
                        r = req.get(api_url, timeout=10)
                        return r.text if r.status_code == 200 and len(r.text) > 100 else ''
                    return await loop.run_in_executor(None, _sync_fetch)
                except Exception:
                    pass
                break
        return ''

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_html(html: str) -> str:
        """Parse HTML to clean text."""
        if not _HAS_BS4:
            return ''
        try:
            soup = BeautifulSoup(html, _BS_PARSER)
            for tag in soup(['script', 'style', 'noscript']):
                tag.decompose()
            text = soup.get_text(separator=' ', strip=True)
            text = re.sub(r'\n{3,}', '\n\n', text)
            text = re.sub(r' {2,}', ' ', text)
            return text
        except Exception:
            return ''

    @staticmethod
    def _enrich_from_html(content: dict, html: str, page_url: str) -> None:
        """Enrich content dict with structured data from HTML."""
        if not _HAS_BS4:
            return
        try:
            soup = BeautifulSoup(html, _BS_PARSER)
            if not content['title'] or content['title'] == page_url:
                h1 = soup.find('h1')
                if h1:
                    content['title'] = h1.get_text(strip=True)
            for level in range(1, 7):
                hs = soup.find_all(f'h{level}')
                if hs:
                    content['headings'][f'h{level}'] = [
                        h.get_text(strip=True) for h in hs if h.get_text(strip=True)
                    ]
            if not content['tables']:
                for table in soup.find_all('table'):
                    td = {'headers': [], 'rows': []}
                    for th in table.find_all('th'):
                        td['headers'].append(th.get_text(strip=True))
                    for tr in table.find_all('tr'):
                        cells = tr.find_all('td')
                        if cells:
                            td['rows'].append([c.get_text(strip=True) for c in cells])
                    if td['headers'] or td['rows']:
                        content['tables'].append(td)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Static fallback
    # ------------------------------------------------------------------

    async def _crawl_page_static(
        self,
        url: str,
        depth: int,
        parent_url: str,
        section_path: List[str],
    ) -> Optional[_PageResult]:
        """Static fallback using requests + BeautifulSoup."""
        if not _HAS_BS4:
            return None

        logger.info(f"[STATIC-FALLBACK] {url[:70]}")

        loop = asyncio.get_event_loop()

        def _sync_fetch():
            import requests as req
            headers = {
                'User-Agent': self.config.user_agent,
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'en-US,en;q=0.9',
            }
            return req.get(url, headers=headers, timeout=15)

        try:
            response = await loop.run_in_executor(None, _sync_fetch)

            if response.status_code >= 400:
                self._errors.append({
                    'url': url,
                    'error': f"HTTP {response.status_code} (static)",
                    'depth': depth,
                })
                return None

            html = response.text
            soup = BeautifulSoup(html, _BS_PARSER)

            title = ""
            t = soup.find('title')
            if t:
                title = t.get_text(strip=True)
            elif soup.find('h1'):
                title = soup.find('h1').get_text(strip=True)

            headings = {}
            for lvl in range(1, 7):
                hs = soup.find_all(f'h{lvl}')
                if hs:
                    headings[f'h{lvl}'] = [
                        h.get_text(strip=True) for h in hs if h.get_text(strip=True)
                    ]

            main = None
            for sel in ['main', 'article', '.content', '#content', '[role="main"]']:
                main = soup.select_one(sel)
                if main:
                    break
            if not main:
                main = soup.find('body')

            text_content = ""
            if main:
                for tag in main.find_all(['script', 'style', 'nav', 'footer', 'header']):
                    tag.decompose()
                text_content = main.get_text(separator=' ', strip=True)
                text_content = re.sub(r'\s+', ' ', text_content)

            # Links
            base_domain = urlparse(url).netloc.lower()
            internal_links = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                abs_url = urljoin(ensure_joinable_base(url), href)
                if abs_url.startswith(('http://', 'https://')):
                    if urlparse(abs_url).netloc.lower() == base_domain:
                        if '#' in abs_url:
                            abs_url = abs_url.split('#')[0]
                        norm = self.url_normalizer.normalize(abs_url)
                        if norm and self._scope_filter and self._scope_filter.accept(norm):
                            if norm not in internal_links:
                                internal_links.append(norm)

            return _PageResult(
                url=url, title=title, section_path=section_path,
                headings=headings, text_content=text_content[:50000],
                internal_links=internal_links, parent_url=parent_url,
                depth=depth, word_count=len(text_content.split()),
            )
        except Exception as e:
            logger.warning(f"[STATIC-FALLBACK] Failed: {url[:60]}: {e}")
            self._errors.append({
                'url': url, 'error': f"Static fallback failed: {e}", 'depth': depth,
            })
            return None

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def export_json(self, result: AsyncCrawlResult, filepath: str) -> str:
        """Export results to JSON (legacy page-based format)."""
        import json
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'stats': result.stats,
            'pages': [p.to_dict() for p in result.pages],
            'errors': result.errors,
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return str(path.absolute())

    def export_rag_json(self, result: AsyncCrawlResult, filepath: str) -> str:
        """Export RAG corpus (hierarchical document+chunks format)."""
        if result.rag_corpus:
            return result.rag_corpus.export_json(filepath)
        return self.export_json(result, filepath)

    def export_rag_jsonl(self, result: AsyncCrawlResult, filepath: str) -> str:
        """Export RAG chunks as JSONL (one chunk per line)."""
        if result.rag_corpus:
            return result.rag_corpus.export_jsonl(filepath)
        return ""

    def export_csv(self, result: AsyncCrawlResult, filepath: str) -> str:
        """Export results to CSV."""
        import csv
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not result.pages:
            return str(path.absolute())
        rows = [p.to_flat_dict() for p in result.pages]
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        return str(path.absolute())

    def export_docx(self, result: AsyncCrawlResult, filepath: str) -> str:
        """Export structured Word document."""
        from .word_exporter import export_docx
        if result.rag_corpus:
            return export_docx(result.rag_corpus, filepath)
        # Fallback: build corpus from pages
        from .pipeline import transform_batch
        page_dicts = [p.to_dict() for p in result.pages]
        docs = transform_batch(page_dicts, self.config.pipeline_config)
        corpus = RAGCorpus(
            documents=docs,
            crawl_stats=result.stats,
        )
        return export_docx(corpus, filepath)

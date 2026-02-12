"""
Deep Interactive Crawler
Specialized crawler for hierarchical documentation sites with expandable content.
Clicks dropdowns, expands accordions, and crawls revealed sub-links.
"""

import logging
import time
import json
import re
from typing import List, Dict, Set, Optional, Callable, Any
from dataclasses import dataclass, field
from urllib.parse import urlparse, urljoin
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .scraper import PageData
from .utils import URLNormalizer

logger = logging.getLogger(__name__)


@dataclass
class DeepCrawlConfig:
    """Configuration for deep interactive crawling."""
    # Crawl limits
    max_pages: int = 500
    max_depth: int = 10
    timeout: int = 60000  # ms - longer for slow doc sites
    
    # Rate limiting
    delay_between_pages: float = 1.0  # seconds
    delay_after_click: float = 0.5  # seconds after clicking expandable
    
    # Browser settings
    headless: bool = True
    viewport_width: int = 1920
    viewport_height: int = 1080
    
    # User agent
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    
    # Expandable element selectors (CSS selectors for clickable dropdowns)
    expandable_selectors: List[str] = field(default_factory=lambda: [
        # Common accordion/dropdown patterns
        '[aria-expanded="false"]',
        '.collapsed',
        '.expandable:not(.expanded)',
        '.accordion-header:not(.active)',
        '.tree-node:not(.expanded)',
        '.dropdown-toggle',
        'details:not([open]) > summary',
        '[data-toggle="collapse"]',
        '.nav-link[data-bs-toggle]',
        # Oracle docs specific
        '.toc-item > .toc-link',
        '.tree-item:not(.is-expanded)',
        'li[role="treeitem"] > span',
        '.ohc-sidebar-item',
        '[class*="expand"]',
        '[class*="collapse"]',
    ])
    
    # Link selectors to find after expansion
    link_selectors: List[str] = field(default_factory=lambda: [
        'a[href]',
        '.toc-link[href]',
        '.nav-link[href]',
        '[role="treeitem"] a',
    ])
    
    # Content selectors (main content area)
    content_selectors: List[str] = field(default_factory=lambda: [
        'main',
        'article', 
        '.content',
        '.main-content',
        '#content',
        '.documentation',
        '.doc-content',
        # Oracle docs specific
        '.ohc-main-content',
        '.topic-content',
        '[role="main"]',
    ])
    
    # Selectors to exclude from content
    exclude_selectors: List[str] = field(default_factory=lambda: [
        'nav',
        'header',
        'footer',
        '.sidebar',
        '.toc',
        '.breadcrumb',
        'script',
        'style',
        'noscript',
    ])


@dataclass  
class DeepPageData:
    """Data extracted from a deeply crawled page."""
    url: str
    title: str = ""
    breadcrumb: List[str] = field(default_factory=list)
    headings: Dict[str, List[str]] = field(default_factory=dict)
    text_content: str = ""
    tables: List[Dict] = field(default_factory=list)
    code_blocks: List[str] = field(default_factory=list)
    internal_links: List[str] = field(default_factory=list)
    parent_url: str = ""
    depth: int = 0
    section_path: List[str] = field(default_factory=list)  # Hierarchy path like ["3 Absence Management", "Tables", "ANC_ABSENCE_AGREEMENTS_F"]
    word_count: int = 0
    
    def to_dict(self) -> dict:
        return {
            'url': self.url,
            'title': self.title,
            'breadcrumb': self.breadcrumb,
            'section_path': self.section_path,
            'headings': self.headings,
            'text_content': self.text_content,
            'tables': self.tables,
            'code_blocks': self.code_blocks,
            'internal_links': self.internal_links,
            'parent_url': self.parent_url,
            'depth': self.depth,
            'word_count': self.word_count,
        }
    
    def to_flat_dict(self) -> dict:
        """Flat dict for CSV export."""
        return {
            'url': self.url,
            'title': self.title,
            'breadcrumb': ' > '.join(self.breadcrumb),
            'section_path': ' > '.join(self.section_path),
            'h1': ' | '.join(self.headings.get('h1', [])),
            'h2': ' | '.join(self.headings.get('h2', [])),
            'h3': ' | '.join(self.headings.get('h3', [])),
            'text_content': self.text_content[:15000],
            'tables_count': len(self.tables),
            'code_blocks_count': len(self.code_blocks),
            'internal_links_count': len(self.internal_links),
            'depth': self.depth,
            'word_count': self.word_count,
        }


@dataclass
class DeepCrawlResult:
    """Result of a deep crawl operation."""
    pages: List[DeepPageData] = field(default_factory=list)
    stats: Dict = field(default_factory=dict)
    errors: List[Dict] = field(default_factory=list)
    hierarchy: Dict = field(default_factory=dict)  # Tree structure of crawled pages


class DeepDocCrawler:
    """
    Deep interactive crawler for documentation sites with expandable content.
    
    Uses Playwright to:
    1. Load pages with full JS rendering
    2. Click on expandable elements (accordions, dropdowns, tree nodes)
    3. Extract revealed content and links
    4. Recursively crawl discovered pages
    """
    
    def __init__(self, config: DeepCrawlConfig = None):
        self.config = config or DeepCrawlConfig()
        self.url_normalizer = URLNormalizer()
        
        # State
        self._visited_urls: Set[str] = set()
        self._pages: List[DeepPageData] = []
        self._errors: List[Dict] = []
        self._hierarchy: Dict = {}
        
        # Playwright
        self._playwright = None
        self._browser: Browser = None
        self._context: BrowserContext = None
        
        # Progress
        self._progress_callback: Optional[Callable] = None
        self._stop_requested = False
        
        # Stats
        self._start_time = 0
        self._expandables_clicked = 0
        self._links_discovered = 0
    
    def set_progress_callback(self, callback: Callable) -> None:
        """Set callback for progress updates: callback(pages_crawled, current_url, stats)"""
        self._progress_callback = callback
    
    def stop(self) -> None:
        """Request crawler to stop."""
        self._stop_requested = True
        logger.info("Stop requested")
    
    def _init_browser(self) -> None:
        """Initialize Playwright browser."""
        if self._playwright is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=self.config.headless,
                args=[
                    '--disable-gpu',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-web-security',
                ]
            )
            self._context = self._browser.new_context(
                user_agent=self.config.user_agent,
                viewport={
                    'width': self.config.viewport_width,
                    'height': self.config.viewport_height
                },
                locale='en-US',
                timezone_id='America/New_York',
            )
            logger.info("Playwright browser initialized for deep crawling")
    
    def _close_browser(self) -> None:
        """Close Playwright browser."""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
    
    def _expand_all_elements(self, page: Page) -> int:
        """
        Click on all expandable elements to reveal hidden content.
        Returns the number of elements expanded.
        """
        expanded_count = 0
        
        for selector in self.config.expandable_selectors:
            try:
                elements = page.query_selector_all(selector)
                
                for element in elements:
                    try:
                        # Check if element is visible and clickable
                        if not element.is_visible():
                            continue
                        
                        # Get element info for logging
                        tag = element.evaluate("el => el.tagName")
                        text = element.inner_text()[:50] if element.inner_text() else ""
                        
                        # Click to expand
                        element.click(timeout=2000)
                        expanded_count += 1
                        
                        # Small delay after click
                        page.wait_for_timeout(int(self.config.delay_after_click * 1000))
                        
                        logger.debug(f"Expanded: {tag} - {text}")
                        
                    except Exception as e:
                        # Element might not be clickable or already expanded
                        continue
                        
            except Exception as e:
                continue
        
        self._expandables_clicked += expanded_count
        return expanded_count
    
    def _extract_links_from_page(self, page: Page, base_url: str) -> List[str]:
        """Extract all internal links from the page after expansion."""
        links = set()
        base_domain = urlparse(base_url).netloc.lower()
        
        for selector in self.config.link_selectors:
            try:
                elements = page.query_selector_all(selector)
                
                for element in elements:
                    try:
                        href = element.get_attribute('href')
                        if not href:
                            continue
                        
                        # Resolve relative URLs
                        absolute_url = urljoin(page.url, href)
                        
                        # Skip non-http links
                        if not absolute_url.startswith(('http://', 'https://')):
                            continue
                        
                        # Skip external links
                        url_domain = urlparse(absolute_url).netloc.lower()
                        if url_domain != base_domain:
                            continue
                        
                        # Skip anchors, javascript, etc
                        if '#' in absolute_url:
                            absolute_url = absolute_url.split('#')[0]
                        
                        # Normalize
                        normalized = self.url_normalizer.normalize(absolute_url)
                        if normalized:
                            links.add(normalized)
                            
                    except Exception:
                        continue
                        
            except Exception:
                continue
        
        self._links_discovered += len(links)
        return list(links)
    
    def _extract_breadcrumb(self, page: Page) -> List[str]:
        """Extract breadcrumb navigation."""
        breadcrumb = []
        
        selectors = [
            '.breadcrumb a',
            '.breadcrumb li',
            '[aria-label="breadcrumb"] a',
            '.ohc-breadcrumb a',
            'nav[aria-label*="breadcrumb"] a',
        ]
        
        for selector in selectors:
            try:
                elements = page.query_selector_all(selector)
                if elements:
                    for el in elements:
                        text = el.inner_text().strip()
                        if text and text not in breadcrumb:
                            breadcrumb.append(text)
                    if breadcrumb:
                        break
            except Exception:
                continue
        
        return breadcrumb
    
    def _extract_section_path(self, page: Page) -> List[str]:
        """Extract the hierarchical section path from sidebar/TOC."""
        path = []
        
        selectors = [
            '.toc-item.active',
            '.nav-item.active',
            '.tree-item.selected',
            '[aria-current="page"]',
            '.ohc-sidebar-item.active',
            '.is-selected',
        ]
        
        for selector in selectors:
            try:
                # Find the active item and its ancestors
                active = page.query_selector(selector)
                if active:
                    # Try to get parent items
                    current = active
                    while current:
                        text = current.inner_text().strip().split('\n')[0][:100]
                        if text:
                            path.insert(0, text)
                        
                        # Move to parent
                        parent = current.evaluate("""el => {
                            const parent = el.closest('li, .toc-item, .tree-item, .nav-item');
                            if (parent && parent.parentElement) {
                                const grandparent = parent.parentElement.closest('li, .toc-item, .tree-item, .nav-item');
                                return grandparent ? true : false;
                            }
                            return false;
                        }""")
                        
                        if not parent:
                            break
                        
                        current = current.evaluate_handle("""el => {
                            const parent = el.closest('li, .toc-item, .tree-item, .nav-item');
                            if (parent && parent.parentElement) {
                                return parent.parentElement.closest('li, .toc-item, .tree-item, .nav-item');
                            }
                            return null;
                        }""").as_element()
                        
                        if not current:
                            break
                    
                    if path:
                        break
            except Exception:
                continue
        
        return path
    
    def _extract_content(self, page: Page) -> Dict[str, Any]:
        """Extract main content from the page."""
        content = {
            'title': '',
            'headings': {},
            'text': '',
            'tables': [],
            'code_blocks': [],
        }
        
        # Extract title
        try:
            title_el = page.query_selector('h1') or page.query_selector('title')
            if title_el:
                content['title'] = title_el.inner_text().strip()
        except Exception:
            pass
        
        # Find main content area
        main_content = None
        for selector in self.config.content_selectors:
            try:
                main_content = page.query_selector(selector)
                if main_content:
                    break
            except Exception:
                continue
        
        if not main_content:
            main_content = page.query_selector('body')
        
        if main_content:
            # Extract headings
            for level in range(1, 7):
                try:
                    headings = main_content.query_selector_all(f'h{level}')
                    if headings:
                        content['headings'][f'h{level}'] = [
                            h.inner_text().strip() for h in headings if h.inner_text().strip()
                        ]
                except Exception:
                    pass
            
            # Extract tables
            try:
                tables = main_content.query_selector_all('table')
                for table in tables:
                    try:
                        # Get table as structured data
                        table_data = {
                            'headers': [],
                            'rows': []
                        }
                        
                        # Headers
                        headers = table.query_selector_all('th')
                        table_data['headers'] = [h.inner_text().strip() for h in headers]
                        
                        # Rows
                        rows = table.query_selector_all('tr')
                        for row in rows:
                            cells = row.query_selector_all('td')
                            if cells:
                                table_data['rows'].append([c.inner_text().strip() for c in cells])
                        
                        if table_data['headers'] or table_data['rows']:
                            content['tables'].append(table_data)
                    except Exception:
                        pass
            except Exception:
                pass
            
            # Extract code blocks
            try:
                code_blocks = main_content.query_selector_all('pre, code')
                for block in code_blocks:
                    try:
                        code_text = block.inner_text().strip()
                        if code_text and len(code_text) > 10:
                            content['code_blocks'].append(code_text)
                    except Exception:
                        pass
            except Exception:
                pass
            
            # Extract text content
            try:
                # Remove excluded elements
                for selector in self.config.exclude_selectors:
                    try:
                        for el in main_content.query_selector_all(selector):
                            el.evaluate("el => el.remove()")
                    except Exception:
                        pass
                
                content['text'] = main_content.inner_text().strip()
                # Clean up whitespace
                content['text'] = re.sub(r'\n{3,}', '\n\n', content['text'])
                content['text'] = re.sub(r' {2,}', ' ', content['text'])
            except Exception:
                pass
        
        return content
    
    def _crawl_page(
        self,
        url: str,
        depth: int,
        parent_url: str,
        section_path: List[str],
        base_url: str
    ) -> Optional[DeepPageData]:
        """Crawl a single page with expansion."""
        
        # Check limits
        if len(self._pages) >= self.config.max_pages:
            return None
        
        if depth > self.config.max_depth:
            return None
        
        if self._stop_requested:
            return None
        
        # Normalize URL
        normalized_url = self.url_normalizer.normalize(url)
        if not normalized_url:
            return None
        
        # Check if already visited
        if normalized_url in self._visited_urls:
            return None
        
        self._visited_urls.add(normalized_url)
        
        logger.info(f"Deep crawling [{depth}]: {normalized_url}")
        
        try:
            # Create new page
            page = self._context.new_page()
            
            try:
                # Navigate to URL
                response = page.goto(
                    normalized_url,
                    timeout=self.config.timeout,
                    wait_until='load'
                )
                
                if response is None or response.status >= 400:
                    self._errors.append({
                        'url': normalized_url,
                        'error': f"HTTP {response.status if response else 'No response'}",
                        'depth': depth
                    })
                    return None
                
                # Wait for content
                page.wait_for_load_state('domcontentloaded')
                page.wait_for_timeout(2000)  # Extra time for JS
                
                # Expand all expandable elements
                expanded = self._expand_all_elements(page)
                logger.debug(f"Expanded {expanded} elements on {normalized_url}")
                
                # Wait after expansion
                if expanded > 0:
                    page.wait_for_timeout(1000)
                
                # Extract content
                content = self._extract_content(page)
                
                # Extract links
                links = self._extract_links_from_page(page, base_url)
                
                # Extract breadcrumb and section path
                breadcrumb = self._extract_breadcrumb(page)
                current_section = self._extract_section_path(page) or section_path
                
                # Build page data
                page_data = DeepPageData(
                    url=normalized_url,
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
                    word_count=len(content['text'].split())
                )
                
                # Progress callback
                if self._progress_callback:
                    try:
                        stats = {
                            'pages_crawled': len(self._pages) + 1,
                            'expandables_clicked': self._expandables_clicked,
                            'links_discovered': self._links_discovered,
                        }
                        self._progress_callback(len(self._pages) + 1, normalized_url, stats)
                    except Exception:
                        pass
                
                return page_data
                
            finally:
                page.close()
                
        except PlaywrightTimeout:
            self._errors.append({
                'url': normalized_url,
                'error': 'Timeout',
                'depth': depth
            })
            return None
        except Exception as e:
            self._errors.append({
                'url': normalized_url,
                'error': str(e),
                'depth': depth
            })
            return None
    
    def crawl(self, start_url: str) -> DeepCrawlResult:
        """
        Start deep crawling from the given URL.
        
        Args:
            start_url: Starting URL (documentation index page)
            
        Returns:
            DeepCrawlResult with all crawled pages
        """
        logger.info(f"Starting deep crawl of {start_url}")
        logger.info(f"Config: max_pages={self.config.max_pages}, max_depth={self.config.max_depth}")
        
        # Reset state
        self._visited_urls.clear()
        self._pages.clear()
        self._errors.clear()
        self._hierarchy.clear()
        self._stop_requested = False
        self._expandables_clicked = 0
        self._links_discovered = 0
        self._start_time = time.time()
        
        # Initialize browser
        self._init_browser()
        
        try:
            # Queue: (url, depth, parent_url, section_path)
            queue = [(start_url, 0, "", [])]
            
            while queue and not self._stop_requested:
                if len(self._pages) >= self.config.max_pages:
                    logger.info(f"Reached max pages limit: {self.config.max_pages}")
                    break
                
                url, depth, parent_url, section_path = queue.pop(0)
                
                # Crawl page
                page_data = self._crawl_page(url, depth, parent_url, section_path, start_url)
                
                if page_data:
                    self._pages.append(page_data)
                    
                    # Add discovered links to queue
                    if depth < self.config.max_depth:
                        for link in page_data.internal_links:
                            if link not in self._visited_urls:
                                queue.append((
                                    link,
                                    depth + 1,
                                    page_data.url,
                                    page_data.section_path.copy()
                                ))
                    
                    # Rate limiting
                    time.sleep(self.config.delay_between_pages)
            
        finally:
            self._close_browser()
        
        # Build stats
        elapsed = time.time() - self._start_time
        stats = {
            'pages_crawled': len(self._pages),
            'pages_failed': len(self._errors),
            'expandables_clicked': self._expandables_clicked,
            'links_discovered': self._links_discovered,
            'elapsed_time': round(elapsed, 2),
            'pages_per_second': round(len(self._pages) / elapsed, 2) if elapsed > 0 else 0,
        }
        
        logger.info(f"Deep crawl complete. Stats: {stats}")
        
        return DeepCrawlResult(
            pages=self._pages.copy(),
            stats=stats,
            errors=self._errors.copy(),
            hierarchy=self._build_hierarchy()
        )
    
    def _build_hierarchy(self) -> Dict:
        """Build a tree structure from crawled pages."""
        hierarchy = {'root': {}, 'pages': {}}
        
        for page in self._pages:
            # Add to flat lookup
            hierarchy['pages'][page.url] = {
                'title': page.title,
                'section_path': page.section_path,
                'depth': page.depth,
            }
            
            # Build tree
            if page.section_path:
                current = hierarchy['root']
                for section in page.section_path:
                    if section not in current:
                        current[section] = {'_pages': [], '_children': {}}
                    current[section]['_pages'].append(page.url)
                    current = current[section]['_children']
        
        return hierarchy
    
    def export_json(self, result: DeepCrawlResult, filepath: str) -> str:
        """Export results to JSON."""
        output_path = Path(filepath)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            'stats': result.stats,
            'hierarchy': result.hierarchy,
            'pages': [p.to_dict() for p in result.pages],
            'errors': result.errors,
        }
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        return str(output_path.absolute())
    
    def export_csv(self, result: DeepCrawlResult, filepath: str) -> str:
        """Export results to CSV."""
        import csv
        
        output_path = Path(filepath)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        if not result.pages:
            return str(output_path.absolute())
        
        rows = [p.to_flat_dict() for p in result.pages]
        
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        
        return str(output_path.absolute())


# Convenience function
def deep_crawl_docs(url: str, max_pages: int = 100, max_depth: int = 5) -> DeepCrawlResult:
    """
    Quick function to deep crawl a documentation site.
    
    Args:
        url: Starting URL
        max_pages: Maximum pages to crawl
        max_depth: Maximum depth to crawl
        
    Returns:
        DeepCrawlResult
    """
    config = DeepCrawlConfig(max_pages=max_pages, max_depth=max_depth)
    crawler = DeepDocCrawler(config)
    return crawler.crawl(url)

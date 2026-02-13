"""
Page Scraper
Extracts structured content from HTML pages.
"""

import logging
import re
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from bs4 import BeautifulSoup, Comment, NavigableString
from urllib.parse import urljoin
from .utils import ensure_joinable_base

logger = logging.getLogger(__name__)

# Choose the best available HTML parser — prefer lxml for speed,
# fall back to the stdlib html.parser so the crawler never crashes.
try:
    import lxml  # noqa: F401
    _BS_PARSER = "lxml"
except ImportError:
    _BS_PARSER = "html.parser"
    logger.info("lxml not installed — using html.parser (slower but functional)")

@dataclass
class PageData:
    """
    Structured data extracted from a web page.
    """
    url: str
    title: str = ""
    meta_description: str = ""
    headings: Dict[str, List[str]] = field(default_factory=dict)
    text_content: str = ""
    internal_links: List[str] = field(default_factory=list)
    external_links: List[str] = field(default_factory=list)
    images: List[Dict[str, str]] = field(default_factory=list)
    status_code: int = 200
    content_type: str = ""
    word_count: int = 0
    crawl_depth: int = 0
    error: str = ""
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON export."""
        return {
            'url': self.url,
            'title': self.title,
            'meta_description': self.meta_description,
            'headings': self.headings,
            'text_content': self.text_content,
            'internal_links': self.internal_links,
            'external_links': self.external_links,
            'images': self.images,
            'status_code': self.status_code,
            'content_type': self.content_type,
            'word_count': self.word_count,
            'crawl_depth': self.crawl_depth,
            'error': self.error
        }
    
    def to_flat_dict(self) -> dict:
        """Convert to flat dictionary for CSV export."""
        return {
            'url': self.url,
            'title': self.title,
            'meta_description': self.meta_description,
            'h1': ' | '.join(self.headings.get('h1', [])),
            'h2': ' | '.join(self.headings.get('h2', [])),
            'h3': ' | '.join(self.headings.get('h3', [])),
            'h4': ' | '.join(self.headings.get('h4', [])),
            'h5': ' | '.join(self.headings.get('h5', [])),
            'h6': ' | '.join(self.headings.get('h6', [])),
            'text_content': self.text_content[:10000] if self.text_content else "",  # Limit for CSV
            'internal_links_count': len(self.internal_links),
            'internal_links': ' | '.join(self.internal_links[:50]),  # Limit for CSV
            'external_links_count': len(self.external_links),
            'images_count': len(self.images),
            'status_code': self.status_code,
            'content_type': self.content_type,
            'word_count': self.word_count,
            'crawl_depth': self.crawl_depth,
            'error': self.error
        }


class PageScraper:
    """
    Extracts structured content from HTML pages.
    """
    
    # Tags always removed from soup (non-content, safe to strip globally)
    ALWAYS_STRIP_TAGS = {
        'script', 'style', 'noscript', 'iframe', 'svg', 'canvas',
        'template', 'picture', 'source', 'video', 'audio'
    }
    
    # Tags removed only during text extraction (may contain layout/nav text)
    TEXT_STRIP_TAGS = {
        'nav', 'footer', 'header', 'aside', 'form', 'button',
        'input', 'select', 'textarea', 'label'
    }
    
    # Tags that indicate main content areas
    MAIN_CONTENT_TAGS = {'main', 'article', 'section', 'div'}
    
    # IDs/classes that typically indicate main content
    MAIN_CONTENT_IDENTIFIERS = {
        'content', 'main', 'main-content', 'article', 'post',
        'entry', 'body-content', 'page-content', 'primary',
        'main-body', 'page-body', 'wrapper', 'container',
        'site-content', 'entry-content', 'post-content',
        'text-content', 'maincontent', 'mainContent'
    }
    
    # CSS class patterns that suggest navigation/non-content
    NAV_CLASS_PATTERNS = {
        'nav', 'menu', 'sidebar', 'footer', 'header',
        'breadcrumb', 'pagination', 'social', 'share',
        'cookie', 'consent', 'banner', 'modal', 'popup',
        'ad', 'advertisement', 'promo', 'related',
        'comment', 'widget'
    }
    
    def __init__(
        self,
        base_url: str,
        extract_images: bool = False,
        extract_external_links: bool = True,
        clean_text: bool = True,
        max_text_length: int = None
    ):
        """
        Initialize the scraper.
        
        Args:
            base_url: Base URL for resolving relative links
            extract_images: Whether to extract image information
            extract_external_links: Whether to extract external links
            clean_text: Whether to clean extracted text
            max_text_length: Maximum text content length (None for unlimited)
        """
        self.base_url = base_url
        self.extract_images = extract_images
        self.extract_external_links = extract_external_links
        self.clean_text = clean_text
        self.max_text_length = max_text_length
        
        # Extract base domain for link classification
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        self.base_domain = parsed.netloc.lower()
        if self.base_domain.startswith('www.'):
            self.base_domain = self.base_domain[4:]
    
    def scrape(
        self,
        html: str,
        url: str,
        status_code: int = 200,
        content_type: str = "",
        depth: int = 0
    ) -> PageData:
        """
        Scrape content from HTML.
        
        Args:
            html: HTML content
            url: Page URL
            status_code: HTTP status code
            content_type: Content-Type header value
            depth: Crawl depth of this page
            
        Returns:
            PageData object with extracted content
        """
        page_data = PageData(
            url=url,
            status_code=status_code,
            content_type=content_type,
            crawl_depth=depth
        )
        
        try:
            soup = BeautifulSoup(html, _BS_PARSER)
            
            # ── Parse-quality guard ──────────────────────────────────────
            # lxml occasionally produces an empty tree from valid HTML
            # (observed on MDN / Next.js SSR pages).  If the body has text
            # but lxml found zero <a> tags, retry with html.parser.
            if _BS_PARSER == "lxml":
                body = soup.find('body')
                body_len = len(body.get_text(strip=True)) if body else 0
                a_count = len(soup.find_all('a', href=True))
                if body_len > 200 and a_count == 0:
                    logger.info(
                        f"[PARSER] lxml produced 0 links from {body_len} chars "
                        f"of body text — retrying with html.parser"
                    )
                    soup = BeautifulSoup(html, 'html.parser')
            
            # Remove unwanted elements
            self._remove_unwanted_elements(soup)
            
            # Extract metadata
            page_data.title = self._extract_title(soup)
            page_data.meta_description = self._extract_meta_description(soup)
            
            # Extract headings
            page_data.headings = self._extract_headings(soup)
            
            # Extract text content
            page_data.text_content = self._extract_text(soup)
            page_data.word_count = len(page_data.text_content.split())
            
            # Extract links
            internal, external = self._extract_links(soup, url)
            page_data.internal_links = internal
            if self.extract_external_links:
                page_data.external_links = external
            
            # Extract images
            if self.extract_images:
                page_data.images = self._extract_images(soup, url)
            
            logger.info(
                f"[SCRAPE] {url[:70]} — title='{page_data.title[:50]}', "
                f"words={page_data.word_count:,}, "
                f"headings={sum(len(v) for v in page_data.headings.values())}, "
                f"links={len(internal)}"
            )
            
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            page_data.error = str(e)
        
        return page_data
    
    def _remove_unwanted_elements(self, soup: BeautifulSoup) -> None:
        """Remove script, style, and other non-content elements."""
        # Remove comments
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()
        
        # Only strip tags that never contain useful content
        for tag in self.ALWAYS_STRIP_TAGS:
            for element in soup.find_all(tag):
                element.decompose()
    
    def _extract_title(self, soup: BeautifulSoup) -> str:
        """Extract page title."""
        # Try <title> tag first
        title_tag = soup.find('title')
        if title_tag and title_tag.string:
            return self._clean_text(title_tag.string)
        
        # Try og:title
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            return self._clean_text(og_title['content'])
        
        # Try first H1
        h1 = soup.find('h1')
        if h1:
            return self._clean_text(h1.get_text())
        
        return ""
    
    def _extract_meta_description(self, soup: BeautifulSoup) -> str:
        """Extract meta description."""
        # Try standard meta description
        meta = soup.find('meta', attrs={'name': 'description'})
        if meta and meta.get('content'):
            return self._clean_text(meta['content'])
        
        # Try og:description
        og_desc = soup.find('meta', property='og:description')
        if og_desc and og_desc.get('content'):
            return self._clean_text(og_desc['content'])
        
        # Try twitter:description
        tw_desc = soup.find('meta', attrs={'name': 'twitter:description'})
        if tw_desc and tw_desc.get('content'):
            return self._clean_text(tw_desc['content'])
        
        return ""
    
    def _extract_headings(self, soup: BeautifulSoup) -> Dict[str, List[str]]:
        """Extract all headings (H1-H6)."""
        headings = {}
        
        for level in range(1, 7):
            tag_name = f'h{level}'
            found = soup.find_all(tag_name)
            if found:
                headings[tag_name] = [
                    self._clean_text(h.get_text())
                    for h in found
                    if h.get_text().strip()
                ]
        
        return headings
    
    def _extract_text(self, soup: BeautifulSoup) -> str:
        """Extract visible text content with aggressive fallback strategies."""
        import copy
        
        # Work on a deep copy so we don't mutate the soup used by other extractors
        text_soup = copy.deepcopy(soup)
        
        # Remove nav/header/footer/form elements only for text extraction
        for tag in self.TEXT_STRIP_TAGS:
            for element in text_soup.find_all(tag):
                element.decompose()
        
        # Also remove elements with nav-like classes/roles
        for element in text_soup.find_all(attrs={'role': ['navigation', 'banner', 'complementary', 'contentinfo']}):
            element.decompose()
        
        # Strategy 1: Try to find main content area
        main_content = self._find_main_content(text_soup)
        
        if main_content:
            text = main_content.get_text(separator='\n', strip=True)
            if len(text) >= 200:
                if self.clean_text:
                    text = self._clean_text(text)
                if self.max_text_length and len(text) > self.max_text_length:
                    text = text[:self.max_text_length] + "..."
                return text
        
        # Strategy 2: Find the largest text-rich div (content density heuristic)
        text = self._extract_by_density(text_soup)
        if text and len(text) >= 200:
            if self.clean_text:
                text = self._clean_text(text)
            if self.max_text_length and len(text) > self.max_text_length:
                text = text[:self.max_text_length] + "..."
            return text
        
        # Strategy 3: Fallback to full body text
        body = text_soup.find('body')
        if body:
            text = body.get_text(separator='\n', strip=True)
        else:
            text = text_soup.get_text(separator='\n', strip=True)
        
        if self.clean_text:
            text = self._clean_text(text)
        
        if self.max_text_length and len(text) > self.max_text_length:
            text = text[:self.max_text_length] + "..."
        
        return text
    
    def _extract_by_density(self, soup: BeautifulSoup) -> Optional[str]:
        """
        Find content by text density — the largest block of meaningful text.
        Useful for modern CMS/enterprise sites where main content isn't
        wrapped in semantic HTML tags.
        """
        candidates = []
        
        # Collect all substantial text blocks
        for tag in soup.find_all(['div', 'section', 'article', 'main']):
            # Skip if it looks like navigation
            tag_classes = ' '.join(tag.get('class', []) or []).lower()
            tag_id = (tag.get('id') or '').lower()
            combined = tag_classes + ' ' + tag_id
            
            is_nav = any(pat in combined for pat in self.NAV_CLASS_PATTERNS)
            if is_nav:
                continue
            
            text = tag.get_text(separator=' ', strip=True)
            
            # Count actual paragraphs / text nodes inside
            p_count = len(tag.find_all(['p', 'li', 'td', 'blockquote']))
            
            # Score: text length + bonus for paragraph-rich areas
            score = len(text) + (p_count * 50)
            
            if len(text) >= 100:
                candidates.append((score, text, tag))
        
        if not candidates:
            return None
        
        # Return the highest-scoring candidate
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    
    def _find_main_content(self, soup: BeautifulSoup) -> Optional[Any]:
        """Try to find the main content area of the page."""
        # Try semantic tags first
        for tag in ['main', 'article']:
            element = soup.find(tag)
            if element and len(element.get_text(strip=True)) >= 100:
                return element
        
        # Try role="main"
        element = soup.find(attrs={'role': 'main'})
        if element and len(element.get_text(strip=True)) >= 100:
            return element
        
        # Try common IDs and classes
        for identifier in self.MAIN_CONTENT_IDENTIFIERS:
            # Try as ID
            element = soup.find(id=identifier)
            if element and len(element.get_text(strip=True)) >= 100:
                return element
            # Try as class
            element = soup.find(class_=identifier)
            if element and len(element.get_text(strip=True)) >= 100:
                return element
        
        # Try partial class/id matches (e.g., "main-wrapper", "content-area")
        for element in soup.find_all(['div', 'section']):
            el_id = (element.get('id') or '').lower()
            el_class = ' '.join(element.get('class', []) or []).lower()
            combined = el_id + ' ' + el_class
            
            if any(kw in combined for kw in ['content', 'main', 'body', 'article', 'entry']):
                # Make sure it's not a nav-like element
                if not any(nav in combined for nav in ['nav', 'menu', 'sidebar', 'footer', 'header']):
                    if len(element.get_text(strip=True)) >= 100:
                        return element
        
        return None
    
    def _extract_links(
        self,
        soup: BeautifulSoup,
        current_url: str
    ) -> tuple:
        """
        Extract and classify links.
        
        Returns:
            Tuple of (internal_links, external_links)
        """
        internal_links = []
        external_links = []
        seen_urls = set()
        
        for anchor in soup.find_all('a', href=True):
            href = anchor['href'].strip()
            
            # Skip empty, javascript, and fragment-only links
            if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
                continue
            
            # Resolve relative URLs (ensure directory paths keep trailing /)
            absolute_url = urljoin(ensure_joinable_base(current_url), href)
            
            # Remove fragments for deduplication
            if '#' in absolute_url:
                absolute_url = absolute_url.split('#')[0]
            
            # Skip if already seen
            if absolute_url in seen_urls:
                continue
            seen_urls.add(absolute_url)
            
            # Classify as internal or external
            if self._is_internal_link(absolute_url):
                internal_links.append(absolute_url)
            else:
                external_links.append(absolute_url)
        
        return internal_links, external_links
    
    def _is_internal_link(self, url: str) -> bool:
        """Check if URL is internal to the base domain."""
        from urllib.parse import urlparse
        
        try:
            parsed = urlparse(url)
            
            # Must be http/https
            if parsed.scheme not in ('http', 'https'):
                return False
            
            domain = parsed.netloc.lower()
            if domain.startswith('www.'):
                domain = domain[4:]
            
            # Check if same domain or subdomain
            return domain == self.base_domain or domain.endswith('.' + self.base_domain)
            
        except Exception:
            return False
    
    def _extract_images(
        self,
        soup: BeautifulSoup,
        current_url: str
    ) -> List[Dict[str, str]]:
        """Extract image information."""
        images = []
        
        for img in soup.find_all('img'):
            src = img.get('src', '').strip()
            if not src:
                continue
            
            # Resolve relative URLs (ensure directory paths keep trailing /)
            absolute_src = urljoin(ensure_joinable_base(current_url), src)
            
            images.append({
                'src': absolute_src,
                'alt': img.get('alt', ''),
                'title': img.get('title', ''),
                'width': img.get('width', ''),
                'height': img.get('height', '')
            })
        
        return images
    
    def _clean_text(self, text: str) -> str:
        """Clean and normalize text."""
        if not text:
            return ""
        
        # Replace multiple whitespace with single space
        text = re.sub(r'\s+', ' ', text)
        
        # Remove leading/trailing whitespace
        text = text.strip()
        
        return text


def detect_js_required(html: str) -> bool:
    """
    Detect if a page likely requires JavaScript to render content.
    
    Args:
        html: HTML content
        
    Returns:
        True if JavaScript rendering is likely needed
    """
    soup = BeautifulSoup(html, _BS_PARSER)
    
    # Guard: if lxml produced a broken parse, retry with html.parser
    # so we don't falsely flag a valid SSR page as needing JS.
    if _BS_PARSER == "lxml":
        body = soup.find('body')
        body_len = len(body.get_text(strip=True)) if body else 0
        a_count = len(soup.find_all('a', href=True))
        if body_len > 200 and a_count == 0:
            soup = BeautifulSoup(html, 'html.parser')
    
    # Check if body is nearly empty (very strong indicator)
    body = soup.find('body')
    if body:
        body_text = body.get_text(strip=True)
        if len(body_text) < 150:
            return True
    
    # Check for common SPA indicators
    spa_roots = [
        soup.find('div', id='root'),
        soup.find('div', id='app'),
        soup.find('div', id='__next'),
        soup.find('div', id='__nuxt'),
        soup.find('app-root'),
    ]
    
    for indicator in spa_roots:
        if indicator and indicator.name == 'div' and not indicator.get_text(strip=True):
            return True
    
    # Check for noscript fallback messages
    noscript = soup.find('noscript')
    if noscript:
        noscript_text = noscript.get_text(strip=True).lower()
        if any(kw in noscript_text for kw in ['enable javascript', 'requires javascript', 'need javascript', 'browser']):
            return True
    
    # Check for heavy JS framework indicators with minimal body content
    if body and len(body.get_text(strip=True)) < 500:
        scripts = soup.find_all('script')
        
        # Many scripts + little content = likely SPA
        if len(scripts) > 15:
            return True
        
        js_framework_patterns = [
            'react', 'vue', 'angular', 'next', 'nuxt', 'gatsby',
            'webpack', 'chunk', 'bundle', 'app.', 'main.'
        ]
        
        framework_count = 0
        for script in scripts:
            src = (script.get('src') or '').lower()
            content = (script.string or '')[:200].lower()
            for pattern in js_framework_patterns:
                if pattern in src or pattern in content:
                    framework_count += 1
                    break
        
        if framework_count >= 2:
            return True
    
    # Check for JSON-LD / structured data without visible content
    json_ld = soup.find_all('script', type='application/ld+json')
    if json_ld and body and len(body.get_text(strip=True)) < 300:
        return True
    
    return False

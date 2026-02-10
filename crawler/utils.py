"""
Utility Functions
URL normalization, rate limiting, retry logic, and helper functions.
"""

import logging
import time
import hashlib
import re
from urllib.parse import urlparse, urlunparse, urljoin, parse_qs, urlencode
from typing import Optional, Callable, Any, Set
from functools import wraps
from threading import Lock
from collections import defaultdict
import random

logger = logging.getLogger(__name__)


class URLNormalizer:
    """
    Handles URL normalization to prevent duplicate crawling.
    Removes fragments, normalizes trailing slashes, sorts query params, etc.
    """
    
    # Common tracking parameters to remove
    TRACKING_PARAMS = {
        'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
        'fbclid', 'gclid', 'ref', 'source', 'mc_cid', 'mc_eid',
        '_ga', '_gid', 'dclid', 'zanpid', 'epik'
    }
    
    # File extensions to skip (non-HTML resources)
    SKIP_EXTENSIONS = {
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.webp', '.ico',
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.zip', '.rar', '.tar', '.gz', '.7z',
        '.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm',
        '.css', '.js', '.json', '.xml', '.rss', '.atom',
        '.woff', '.woff2', '.ttf', '.eot', '.otf'
    }
    
    def __init__(
        self,
        remove_tracking_params: bool = True,
        remove_fragments: bool = True,
        lowercase_path: bool = False,
        strip_www: bool = False
    ):
        """
        Initialize the URL normalizer.
        
        Args:
            remove_tracking_params: Remove common tracking query parameters
            remove_fragments: Remove URL fragments (#section)
            lowercase_path: Convert path to lowercase
            strip_www: Remove www. prefix from domain
        """
        self.remove_tracking_params = remove_tracking_params
        self.remove_fragments = remove_fragments
        self.lowercase_path = lowercase_path
        self.strip_www = strip_www
    
    def normalize(self, url: str, base_url: str = None) -> Optional[str]:
        """
        Normalize a URL for consistent comparison.
        
        Args:
            url: The URL to normalize
            base_url: Optional base URL for resolving relative URLs
            
        Returns:
            Normalized URL string or None if invalid
        """
        if not url:
            return None
        
        # Strip whitespace
        url = url.strip()
        
        # Skip javascript:, mailto:, tel:, data: URLs
        if url.lower().startswith(('javascript:', 'mailto:', 'tel:', 'data:', '#')):
            return None
        
        # Resolve relative URLs
        if base_url:
            url = urljoin(base_url, url)
        
        try:
            parsed = urlparse(url)
        except Exception:
            return None
        
        # Must have a valid scheme
        if parsed.scheme not in ('http', 'https'):
            return None
        
        # Must have a netloc (domain)
        if not parsed.netloc:
            return None
        
        # Process netloc
        netloc = parsed.netloc.lower()
        if self.strip_www and netloc.startswith('www.'):
            netloc = netloc[4:]
        
        # Process path
        path = parsed.path or '/'
        
        # Normalize path (remove double slashes, resolve . and ..)
        path = re.sub(r'/+', '/', path)
        
        if self.lowercase_path:
            path = path.lower()
        
        # Remove trailing slash unless it's the root
        if path != '/' and path.endswith('/'):
            path = path.rstrip('/')
        
        # Check for skip extensions
        lower_path = path.lower()
        for ext in self.SKIP_EXTENSIONS:
            if lower_path.endswith(ext):
                return None
        
        # Process query string
        query = parsed.query
        if query and self.remove_tracking_params:
            params = parse_qs(query, keep_blank_values=True)
            # Remove tracking params
            filtered_params = {
                k: v for k, v in params.items()
                if k.lower() not in self.TRACKING_PARAMS
            }
            # Sort params for consistent URLs
            query = urlencode(filtered_params, doseq=True)
        
        # Handle fragment
        fragment = '' if self.remove_fragments else parsed.fragment
        
        # Reconstruct URL
        normalized = urlunparse((
            parsed.scheme.lower(),
            netloc,
            path,
            parsed.params,
            query,
            fragment
        ))
        
        return normalized
    
    def is_same_domain(self, url: str, base_url: str) -> bool:
        """
        Check if URL belongs to the same domain as base URL.
        
        Args:
            url: URL to check
            base_url: Base domain URL
            
        Returns:
            True if same domain, False otherwise
        """
        try:
            url_parsed = urlparse(url)
            base_parsed = urlparse(base_url)
            
            url_domain = url_parsed.netloc.lower()
            base_domain = base_parsed.netloc.lower()
            
            # Strip www for comparison
            if url_domain.startswith('www.'):
                url_domain = url_domain[4:]
            if base_domain.startswith('www.'):
                base_domain = base_domain[4:]
            
            return url_domain == base_domain
            
        except Exception:
            return False
    
    # File extensions that indicate a leaf page (not a directory)
    FILE_EXTENSIONS = {
        '.html', '.htm', '.php', '.asp', '.aspx', '.jsp', '.shtml',
        '.xhtml', '.cfm', '.cgi', '.pl', '.py', '.rb'
    }
    
    def _get_scope_path(self, start_path: str) -> str:
        """
        Determine the scope path from a start URL path.
        
        If the path ends in a file extension (e.g. /blog/post.html),
        use the parent directory (/blog/) as the scope.
        If the path is a directory-style path (e.g. /blog/posts),
        use it as-is.
        
        Args:
            start_path: URL path from the start URL
            
        Returns:
            Scope path to use for boundary checking
        """
        # Normalize
        if start_path != '/' and start_path.endswith('/'):
            start_path = start_path.rstrip('/')
        
        if start_path == '/' or start_path == '':
            return '/'
        
        # Check if the last segment has a file extension
        last_segment = start_path.rsplit('/', 1)[-1] if '/' in start_path else start_path
        lower_last = last_segment.lower()
        
        for ext in self.FILE_EXTENSIONS:
            if lower_last.endswith(ext):
                # Use parent directory as scope
                parent = start_path.rsplit('/', 1)[0]
                return parent if parent else '/'
        
        return start_path
    
    def is_within_scope(self, url: str, start_url: str) -> bool:
        """
        Check if URL is within the crawling scope defined by the start URL.
        
        Scope rules:
        - If start_url is a root domain (path is "/" or empty), crawl entire domain
        - If start_url is a sub-URL (has a specific path), only crawl URLs under that path
        
        Args:
            url: URL to check
            start_url: The original start URL that defines the scope
            
        Returns:
            True if URL is within scope, False otherwise
            
        Examples:
            start_url = "https://example.com" -> crawls entire domain
            start_url = "https://example.com/blog" -> only crawls /blog/**
            start_url = "https://example.com/blog/post.html" -> crawls /blog/**
        """
        try:
            url_parsed = urlparse(url)
            start_parsed = urlparse(start_url)
            
            # First check: must be same domain
            url_domain = url_parsed.netloc.lower()
            start_domain = start_parsed.netloc.lower()
            
            # Strip www for comparison
            if url_domain.startswith('www.'):
                url_domain = url_domain[4:]
            if start_domain.startswith('www.'):
                start_domain = start_domain[4:]
            
            if url_domain != start_domain:
                return False
            
            # Get the scope path (handles file-based URLs like /blog/post.html -> /blog)
            raw_start_path = start_parsed.path or '/'
            scope_path = self._get_scope_path(raw_start_path)
            
            # If scope is root domain, allow all paths on this domain
            if scope_path == '/' or scope_path == '':
                return True
            
            # Get the URL's path
            url_path = url_parsed.path or '/'
            
            # Normalize URL path
            if url_path != '/' and url_path.endswith('/'):
                url_path = url_path.rstrip('/')
            
            # Check if URL path starts with the scope path
            # Must be exact match or followed by / to avoid partial matches
            # e.g., /blog should match /blog and /blog/post but not /blogger
            if url_path == scope_path:
                return True
            
            if url_path.startswith(scope_path + '/'):
                return True
            
            return False
            
        except Exception:
            return False
    
    def get_scope_info(self, start_url: str) -> dict:
        """
        Get information about the crawling scope for a given start URL.
        
        Args:
            start_url: The start URL
            
        Returns:
            Dictionary with scope details
        """
        try:
            parsed = urlparse(start_url)
            raw_path = parsed.path or '/'
            
            # Get the effective scope path (handles file-based URLs)
            scope_path = self._get_scope_path(raw_path)
            
            is_root = scope_path == '/' or scope_path == ''
            
            domain = parsed.netloc.lower()
            if domain.startswith('www.'):
                domain = domain[4:]
            
            return {
                'is_root_domain': is_root,
                'base_domain': domain,
                'base_path': scope_path if not is_root else '/',
                'scope_description': (
                    f"Entire domain: {domain}"
                    if is_root
                    else f"Sub-path only: {domain}{scope_path}/**"
                ),
                'example_allowed': (
                    f"https://{domain}/any/path"
                    if is_root
                    else f"https://{domain}{scope_path}/subpage"
                ),
                'example_blocked': (
                    "External domains only"
                    if is_root
                    else f"https://{domain}/other/path"
                )
            }
        except Exception:
            return {
                'is_root_domain': True,
                'base_domain': '',
                'base_path': '/',
                'scope_description': 'Unknown',
                'example_allowed': '',
                'example_blocked': ''
            }
    
    def get_url_hash(self, url: str) -> str:
        """Generate a hash for a URL (useful for deduplication)."""
        return hashlib.md5(url.encode('utf-8')).hexdigest()


class RateLimiter:
    """
    Token bucket rate limiter for controlling request frequency.
    Thread-safe implementation.
    """
    
    def __init__(
        self,
        requests_per_second: float = 1.0,
        burst_size: int = 1,
        per_domain: bool = True
    ):
        """
        Initialize the rate limiter.
        
        Args:
            requests_per_second: Maximum requests per second
            burst_size: Maximum burst of requests allowed
            per_domain: Whether to rate limit per domain
        """
        self.requests_per_second = requests_per_second
        self.burst_size = burst_size
        self.per_domain = per_domain
        
        self._tokens: defaultdict = defaultdict(lambda: burst_size)
        self._last_update: defaultdict = defaultdict(time.time)
        self._lock = Lock()
    
    def _get_key(self, url: str) -> str:
        """Get rate limiting key for URL."""
        if self.per_domain:
            parsed = urlparse(url)
            return parsed.netloc.lower()
        return "global"
    
    def _update_tokens(self, key: str) -> None:
        """Update token count based on elapsed time."""
        now = time.time()
        elapsed = now - self._last_update[key]
        self._tokens[key] = min(
            self.burst_size,
            self._tokens[key] + elapsed * self.requests_per_second
        )
        self._last_update[key] = now
    
    def acquire(self, url: str) -> float:
        """
        Acquire a token for making a request. Returns wait time.
        
        Args:
            url: URL for domain-based rate limiting
            
        Returns:
            Time to wait before making request (0 if immediate)
        """
        key = self._get_key(url)
        
        with self._lock:
            self._update_tokens(key)
            
            if self._tokens[key] >= 1:
                self._tokens[key] -= 1
                return 0
            
            # Calculate wait time
            wait_time = (1 - self._tokens[key]) / self.requests_per_second
            return wait_time
    
    def wait(self, url: str) -> None:
        """
        Wait until a request can be made.
        
        Args:
            url: URL for domain-based rate limiting
        """
        wait_time = self.acquire(url)
        if wait_time > 0:
            logger.debug(f"Rate limiting: waiting {wait_time:.2f}s")
            time.sleep(wait_time)
            # Re-acquire after waiting
            self.acquire(url)
    
    def set_crawl_delay(self, url: str, delay: float) -> None:
        """
        Set crawl delay from robots.txt.
        
        Args:
            url: URL to set delay for
            delay: Delay in seconds
        """
        if delay > 0:
            self.requests_per_second = min(
                self.requests_per_second,
                1.0 / delay
            )
            logger.info(f"Crawl delay set to {delay}s (from robots.txt)")


class RetryHandler:
    """
    Handles retry logic with exponential backoff.
    """
    
    # HTTP status codes that should trigger a retry
    RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
    
    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True
    ):
        """
        Initialize the retry handler.
        
        Args:
            max_retries: Maximum number of retry attempts
            base_delay: Initial delay between retries in seconds
            max_delay: Maximum delay between retries
            exponential_base: Base for exponential backoff
            jitter: Add random jitter to prevent thundering herd
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
    
    def calculate_delay(self, attempt: int) -> float:
        """
        Calculate delay for given retry attempt.
        
        Args:
            attempt: Current attempt number (0-indexed)
            
        Returns:
            Delay in seconds
        """
        delay = self.base_delay * (self.exponential_base ** attempt)
        delay = min(delay, self.max_delay)
        
        if self.jitter:
            # Add random jitter (±25%)
            jitter_range = delay * 0.25
            delay += random.uniform(-jitter_range, jitter_range)
        
        return max(0, delay)
    
    def should_retry(self, status_code: int, attempt: int) -> bool:
        """
        Determine if request should be retried.
        
        Args:
            status_code: HTTP status code
            attempt: Current attempt number
            
        Returns:
            True if should retry, False otherwise
        """
        if attempt >= self.max_retries:
            return False
        return status_code in self.RETRYABLE_STATUS_CODES
    
    def execute_with_retry(
        self,
        func: Callable,
        *args,
        **kwargs
    ) -> Any:
        """
        Execute function with retry logic.
        
        Args:
            func: Function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments
            
        Returns:
            Function result
            
        Raises:
            Last exception if all retries fail
        """
        last_exception = None
        
        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                
                if attempt < self.max_retries:
                    delay = self.calculate_delay(attempt)
                    logger.warning(
                        f"Attempt {attempt + 1} failed: {e}. "
                        f"Retrying in {delay:.2f}s..."
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"All {self.max_retries + 1} attempts failed")
        
        raise last_exception


class ContentHasher:
    """
    Generates content hashes for detecting duplicate pages.
    """
    
    @staticmethod
    def hash_content(content: str) -> str:
        """Generate hash of page content."""
        # Normalize whitespace before hashing
        normalized = ' '.join(content.split())
        return hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    
    @staticmethod
    def simhash(content: str, bits: int = 64) -> int:
        """
        Generate SimHash for near-duplicate detection.
        
        Args:
            content: Text content
            bits: Number of bits in hash
            
        Returns:
            SimHash value
        """
        tokens = content.lower().split()
        v = [0] * bits
        
        for token in tokens:
            token_hash = int(hashlib.md5(token.encode('utf-8')).hexdigest(), 16)
            for i in range(bits):
                bitmask = 1 << i
                if token_hash & bitmask:
                    v[i] += 1
                else:
                    v[i] -= 1
        
        fingerprint = 0
        for i in range(bits):
            if v[i] >= 0:
                fingerprint |= (1 << i)
        
        return fingerprint
    
    @staticmethod
    def hamming_distance(hash1: int, hash2: int) -> int:
        """Calculate Hamming distance between two hashes."""
        return bin(hash1 ^ hash2).count('1')


class ProgressTracker:
    """
    Tracks crawling progress for reporting.
    """
    
    def __init__(self):
        self.pages_crawled = 0
        self.pages_failed = 0
        self.pages_skipped = 0
        self.start_time = None
        self.end_time = None
        self._lock = Lock()
    
    def start(self) -> None:
        """Mark crawl start."""
        self.start_time = time.time()
    
    def finish(self) -> None:
        """Mark crawl end."""
        self.end_time = time.time()
    
    def increment_crawled(self) -> int:
        """Increment crawled counter."""
        with self._lock:
            self.pages_crawled += 1
            return self.pages_crawled
    
    def increment_failed(self) -> int:
        """Increment failed counter."""
        with self._lock:
            self.pages_failed += 1
            return self.pages_failed
    
    def increment_skipped(self) -> int:
        """Increment skipped counter."""
        with self._lock:
            self.pages_skipped += 1
            return self.pages_skipped
    
    @property
    def total_processed(self) -> int:
        """Total pages processed."""
        return self.pages_crawled + self.pages_failed + self.pages_skipped
    
    @property
    def elapsed_time(self) -> float:
        """Elapsed time in seconds."""
        if self.start_time is None:
            return 0
        end = self.end_time or time.time()
        return end - self.start_time
    
    @property
    def pages_per_second(self) -> float:
        """Crawling rate."""
        elapsed = self.elapsed_time
        if elapsed == 0:
            return 0
        return self.pages_crawled / elapsed
    
    def get_stats(self) -> dict:
        """Get current statistics."""
        return {
            'pages_crawled': self.pages_crawled,
            'pages_failed': self.pages_failed,
            'pages_skipped': self.pages_skipped,
            'total_processed': self.total_processed,
            'elapsed_time': round(self.elapsed_time, 2),
            'pages_per_second': round(self.pages_per_second, 2)
        }


def extract_domain(url: str) -> str:
    """Extract domain from URL."""
    parsed = urlparse(url)
    return parsed.netloc.lower()


def is_valid_url(url: str) -> bool:
    """Check if URL is valid."""
    try:
        parsed = urlparse(url)
        return all([parsed.scheme in ('http', 'https'), parsed.netloc])
    except Exception:
        return False


def clean_text(text: str) -> str:
    """Clean and normalize text content."""
    if not text:
        return ""
    
    # Replace multiple whitespace with single space
    text = re.sub(r'\s+', ' ', text)
    
    # Remove leading/trailing whitespace
    text = text.strip()
    
    return text

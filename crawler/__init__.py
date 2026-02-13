"""
Web Crawler Package
A production-grade web crawler with support for static and JavaScript-rendered pages.

CLI Usage:
    python -m crawler <url> [options]
    
    Options:
        --max_depth     Maximum crawl depth (default: 5)
        --max_pages     Maximum pages to crawl (default: 150)
        --timeout       Per-page timeout in seconds (default: 20)
        --rate          Delay between requests (default: 1.0)
        --no_js         Disable JavaScript rendering
        --output_json   Export to JSON file
        --output_csv    Export to CSV file
        --output_docx   Export to DOCX file
"""

from .crawler import WebCrawler, CrawlConfig, CrawlResult
from .scraper import PageScraper, PageData
from .robots import RobotsHandler
from .utils import URLNormalizer, RateLimiter, RetryHandler
from .deep_crawler import DeepDocCrawler, DeepCrawlConfig, DeepCrawlResult, DeepPageData, deep_crawl_docs
from .run_config import CrawlerRunConfig
from .scope_filter import ScopeFilter, is_within_scope
from . import interaction_policy

__all__ = [
    'WebCrawler',
    'CrawlConfig',
    'CrawlResult',
    'PageScraper',
    'PageData',
    'RobotsHandler',
    'URLNormalizer',
    'RateLimiter',
    'RetryHandler',
    # Deep crawler
    'DeepDocCrawler',
    'DeepCrawlConfig', 
    'DeepCrawlResult',
    'DeepPageData',
    'deep_crawl_docs',
    # P0 additions
    'CrawlerRunConfig',
    'interaction_policy',
    # Scope filtering
    'ScopeFilter',
    'is_within_scope',
]

__version__ = '2.0.0'

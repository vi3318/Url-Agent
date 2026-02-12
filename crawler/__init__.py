"""
Web Crawler Package
A production-grade web crawler with support for static and JavaScript-rendered pages.
"""

from .crawler import WebCrawler, CrawlConfig, CrawlResult
from .scraper import PageScraper, PageData
from .robots import RobotsHandler
from .utils import URLNormalizer, RateLimiter, RetryHandler
from .deep_crawler import DeepDocCrawler, DeepCrawlConfig, DeepCrawlResult, DeepPageData, deep_crawl_docs

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
]

__version__ = '1.0.0'

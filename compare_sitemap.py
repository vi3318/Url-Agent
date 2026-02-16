#!/usr/bin/env python3
"""
Sitemap vs Crawl Output Comparison Tool
========================================
Compares a sitemap.xml against the crawler's JSON output to measure
URL coverage — proving the crawler found all (or most) discoverable pages.

Usage:
    python3 compare_sitemap.py sitemap.xml books_crawl.json

Output:
    - URLs in both (matched)
    - URLs in sitemap but NOT crawled (missed)
    - URLs crawled but NOT in sitemap (extra discoveries)
    - Coverage percentage
"""

import json
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlparse


def normalize_url(url: str) -> str:
    """Normalize a URL for fair comparison."""
    url = url.strip().rstrip('/')
    parsed = urlparse(url)
    # Lowercase scheme and host
    normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}"
    if normalized.endswith('/index.html'):
        normalized = normalized[:-len('/index.html')]
    return normalized


def load_sitemap_urls(sitemap_path: str) -> set:
    """Parse sitemap.xml and extract all <loc> URLs."""
    urls = set()
    tree = ET.parse(sitemap_path)
    root = tree.getroot()

    # Handle namespace (sitemaps use xmlns)
    ns = ''
    if root.tag.startswith('{'):
        ns = root.tag.split('}')[0] + '}'

    for url_elem in root.findall(f'.//{ns}loc'):
        if url_elem.text:
            urls.add(normalize_url(url_elem.text))

    return urls


def load_crawl_urls(json_path: str) -> set:
    """Load crawled URLs from the crawler's JSON output."""
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    urls = set()
    pages = data.get('pages', [])
    for page in pages:
        url = page.get('url', '')
        if url:
            urls.add(normalize_url(url))

    return urls


def compare(sitemap_urls: set, crawl_urls: set):
    """Compare and report."""
    matched = sitemap_urls & crawl_urls
    missed = sitemap_urls - crawl_urls
    extra = crawl_urls - sitemap_urls

    total_sitemap = len(sitemap_urls)
    total_crawled = len(crawl_urls)
    coverage = (len(matched) / total_sitemap * 100) if total_sitemap > 0 else 0

    print("=" * 60)
    print("  SITEMAP vs CRAWL — COMPARISON REPORT")
    print("=" * 60)
    print(f"  Sitemap URLs:      {total_sitemap}")
    print(f"  Crawled URLs:      {total_crawled}")
    print(f"  Matched:           {len(matched)}")
    print(f"  Missed (in sitemap, not crawled): {len(missed)}")
    print(f"  Extra (crawled, not in sitemap):  {len(extra)}")
    print(f"  Coverage:          {coverage:.1f}%")
    print("=" * 60)

    if matched:
        print(f"\n✅ MATCHED URLs ({len(matched)}):")
        for url in sorted(matched):
            print(f"   {url}")

    if missed:
        print(f"\n❌ MISSED URLs ({len(missed)}) — in sitemap but NOT crawled:")
        for url in sorted(missed):
            print(f"   {url}")

    if extra:
        print(f"\n➕ EXTRA URLs ({len(extra)}) — crawled but NOT in sitemap:")
        for url in sorted(extra):
            print(f"   {url}")

    print(f"\n{'=' * 60}")
    print(f"  FINAL COVERAGE: {coverage:.1f}%")
    if coverage >= 90:
        print("  ✅ EXCELLENT — crawler captured 90%+ of sitemap URLs")
    elif coverage >= 70:
        print("  ⚠️  GOOD — some URLs missed (may need higher --pages or --depth)")
    else:
        print("  ❌ LOW — check scope, depth limits, or JS rendering needs")
    print(f"{'=' * 60}")


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 compare_sitemap.py <sitemap.xml> <crawl_output.json>")
        print("Example: python3 compare_sitemap.py sitemap.xml books_crawl.json")
        sys.exit(1)

    sitemap_path = sys.argv[1]
    json_path = sys.argv[2]

    print(f"Loading sitemap: {sitemap_path}")
    sitemap_urls = load_sitemap_urls(sitemap_path)
    print(f"  → {len(sitemap_urls)} URLs found")

    print(f"Loading crawl output: {json_path}")
    crawl_urls = load_crawl_urls(json_path)
    print(f"  → {len(crawl_urls)} URLs found")
    print()

    compare(sitemap_urls, crawl_urls)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Sitemap vs Crawl Output Comparison Tool
========================================
Compares a sitemap.xml (or a second crawl JSON) against the crawler's
JSON output to measure URL coverage in both directions.

Usage:
    python3 compare_sitemap.py sitemap.xml  crawl_output.json
    python3 compare_sitemap.py crawl_a.json crawl_b.json

Output:
    - Bidirectional coverage analysis
    - URLs in both (matched)
    - URLs missed / extra
    - Combined site‑size estimate
"""

import json
import sys
import os
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, parse_qs, urlencode


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Aggressively normalize a URL for fair comparison.

    * lowercases scheme + host
    * strips trailing slash
    * strips default index files (index.html, index.htm, index.php)
    * strips fragments (#...)
    * strips tracking / non‑semantic query params (utm_*, ref, source)
    * sorts remaining query params
    * collapses // in path
    """
    url = url.strip()
    parsed = urlparse(url)

    scheme = parsed.scheme.lower() or 'https'
    host = parsed.netloc.lower()
    # Remove default port
    if host.endswith(':443') and scheme == 'https':
        host = host[:-4]
    if host.endswith(':80') and scheme == 'http':
        host = host[:-3]
    # Remove www. prefix for matching
    if host.startswith('www.'):
        host = host[4:]

    path = parsed.path
    # Collapse double slashes
    while '//' in path:
        path = path.replace('//', '/')
    # Strip trailing slash
    path = path.rstrip('/')
    # Strip default index files
    for idx in ('/index.html', '/index.htm', '/index.php'):
        if path.endswith(idx):
            path = path[:-len(idx)]
            break

    # Strip fragment
    # (already excluded by urlparse fragment handling)

    # Clean query params — drop tracking params, sort the rest
    STRIP_PARAMS = {
        'utm_source', 'utm_medium', 'utm_campaign', 'utm_term',
        'utm_content', 'ref', 'source', 'fbclid', 'gclid',
    }
    qs = parse_qs(parsed.query, keep_blank_values=True)
    cleaned = {k: v for k, v in qs.items() if k.lower() not in STRIP_PARAMS}
    query = urlencode(sorted(cleaned.items()), doseq=True) if cleaned else ''

    normalized = f"{scheme}://{host}{path}"
    if query:
        normalized += f"?{query}"
    return normalized


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

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


def load_urls(path: str) -> set:
    """Auto-detect format (XML sitemap or crawler JSON) and load URLs."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.xml':
        return load_sitemap_urls(path)
    elif ext == '.json':
        return load_crawl_urls(path)
    else:
        # Try JSON first, fall back to XML
        try:
            return load_crawl_urls(path)
        except (json.JSONDecodeError, KeyError):
            return load_sitemap_urls(path)


# ---------------------------------------------------------------------------
# Path analysis helpers
# ---------------------------------------------------------------------------

def _common_prefix(urls: set) -> str:
    """Find the longest common URL path prefix."""
    if not urls:
        return ''
    paths = sorted(urls)
    first, last = paths[0], paths[-1]
    i = 0
    while i < len(first) and i < len(last) and first[i] == last[i]:
        i += 1
    prefix = first[:i]
    # Trim to last /
    slash = prefix.rfind('/')
    return prefix[:slash + 1] if slash > 0 else prefix


def _count_in_scope(urls: set, scope_prefix: str) -> int:
    """Count URLs that fall within a given path prefix."""
    return sum(1 for u in urls if u.startswith(scope_prefix))


# ---------------------------------------------------------------------------
# Comparison & report
# ---------------------------------------------------------------------------

def compare(source_urls: set, crawl_urls: set, source_label: str = "Sitemap"):
    """Compare and print a comprehensive bidirectional report."""
    matched = source_urls & crawl_urls
    missed = source_urls - crawl_urls
    extra = crawl_urls - source_urls

    total_source = len(source_urls)
    total_crawled = len(crawl_urls)
    combined = source_urls | crawl_urls
    total_combined = len(combined)

    coverage = (len(matched) / total_source * 100) if total_source > 0 else 0
    crawl_precision = (len(matched) / total_crawled * 100) if total_crawled > 0 else 0

    # Scope analysis
    scope_prefix = _common_prefix(source_urls)
    extras_in_scope = _count_in_scope(extra, scope_prefix) if scope_prefix else 0

    print()
    print("=" * 64)
    print("  SITEMAP vs CRAWL — COMPARISON REPORT")
    print("=" * 64)

    print(f"\n  📊  NUMBERS")
    print(f"  {'─' * 50}")
    print(f"  {source_label} URLs :       {total_source}")
    print(f"  Crawled URLs :       {total_crawled}")
    print(f"  Matched :            {len(matched)}")
    print(f"  Missed  (in {source_label.lower()}, not crawled) : {len(missed)}")
    print(f"  Extra   (crawled, not in {source_label.lower()}) : {len(extra)}")

    print(f"\n  📈  COVERAGE ANALYSIS")
    print(f"  {'─' * 50}")
    print(f"  {source_label} coverage  :  {coverage:.1f}%  "
          f"({len(matched)}/{total_source} {source_label.lower()} URLs found)")
    print(f"  Crawl precision  :  {crawl_precision:.1f}%  "
          f"({len(matched)}/{total_crawled} crawled URLs were in {source_label.lower()})")
    if extras_in_scope:
        print(f"  In-scope extras  :  {extras_in_scope}/{len(extra)} extra URLs "
              f"are under the same path")

    print(f"\n  🌐  SITE SIZE ESTIMATE")
    print(f"  {'─' * 50}")
    print(f"  Combined unique URLs : {total_combined}")
    print(f"  {source_label} found     : {total_source}/{total_combined} = "
          f"{total_source / total_combined * 100:.1f}% of estimated site")
    print(f"  Crawler found     : {total_crawled}/{total_combined} = "
          f"{total_crawled / total_combined * 100:.1f}% of estimated site")

    if total_crawled < total_source:
        suggested = min(total_source + 50, total_source * 2)
        print(f"\n  💡  TIP: Re-run with --pages {suggested} to match "
              f"{source_label.lower()} size")

    # Print URL lists (capped for readability)
    MAX_SHOW = 30

    if matched:
        print(f"\n✅ MATCHED URLs ({len(matched)}):")
        for url in sorted(matched)[:MAX_SHOW]:
            print(f"   {url}")
        if len(matched) > MAX_SHOW:
            print(f"   ... and {len(matched) - MAX_SHOW} more")

    if missed:
        showing = min(len(missed), MAX_SHOW)
        print(f"\n❌ MISSED URLs ({len(missed)}) — in {source_label.lower()} but NOT crawled:")
        for url in sorted(missed)[:showing]:
            print(f"   {url}")
        if len(missed) > showing:
            print(f"   ... and {len(missed) - showing} more")

    if extra:
        showing = min(len(extra), MAX_SHOW)
        print(f"\n➕ EXTRA URLs ({len(extra)}) — crawled but NOT in {source_label.lower()}:")
        if extras_in_scope == len(extra):
            print(f"   (all {len(extra)} are valid pages under {scope_prefix})")
        for url in sorted(extra)[:showing]:
            print(f"   {url}")
        if len(extra) > showing:
            print(f"   ... and {len(extra) - showing} more")

    # Final verdict
    print(f"\n{'=' * 64}")
    print(f"  FINAL COVERAGE: {coverage:.1f}%", end="")
    if coverage < 50 and total_crawled < total_source:
        gap = total_source - total_crawled
        print(f"  (page limit {total_crawled} << {source_label.lower()} size {total_source})")
        print(f"  ⚠️  Low coverage is expected — crawler page limit ({total_crawled}) "
              f"is much smaller than {source_label.lower()} ({total_source}).")
        print(f"     Increase --pages to at least {total_source} for a fair comparison.")
    elif coverage >= 90:
        print()
        print("  ✅ EXCELLENT — crawler captured 90%+ of sitemap URLs")
    elif coverage >= 70:
        print()
        print("  ⚠️  GOOD — some URLs missed (may need higher --pages or --depth)")
    elif coverage >= 40:
        print()
        print("  ⚠️  MODERATE — increase --pages / --depth or check JS rendering")
    else:
        print()
        print("  ❌ LOW — check scope, depth limits, page cap, or JS rendering needs")
    print(f"{'=' * 64}")


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 compare_sitemap.py <source> <crawl_output>")
        print()
        print("  source:       sitemap.xml or another crawl .json")
        print("  crawl_output: your crawler's .json export")
        print()
        print("Examples:")
        print("  python3 compare_sitemap.py sitemap.xml  books_crawl.json")
        print("  python3 compare_sitemap.py crawl_a.json crawl_b.json")
        sys.exit(1)

    source_path = sys.argv[1]
    crawl_path = sys.argv[2]

    source_ext = os.path.splitext(source_path)[1].lower()
    source_label = "Sitemap" if source_ext == '.xml' else "Source"

    print(f"Loading {source_label.lower()}: {source_path}")
    source_urls = load_urls(source_path)
    print(f"  → {len(source_urls)} URLs found")

    print(f"Loading crawl output: {crawl_path}")
    crawl_urls = load_urls(crawl_path)
    print(f"  → {len(crawl_urls)} URLs found")

    compare(source_urls, crawl_urls, source_label)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Interactive CLI for the Web Crawler
====================================
Unified mode: Playwright-based crawling with automatic JS/HTML detection.
The system auto-detects whether each page needs JS expansion or is simple HTML,
eliminating the need for users to choose between standard and deep mode.

All configuration flows through ``CrawlerRunConfig`` — the single source of
truth for defaults, CLI overrides, and interactive prompts.

Run with: python -m crawler
"""

import argparse
import logging
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .run_config import CrawlerRunConfig

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def get_user_input(prompt: str, default: str = None) -> str:
    """Get user input with optional default value."""
    if default:
        full_prompt = f"{prompt} [{default}]: "
    else:
        full_prompt = f"{prompt}: "
    response = input(full_prompt).strip()
    return response if response else default


def get_choice(prompt: str, options: list, default: int = 1) -> int:
    """Get user choice from numbered options."""
    print(f"\n{prompt}")
    for i, option in enumerate(options, 1):
        marker = " (default)" if i == default else ""
        print(f"  {i}) {option}{marker}")
    while True:
        response = input(f"Enter choice [1-{len(options)}]: ").strip()
        if not response:
            return default
        try:
            choice = int(response)
            if 1 <= choice <= len(options):
                return choice
        except ValueError:
            pass
        print(f"Please enter a number between 1 and {len(options)}")


# ---------------------------------------------------------------------------
# Interactive flow → builds CrawlerRunConfig
# ---------------------------------------------------------------------------

def _base_name_from_url(url: str) -> str:
    """Derive a filesystem-safe base name from a URL."""
    parsed = urlparse(url)
    base = parsed.netloc.replace('.', '_').replace(':', '_')
    if parsed.path and parsed.path != '/':
        path_part = parsed.path.strip('/').replace('/', '_')[:30]
        base = f"{base}_{path_part}"
    return base


def run_interactive_cli():
    """Prompt the user and build a CrawlerRunConfig."""
    print("\n" + "=" * 60)
    print("🕷️  WEB CRAWLER - Interactive Mode")
    print("=" * 60)

    url = get_user_input("\nEnter URL to crawl")
    if not url:
        print("Error: URL is required")
        sys.exit(1)
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    format_choice = get_choice(
        "Select Output Format:",
        ["JSON only", "CSV only", "DOCX only", "All formats (JSON + CSV + DOCX)"],
        default=4,
    )

    print("\n--- Optional Configuration (press Enter for defaults) ---")
    rc = CrawlerRunConfig()  # start from canonical defaults
    max_depth = int(get_user_input("Max Depth", str(rc.max_depth)) or rc.max_depth)
    max_pages = int(get_user_input("Max Pages", str(rc.max_pages)) or rc.max_pages)
    timeout   = int(get_user_input("Timeout per page (seconds)", str(rc.timeout_seconds)) or rc.timeout_seconds)

    base_name = _base_name_from_url(url)

    cfg = CrawlerRunConfig(
        max_depth=max_depth,
        max_pages=max_pages,
        timeout_seconds=timeout,
        mode="auto",
        output_json=f"{base_name}.json"  if format_choice in [1, 4] else None,
        output_csv=f"{base_name}.csv"   if format_choice in [2, 4] else None,
        output_docx=f"{base_name}.docx" if format_choice in [3, 4] else None,
    )

    cfg.log_summary(url)

    confirm = input("\nProceed with crawl? [Y/n]: ").strip().lower()
    if confirm and confirm != 'y':
        print("Crawl cancelled.")
        sys.exit(0)

    print("\n🚀 Starting crawl...\n")
    _run_crawl(url, cfg)


# ---------------------------------------------------------------------------
# Unified execution — both interactive & flag paths land here
# ---------------------------------------------------------------------------

def _run_crawl(url: str, cfg: CrawlerRunConfig):
    """Execute crawl — uses async engine by default, sync as fallback."""
    start_time = time.time()
    _run_async(url, cfg)
    elapsed = time.time() - start_time
    # elapsed already printed inside helpers via print_summary


def _run_async(url: str, cfg: CrawlerRunConfig):
    """Async crawl: high-performance worker pool with RAG pipeline."""
    from .async_crawler import AsyncDocCrawler
    import asyncio

    async_cfg = cfg.to_async_config(workers=getattr(cfg, '_workers', 6))
    crawler = AsyncDocCrawler(async_cfg)

    def progress_cb(pages_crawled, current_url, stats):
        clicks = stats.get('expandables_clicked', 0)
        print(f"[Page {pages_crawled}/{cfg.max_pages}] {current_url[:70]}...")
        if clicks:
            print(f"  Interactions: {clicks}")

    crawler.set_progress_callback(progress_cb)

    result = asyncio.run(crawler.crawl(url))

    if not result.pages:
        logger.warning("No pages were crawled — skipping export")
    else:
        logger.info(f"Crawl returned {len(result.pages)} pages — exporting...")
        try:
            _export_async(crawler, result, cfg)
        except Exception as exc:
            logger.error(f"Export failed: {exc}", exc_info=True)
    print_summary(result.stats, result.stats.get('elapsed_time', 0))


def _export_async(crawler, result, cfg: CrawlerRunConfig):
    """Export async crawl results to configured formats."""
    exported = []
    if cfg.output_json:
        crawler.export_json(result, cfg.output_json)
        exported.append(cfg.output_json)
    if cfg.output_csv:
        crawler.export_csv(result, cfg.output_csv)
        exported.append(cfg.output_csv)
    if cfg.output_docx:
        crawler.export_docx(result, cfg.output_docx)
        exported.append(cfg.output_docx)
    # RAG-specific exports
    if getattr(cfg, 'output_rag_json', None):
        crawler.export_rag_json(result, cfg.output_rag_json)
        exported.append(cfg.output_rag_json)
    if getattr(cfg, 'output_rag_jsonl', None):
        crawler.export_rag_jsonl(result, cfg.output_rag_jsonl)
        exported.append(cfg.output_rag_jsonl)
    if exported:
        print("\n" + "-" * 40)
        for path in exported:
            print(f"  ✅ Exported: {path}")
        print("-" * 40)
    else:
        logger.warning("No output format was configured — nothing exported")


def print_summary(stats: dict, elapsed: float):
    """Print crawl summary."""
    print("\n" + "=" * 65)
    print("📊 CRAWL COMPLETE")
    print("=" * 65)
    print(f"  Total pages crawled: {stats.get('pages_crawled', 0)}")
    if stats.get('pages_skipped', 0) > 0:
        print(f"  Pages skipped:       {stats.get('pages_skipped', 0)} (empty/cookie/loading)")
    print(f"  Failed pages:        {stats.get('pages_failed', 0)}")
    if stats.get('pages_retried', 0) > 0:
        print(f"  Pages retried:       {stats.get('pages_retried', 0)}")
    print(f"  Total time:          {stats.get('elapsed_time', elapsed):.1f}s")
    print(f"  Overall speed:       {stats.get('pages_per_sec_overall', stats.get('pages_per_second', 0)):.2f} pages/sec")
    if stats.get('pages_per_sec_rolling'):
        print(f"  Rolling speed (30s): {stats.get('pages_per_sec_rolling', 0):.2f} pages/sec")
    if stats.get('avg_page_ms'):
        print(f"  Avg page time:       {stats.get('avg_page_ms', 0):.0f} ms")
    if stats.get('p95_page_ms'):
        print(f"  P95 page time:       {stats.get('p95_page_ms', 0):.0f} ms")
    if stats.get('workers'):
        print(f"  Workers:             {stats.get('workers', 0)}")
    if stats.get('queue_peak'):
        print(f"  Queue peak:          {stats.get('queue_peak', 0)}")
    if stats.get('total_words'):
        print(f"  Total words:         {stats.get('total_words', 0):,}")
        print(f"  Avg words/page:      {stats.get('avg_words_per_page', 0):.0f}")
    if 'expandables_clicked' in stats:
        print(f"  Interactions:        {stats.get('expandables_clicked', 0)}")
    print(f"  Stop reason:         {stats.get('stop_reason', 'completed')}")
    print("=" * 65)


# ---------------------------------------------------------------------------
# Flag-driven entry point
# ---------------------------------------------------------------------------

def run_cli_with_args():
    """Parse argv, build CrawlerRunConfig, run."""
    parser = argparse.ArgumentParser(
        description='Web Crawler - Unified Playwright mode with auto JS/HTML detection',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m crawler                              # Interactive mode
  python -m crawler https://example.com          # Auto-detect mode with defaults
  python -m crawler https://example.com --depth 3 --pages 50 --output-json out.json
        """
    )

    parser.add_argument('url', nargs='?', help='URL to crawl (omit for interactive mode)')
    parser.add_argument('--depth', type=int, default=5, help='Maximum crawl depth (default: 5)')
    parser.add_argument('--pages', type=int, default=300, help='Maximum pages to crawl (default: 300)')
    parser.add_argument('--timeout', type=int, default=15, help='Timeout per page in seconds (default: 15)')
    parser.add_argument('--max-interactions', type=int, default=50, help='Max interactions per page (default: 50)')
    parser.add_argument('--rate', type=float, default=0.3, help='Delay between pages in seconds (default: 0.3)')
    parser.add_argument('--workers', type=int, default=6, help='Number of concurrent workers (default: 6)')
    parser.add_argument('--output-json', type=str, help='JSON output file path')
    parser.add_argument('--output-csv', type=str, help='CSV output file path')
    parser.add_argument('--output-docx', type=str, help='DOCX output file path')
    parser.add_argument('--output-rag-json', type=str, help='RAG corpus JSON output (chunked)')
    parser.add_argument('--output-rag-jsonl', type=str, help='RAG chunks JSONL output (one per line)')
    parser.add_argument(
        '--deny-pattern', type=str, action='append', default=[],
        help='Regex deny-pattern for URLs (repeatable)',
    )
    parser.add_argument(
        '--strip-query', action='store_true',
        help='Strip all query strings from discovered URLs',
    )
    parser.add_argument(
        '--sync', action='store_true',
        help='Use legacy sync crawler instead of async',
    )

    args = parser.parse_args()

    if not args.url:
        run_interactive_cli()
        return

    url = args.url
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    base_name = _base_name_from_url(url)

    # Build unified config from flags
    cfg = CrawlerRunConfig.from_cli_args(args)

    # Store workers count on config for async engine
    cfg._workers = getattr(args, 'workers', 6)

    # Resolve output paths
    has_explicit = args.output_json or args.output_csv or args.output_docx or getattr(args, 'output_rag_json', None) or getattr(args, 'output_rag_jsonl', None)
    if has_explicit:
        cfg.output_json = args.output_json
        cfg.output_csv  = args.output_csv
        cfg.output_docx = args.output_docx
        cfg.output_rag_json = getattr(args, 'output_rag_json', None)
        cfg.output_rag_jsonl = getattr(args, 'output_rag_jsonl', None)
    else:
        cfg.output_json = f"{base_name}.json"

    cfg.log_summary(url)

    # Use sync or async engine
    if getattr(args, 'sync', False):
        _run_sync(url, cfg)
    else:
        _run_crawl(url, cfg)


def _run_sync(url: str, cfg: CrawlerRunConfig):
    """Legacy sync crawl path."""
    from .deep_crawler import DeepDocCrawler
    deep_cfg = cfg.to_deep_config()
    crawler = DeepDocCrawler(deep_cfg)

    def progress_cb(pages_crawled, current_url, stats):
        print(f"[Page {pages_crawled}/{cfg.max_pages}] {current_url[:70]}...")

    crawler.set_progress_callback(progress_cb)
    start = time.time()
    result = crawler.crawl(url)
    elapsed = time.time() - start

    if result.pages:
        exported = []
        if cfg.output_json:
            crawler.export_json(result, cfg.output_json)
            exported.append(cfg.output_json)
        if cfg.output_csv:
            crawler.export_csv(result, cfg.output_csv)
            exported.append(cfg.output_csv)
        if cfg.output_docx:
            crawler.export_docx(result, cfg.output_docx)
            exported.append(cfg.output_docx)
        if exported:
            print("\n" + "-" * 40)
            for path in exported:
                print(f"  ✅ Exported: {path}")
            print("-" * 40)

    print_summary(result.stats, elapsed)


if __name__ == '__main__':
    run_cli_with_args()

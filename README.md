# 🕷️ CLI‑First Production Web Crawler (Internal)

Stable, scope‑aware crawler focused on CLI use and safe JS expansion. Designed for internal deployment on a single machine (Python + dependencies only). UI is optional; stability and limits are enforced by default.

## What’s included
 - Crawl entire domains or scoped subpaths (prefix filtering)
 - Static fetch with JS fallback (Playwright) for rendered pages
 - Safe expandable click logic (collapsibles/TOCs) with strict limits
 - Exports: JSON, CSV, DOCX
 - robots.txt awareness, rate limiting, retries
 - Structured logging and clear stop reasons

## Safety limits (enforced)
 - Per page: 20s timeout, wait_until=domcontentloaded (not networkidle), max 50 expandable clicks, no full DOM dumps
 - Crawl: MAX_PAGES default 150, MAX_DEPTH default 5, visited set maintained, graceful stop when limits hit
 - Rate limiting: configurable delay between pages

## Installation (local machine)
 ```bash
 cd web_crawler
 python -m venv venv
 source venv/bin/activate   # Windows: venv\Scripts\activate
 pip install -r requirements.txt
 playwright install chromium   # required for JS rendering
 ```

## CLI usage (standard crawler)
 Runs the breadth/depth crawler (static first, JS optional fallback).

 ```bash
 python -m crawler.crawler <url> \
    --depth 3 \
    --pages 100 \
    --output-json output.json \
    --output-csv output.csv \
    --rate 1.0 \
    --no-js        # add this flag to disable JS
 ```

Flags available today (from `crawler.py`):
 - `--depth`: max crawl depth (default 3)
 - `--pages`: max pages (default 100)
 - `--output-json`: JSON export path (default `output.json`)
 - `--output-csv`: CSV export path (default `output.csv`)
 - `--rate`: requests per second (default 1.0)
 - `--no-js`: disable Playwright rendering

> Note: A DOCX export helper exists in code for the deep crawler, but the CLI wiring for DOCX isn’t exposed yet. Use the Python API if you need DOCX today.

## Scope handling
 - `https://example.com` → crawl entire domain
 - `https://example.com/docs` → crawl only `/docs/**`
 Same-domain + path-prefix rules prevent out‑of‑scope hops.

## Safe JS expansion (deep crawler path)
 - Load page with `domcontentloaded`
 - Expand visible accordions/sidebar items once, max 50 clicks per page
 - Extract links after expansion, filter to in-scope URLs
 - Fallback to static HTML parse if Playwright fails (no crash)

## Outputs
 - JSON: structured page data + stats/errors
 - CSV: flattened rows for sheets
 - DOCX: per-page title, URL, headings, content (via `DeepDocCrawler.export_docx`)

## How to test manually (you run these)
 1) **Env prep** (once): create venv, install requirements, `playwright install chromium` (see Installation above).
 2) **Quick static crawl** (small, fast):
 ```bash
 python -m crawler.crawler https://example.com --depth 1 --pages 10 --output-json out.json --output-csv out.csv --no-js
 ```
 3) **JS-enabled crawl** (renders pages):
 ```bash
 python -m crawler.crawler https://developer.mozilla.org/en-US/docs --depth 2 --pages 30 --output-json mdn.json --output-csv mdn.csv
 ```
 4) **Scoped subpath crawl**:
 ```bash
 python -m crawler.crawler https://docs.python.org/3/library --depth 2 --pages 40 --output-json pydocs.json --output-csv pydocs.csv
 ```
 5) **Check outputs**: verify JSON/CSV files exist and contain pages; confirm stats (pages_crawled, errors) look reasonable.

Suggested test URLs (safe/public):
 - Static: `https://example.com`, `https://httpbin.org/html`
 - Doc subpath: `https://docs.python.org/3/library`
 - JS-heavy docs: `https://developer.mozilla.org/en-US/docs`

## Python API (for DOCX or custom flows)
 ```python
 from crawler.deep_crawler import DeepDocCrawler, DeepCrawlConfig

 config = DeepCrawlConfig(max_pages=50, max_depth=3)
 crawler = DeepDocCrawler(config)
 result = crawler.crawl("https://developer.mozilla.org/en-US/docs")

 # Export
 crawler.export_json(result, "mdn.json")
 crawler.export_csv(result, "mdn.csv")
 crawler.export_docx(result, "mdn.docx")
 ```

## Deployment (internal machine)
 - Keep it local: Python + dependencies only; no cloud services.
 - Use a service user with network egress to target docs sites.
 - Persist outputs to a shared folder (JSON/CSV/DOCX). No DB required.
 - For repeated runs, wrap the CLI call in a cron/systemd task with conservative limits.

## Troubleshooting
 - Playwright missing: `playwright install chromium`
 - JS hangs: add `--no-js` or lower `--depth/--pages`
 - Slow/blocked: reduce `--rate`, verify robots.txt, and ensure corporate proxy rules allow egress.

## Notes on limits
 - Per-page: 20s timeout, domcontentloaded wait, 50 click cap
 - Crawl: max_pages default 150, max_depth default 5
 - Stops cleanly with a reason (limit hit, queue exhausted, or user stop)

**Stability first. UI optional. Respect targets’ robots and terms.**
# 🕷️ Production Web Crawler

A production-grade, in-depth web crawler that can crawl an entire website and export scraped content to CSV and JSON formats. Built with Python, featuring a Streamlit frontend and support for JavaScript-rendered pages.

## ✨ Features

### Crawling Capabilities
- **BFS/DFS Traversal**: Choose between breadth-first or depth-first crawling strategies
- **Scope-Aware Crawling**: Automatically respects URL boundaries (root domain vs sub-path)
- **JavaScript Rendering**: Automatic fallback to Playwright headless browser for JS-heavy pages
- **Smart JS Detection**: Auto-detects when JavaScript rendering is needed
- **URL Normalization**: Handles fragments, trailing slashes, tracking parameters
- **Deduplication**: Prevents crawling the same page twice
- **Configurable Depth**: Control how deep the crawler follows links

### Content Extraction
- Page URL
- Page title (from `<title>`, `og:title`, or `<h1>`)
- Meta description
- All headings (H1-H6)
- Full visible text content (cleaned)
- Internal and external links
- Optional: Image extraction

### Politeness & Safety
- **robots.txt Compliance**: Respects crawling rules and crawl-delay directives
- **Rate Limiting**: Configurable requests per second (default: 1 req/sec)
- **Retry Logic**: Exponential backoff for failed requests
- **Custom User-Agent**: Identifiable bot user agent

### Export Formats
- **JSON**: Full structured data with metadata
- **CSV**: Flat format suitable for spreadsheet analysis

## 🏗️ Project Structure

```
web_crawler/
├── crawler/
│   ├── __init__.py       # Package initialization
│   ├── crawler.py        # Main crawling logic (BFS/DFS)
│   ├── scraper.py        # HTML parsing and content extraction
│   ├── robots.py         # robots.txt handling
│   └── utils.py          # URL normalization, rate limiting, utilities
├── app.py                # Streamlit web interface
├── requirements.txt      # Python dependencies
└── README.md            # This file
```

## 🚀 Quick Start

### Prerequisites
- Python 3.9 or higher
- pip (Python package manager)

### Installation

1. **Clone or navigate to the project directory**:
   ```bash
   cd web_crawler
   ```

2. **Create a virtual environment** (recommended):
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Install Playwright browsers** (for JavaScript rendering):
   ```bash
   playwright install chromium
   ```

### Running the Streamlit App

```bash
streamlit run app.py
```

The app will open in your browser at `http://localhost:8501`

### Command Line Usage

You can also use the crawler directly from the command line:

```bash
python -m crawler.crawler https://example.com --depth 3 --pages 100 --output-json results.json --output-csv results.csv
```

#### CLI Options:
| Option | Default | Description |
|--------|---------|-------------|
| `--depth` | 3 | Maximum crawl depth |
| `--pages` | 100 | Maximum pages to crawl |
| `--output-json` | output.json | JSON output file |
| `--output-csv` | output.csv | CSV output file |
| `--rate` | 1.0 | Requests per second |
| `--no-js` | False | Disable JavaScript rendering |

### Python API Usage

```python
from crawler import WebCrawler, CrawlConfig

# Configure the crawler
config = CrawlConfig(
    max_depth=3,
    max_pages=100,
    requests_per_second=1.0,
    respect_robots=True,
    enable_js_rendering=True
)

# Create and run crawler
with WebCrawler(config) as crawler:
    result = crawler.crawl("https://example.com")
    
    # Export results
    crawler.export_json(result, "output.json")
    crawler.export_csv(result, "output.csv")
    
    # Access data
    for page in result.pages:
        print(f"{page.url}: {page.title}")
```

## 🎯 Scope-Aware Crawling

The crawler automatically determines crawling boundaries based on the input URL.

### How It Works

| Input URL Type | Behavior |
|----------------|----------|
| **Root Domain** (`https://example.com`) | Crawls the **entire website** |
| **Sub-path URL** (`https://example.com/blog`) | Crawls **only** pages under `/blog/**` |

### Examples

#### Example 1: Root Domain Crawl
```
Input: https://www.oracle.com
```
**Result:** Crawls all pages on oracle.com
- ✅ `https://www.oracle.com/java`
- ✅ `https://www.oracle.com/cloud`
- ✅ `https://www.oracle.com/database`
- ❌ `https://docs.oracle.com` (different subdomain)

#### Example 2: Sub-path Crawl
```
Input: https://www.oracle.com/java/technologies
```
**Result:** Crawls ONLY pages under `/java/technologies/`
- ✅ `https://www.oracle.com/java/technologies`
- ✅ `https://www.oracle.com/java/technologies/javase`
- ✅ `https://www.oracle.com/java/technologies/downloads`
- ❌ `https://www.oracle.com/cloud` (outside scope)
- ❌ `https://www.oracle.com/java/overview` (outside scope)
- ❌ `https://www.oracle.com/index.html` (outside scope)

### URL Boundary Rules

1. **Domain Check**: URL must belong to the same domain
2. **Path Check**: If a sub-path is specified, the URL's path must start with that sub-path
3. **Exact Matching**: `/blog` matches `/blog` and `/blog/post` but NOT `/blogger`

### UI Feedback

The Streamlit interface shows the detected scope before crawling begins:
- 🌐 **Entire Domain** - When crawling from root
- 📂 **Sub-path Only** - When crawling from a specific path

## 🖥️ Streamlit Interface

The web interface provides:

1. **URL Input**: Enter the website URL to crawl
2. **Configuration Panel**:
   - Max crawl depth (1-10)
   - Max pages to crawl
   - Requests per second (rate limiting)
   - Crawl strategy (BFS/DFS)
   - robots.txt compliance toggle
   - JavaScript rendering toggle
3. **Progress Display**:
   - Real-time pages crawled count
   - Current URL being processed
   - Elapsed time
4. **Results View**:
   - Summary metrics
   - Sortable/filterable results table
   - Page detail viewer
5. **Download Buttons**:
   - JSON export
   - CSV export

## 📊 Output Format

### JSON Structure
```json
{
  "metadata": {
    "total_pages": 50,
    "crawl_stats": {
      "pages_crawled": 50,
      "pages_failed": 2,
      "elapsed_time": 120.5,
      "pages_per_second": 0.41
    },
    "config": {...},
    "exported_at": "2024-01-15T10:30:00"
  },
  "pages": [
    {
      "url": "https://example.com/page",
      "title": "Page Title",
      "meta_description": "Description text",
      "headings": {
        "h1": ["Main Heading"],
        "h2": ["Sub Heading 1", "Sub Heading 2"]
      },
      "text_content": "Full page text...",
      "internal_links": ["https://example.com/other"],
      "word_count": 500,
      "crawl_depth": 1,
      "status_code": 200
    }
  ],
  "errors": [
    {"url": "https://example.com/broken", "error": "404 Not Found"}
  ]
}
```

### CSV Columns
| Column | Description |
|--------|-------------|
| url | Page URL |
| title | Page title |
| meta_description | Meta description |
| h1, h2, h3... | Headings (pipe-separated) |
| text_content | Cleaned text (truncated to 10KB) |
| internal_links_count | Number of internal links |
| internal_links | First 50 links (pipe-separated) |
| word_count | Total words on page |
| crawl_depth | Depth from start URL |
| status_code | HTTP status code |

## ⚠️ Limitations

1. **Rate Limiting**: Default rate is 1 request/second. Increasing this may result in IP blocks.

2. **JavaScript Rendering**: 
   - Requires Playwright and Chromium browser
   - Slower than static crawling
   - May not work with all SPAs

3. **Authentication**: Does not handle:
   - Login-protected pages
   - CAPTCHA challenges
   - Cookie consent modals (may block content)

4. **Content Types**: Only processes HTML pages. Skips:
   - PDFs, images, videos
   - CSS, JavaScript files
   - Binary files

5. **Memory**: Large crawls (10,000+ pages) may require significant memory.

6. **Infinite Scroll**: Pages with infinite scroll may not capture all content.

## 🔒 Ethical Considerations

### Always:
- ✅ Respect `robots.txt` directives
- ✅ Use reasonable rate limiting (1-2 req/sec max)
- ✅ Identify your crawler with a descriptive User-Agent
- ✅ Check the website's Terms of Service
- ✅ Consider contacting the website owner for permission

### Never:
- ❌ Crawl sites that explicitly prohibit it
- ❌ Overwhelm servers with excessive requests
- ❌ Scrape personal or sensitive information
- ❌ Ignore rate limiting or crawl delays
- ❌ Use scraped data for unauthorized purposes

## 🔧 Configuration Reference

### CrawlConfig Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_depth` | int | 3 | Maximum link depth to follow |
| `max_pages` | int | 1000 | Maximum pages to crawl |
| `timeout` | int | 30 | Request timeout in seconds |
| `requests_per_second` | float | 1.0 | Rate limiting |
| `respect_robots` | bool | True | Follow robots.txt rules |
| `user_agent` | str | ProductionWebCrawler/1.0 | Bot identification |
| `max_retries` | int | 3 | Retry attempts for failures |
| `retry_delay` | float | 1.0 | Initial retry delay (seconds) |
| `enable_js_rendering` | bool | True | Use Playwright for JS pages |
| `auto_detect_js` | bool | True | Auto-detect JS requirements |
| `extract_images` | bool | False | Extract image information |
| `max_workers` | int | 1 | Concurrent workers |

## 🐛 Troubleshooting

### "Playwright browsers not installed"
```bash
playwright install chromium
```

### "Import could not be resolved"
Ensure you're running from the `web_crawler` directory and the virtual environment is activated.

### "Connection refused" or timeouts
- Check your internet connection
- The target website may be blocking your IP
- Try reducing the request rate

### "No pages crawled"
- Verify the URL is accessible
- Check if robots.txt is blocking the crawler
- Ensure the URL starts with `http://` or `https://`

## 📝 License

This project is provided for educational and legitimate web scraping purposes. Please use responsibly and in accordance with applicable laws and website terms of service.

## 🤝 Contributing

Contributions are welcome! Please ensure any changes:
1. Follow the existing code style
2. Include appropriate error handling
3. Respect the ethical guidelines above
4. Include tests for new functionality

---

**Built for production use** 🚀 **Crawl responsibly** 🕷️

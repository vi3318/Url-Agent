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

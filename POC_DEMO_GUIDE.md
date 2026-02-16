# POC Demo Guide — Web Crawler

## 1. POC Demo Steps (Quick Reference)

### Site Recommendation: `https://books.toscrape.com`

| Property | Value |
|----------|-------|
| URL | https://books.toscrape.com |
| Total pages | ~1,000 (50 catalogue + 1,000 book detail) |
| Auth required | No |
| Robots.txt blocking | None |
| JavaScript required | No (static HTML) |
| Why this site | Purpose-built for scraping demos, won't block or rate-limit |

---

### Step 1 — Generate the Sitemap

Use the free tool **https://www.xml-sitemaps.com**:
1. Go to https://www.xml-sitemaps.com
2. Enter `https://books.toscrape.com` in the URL field
3. Set max pages to **100** (keeps the POC manageable)
4. Click "Start" and wait for it to finish
5. Download the `sitemap.xml` file
6. Save it in the `web_crawler/` directory

### Step 2 — Crawl with the Tool

```bash
cd web_crawler
python3 -m crawler https://books.toscrape.com --depth 2 --pages 60 --no-js --output-json books_crawl.json --output-csv books_crawl.csv
```

**What each flag does:**
| Flag | Purpose |
|------|---------|
| `--depth 2` | Crawl 2 levels deep from the start URL |
| `--pages 60` | Stop after crawling 60 pages |
| `--no-js` | Use Standard Mode (fast static HTTP) |
| `--output-json books_crawl.json` | Save structured data to JSON |
| `--output-csv books_crawl.csv` | Save flat data to CSV |

### Step 3 — Compare Sitemap vs Crawl Output

```bash
python3 compare_sitemap.py sitemap.xml books_crawl.json
```

This will print a report showing:
- Matched URLs (in both sitemap and crawl output)
- Missed URLs (in sitemap but not crawled — typically beyond depth/page limits)
- Extra URLs (crawled but not in sitemap — dynamic or non-indexed pages)
- **Coverage percentage** — the key metric for the POC

---

## 2. Architecture & Code Explanation (For Presenting to the Team)

### 2.1 — What is Web Crawling?

Web crawling is the automated process of **systematically browsing the web** to discover, fetch, and extract content from pages. It's how Google indexes the internet — their bot (Googlebot) starts from known URLs, follows every link on each page, and repeats the process until it has visited billions of pages.

Our crawler does the same thing on a **targeted, controlled scale**:
1. Start from a seed URL (e.g., `https://books.toscrape.com`)
2. Download the HTML of that page
3. Parse the HTML to extract all links (`<a href="...">`)
4. Add those links to a queue
5. Pick the next URL from the queue, download it, extract links, repeat
6. Stop when we hit the configured depth or page limit

### 2.2 — High-Level Architecture

```
┌──────────────────────────────────────────────────────┐
│                       CLI Layer                       │
│                   (__main__.py)                        │
│  Parses args, prompts user, builds CrawlerRunConfig   │
└────────────────────┬─────────────────────────────────┘
                     │
           ┌─────────┴─────────┐
           ▼                   ▼
┌──────────────────┐  ┌──────────────────┐
│  Standard Mode   │  │    Deep Mode      │
│  (crawler.py)    │  │ (deep_crawler.py) │
│                  │  │                   │
│ HTTP requests +  │  │ Playwright +      │
│ BeautifulSoup    │  │ headless Chrome   │
└────────┬─────────┘  └────────┬──────────┘
         │                     │
         │    Shared Modules   │
         │  ┌──────────────┐   │
         ├──► scraper.py   ◄───┤   Content extraction (titles, headings, text, links)
         ├──► scope_filter ◄───┤   URL boundary enforcement
         ├──► utils.py     ◄───┤   URL normalization, rate limiting, retry logic
         ├──► robots.py    ◄───┤   robots.txt compliance
         └──► run_config   ◄───┘   Unified configuration
              interaction_policy   Interactive element expansion (Deep Mode only)
```

### 2.3 — Two Crawling Modes

#### Standard Mode (`crawler.py`)
- Uses the **`requests`** library to make HTTP GET requests
- Parses HTML with **BeautifulSoup** (lxml parser for speed)
- Fast: ~1–2 pages/second
- Works for **static sites** (content is in the HTML source)
- Falls back to Playwright if it detects the page needs JavaScript

**When to use:** Documentation sites, blogs, e-commerce sites, any site where content loads in the initial HTML.

#### Deep Mode (`deep_crawler.py`)
- Uses **Playwright** (headless Chromium browser) to render JavaScript
- Can **click buttons, expand accordions, toggle sidebars** to reveal hidden content
- Slower: ~0.3–0.5 pages/second (browser overhead)
- Handles **SPAs** (React, Angular, Vue) and **interactive doc sites** (Oracle, Confluence)

**When to use:** Sites that load content dynamically via JavaScript, sites with collapsible sidebar navigation, enterprise portals.

### 2.4 — Core Crawling Algorithm: BFS (Breadth-First Search)

```
              [Start URL]  (depth 0)
                  │
          ┌───────┼───────┐
          ▼       ▼       ▼
       [Page A] [Page B] [Page C]  (depth 1)
          │       │
       ┌──┼──┐   └──┐
       ▼  ▼  ▼      ▼
      [D][E][F]    [G]  (depth 2)
```

BFS crawls **level by level** — it visits ALL pages at depth 1 before moving to depth 2. This is the default strategy because:
- It finds the most important pages first (closer to the root = higher value)
- It respects the `--depth` limit naturally
- It gives a representative sample even if stopped early

**The code (simplified):**
```python
queue = deque([(start_url, depth=0)])     # FIFO queue
visited = set()                           # No duplicates
queued = set()                            # Frontier dedup

while queue and pages_crawled < max_pages:
    url, depth = queue.popleft()          # Take next URL
    if depth > max_depth:
        continue

    html = fetch(url)                     # Download the page
    page_data = scrape(html)              # Extract content
    pages.append(page_data)               # Store results

    for link in page_data.internal_links: # Follow links
        if link not in visited and link not in queued:
            if scope_filter.accept(link): # Must be in scope
                queue.append((link, depth + 1))
                queued.add(link)
```

### 2.5 — Scope Filtering (`scope_filter.py`)

**Problem:** If we start at `https://docs.example.com/api/v2/`, we don't want the crawler wandering off to `https://docs.example.com/blog/` or `https://example.com/pricing/`.

**Solution:** The `ScopeFilter` enforces a **subtree boundary**:

```
Root URL:  https://docs.example.com/api/v2/

✅ Accepted:
  https://docs.example.com/api/v2/endpoints    (inside subtree)
  https://docs.example.com/api/v2/auth/oauth   (inside subtree)

❌ Rejected:
  https://docs.example.com/api/v1/endpoints    (different version)
  https://docs.example.com/blog/               (outside subtree)
  https://other-site.com/anything              (different domain)
```

**How it works:**
1. **Canonicalize** both the root URL and candidate URL (lowercase host, remove fragments, normalize `.` segments, fix encoding)
2. **Check scheme + host** — must match exactly
3. **Check path prefix** — candidate's path must start with root's path
4. **Apply deny patterns** — regex-based blocklist (e.g., skip `/login`, `/admin`)
5. **Query filtering** — optionally strip query parameters to avoid infinite pagination

### 2.6 — Content Extraction (`scraper.py`)

The `PageScraper` takes raw HTML and produces structured `PageData`:

```
Raw HTML  →  BeautifulSoup Parse  →  PageData
                    │
                    ├─ Title:  <title> tag
                    ├─ Meta:   <meta name="description">
                    ├─ Headings: h1-h6 tags
                    ├─ Text:   Main content (nav/footer/ads stripped)
                    ├─ Links:  All <a href="..."> classified as internal/external
                    ├─ Images: <img src="..." alt="...">
                    └─ Word count, status code, content type
```

**Smart content extraction:**
- Strips `<script>`, `<style>`, `<nav>`, `<footer>`, `<aside>` tags to get clean text
- Identifies **main content areas** by looking for `<main>`, `<article>`, or elements with IDs/classes like `content`, `main-content`, `page-body`
- Classifies links as **internal** (same domain) or **external** (different domain)
- Resolves relative URLs (`./page.html` → `https://full.url/page.html`)

**Parser fallback:**
- Primary parser: `lxml` (C-based, fastest)
- If lxml produces 0 links but the page body has 200+ characters of text, it automatically retries with Python's built-in `html.parser` (more lenient)

### 2.7 — URL Normalization (`utils.py`)

URLs can look different but point to the same page. Without normalization, the crawler would visit the same page multiple times:

```
These are ALL the same page:
  https://example.com/docs/
  https://example.com/docs
  https://EXAMPLE.COM/docs/
  https://example.com/docs/?utm_source=google
  https://example.com/docs/#section1
  https://example.com/docs/index.html
```

The `URLNormalizer` handles:
- **Fragment removal** (`#section1` → removed)
- **Tracking parameter removal** (`utm_source`, `fbclid`, `gclid`, etc.)
- **Scheme/host lowercasing** (`HTTPS://EXAMPLE.COM` → `https://example.com`)
- **Default port stripping** (`:80` for http, `:443` for https)
- **Skip non-HTML resources** (`.pdf`, `.jpg`, `.zip`, `.css`, `.js`)
- **`ensure_joinable_base()`** — fixes a common bug where `urljoin()` resolves relative URLs to the wrong parent directory when trailing slashes are missing

### 2.8 — Safety & Politeness

| Feature | What it does |
|---------|-------------|
| **robots.txt compliance** | Checks `/robots.txt` before crawling; respects `Disallow` rules |
| **Rate limiting** | Configurable delay between requests (default: 1 req/sec) |
| **Crawl-delay** | Honors `Crawl-delay` directive from robots.txt |
| **Retry with backoff** | Failed requests retry up to 3 times with exponential delay |
| **Realistic User-Agent** | Identifies as Chrome 122 to avoid bot-detection blocks |
| **Page limit** | Hard stop at `--pages N` (prevents runaway crawls) |
| **Depth limit** | Hard stop at `--depth N` (prevents infinite descent) |
| **Timeout** | Per-page timeout (30s standard, 20s deep mode) |

### 2.9 — Deep Mode: Interactive Expansion (`interaction_policy.py`)

Many modern documentation sites hide content behind collapsible sidebars, accordions, and dropdowns. The deep crawler handles this:

```
Phase 0: Bulk Expand
  → Clicks "Expand All" / "Show All" buttons to open everything at once
  → Covers 18 CSS selector patterns

Phase 1: Targeted Expansion
  → Scans for 74 CSS selectors across 20+ JS frameworks:
    Bootstrap, ARIA, Oracle JET, Docusaurus, MkDocs, ReadTheDocs,
    Confluence, Ant Design, Material UI, Chakra UI, Notion,
    Salesforce Lightning, Zendesk, GitBook, Next.js, Gatsby, SAP Fiori
  → Clicks each collapsed element, waits, checks for new links

Phase 2: Text Heuristic
  → Finds elements containing text like "Show more", "View all", "Expand"
  → Clicks them as a fallback
```

**Safety limits per page:**
- Max 50 clicks per page
- 300ms delay after each click
- 20-second total timeout per page
- If element is already expanded (`aria-expanded="true"`), skip it

### 2.10 — Export Formats

| Format | Content | Use Case |
|--------|---------|----------|
| **JSON** | Full structured data: URLs, titles, headings, full text, all links, metadata | Analysis, search indexing, data processing |
| **CSV** | Flat rows: 1 row per page, truncated text, link counts | Spreadsheet review, quick auditing |
| **DOCX** | Formatted Word document with ToC, headers, body text per page | Business reports, sharing with non-technical stakeholders |

### 2.11 — Data Flow Summary

```
User runs CLI command
        │
        ▼
CrawlerRunConfig built (flags + defaults)
        │
        ▼
Start URL → Scope Filter initialized
        │
        ▼
BFS Queue seeded with start URL
        │
        ▼
┌─────────────────────────────────────┐
│  For each URL in queue:             │
│                                     │
│  1. Check: visited? robots? scope?  │
│  2. Fetch HTML (requests or         │
│     Playwright)                     │
│  3. Parse → extract content, links  │
│  4. Log: [SCRAPE] title, words,     │
│     headings, links                 │
│  5. For each link on the page:      │
│     a. Normalize URL                │
│     b. Check scope filter           │
│     c. If new & in scope → enqueue  │
│  6. Store PageData in results       │
│                                     │
│  Stop when: max_pages OR max_depth  │
│  OR queue empty                     │
└─────────────────────────────────────┘
        │
        ▼
Export to JSON / CSV / DOCX
```

---

## 3. File-by-File Code Map

| File | Lines | Purpose |
|------|------:|---------|
| `__main__.py` | 308 | CLI entry point — arg parsing, interactive prompts, export handling |
| `run_config.py` | 176 | Single config class with `from_cli_args()`, feeds both modes |
| `crawler.py` | 906 | Standard mode — HTTP + BeautifulSoup, BFS/DFS, JS fallback |
| `deep_crawler.py` | 1,473 | Deep mode — Playwright headless Chrome, full JS rendering |
| `scraper.py` | 613 | HTML → structured PageData (titles, headings, text, links) |
| `scope_filter.py` | 450 | URL boundary enforcement — subtree, deny patterns, query strip |
| `interaction_policy.py` | 701 | Click expansion engine — 74 selectors, 3-phase expand |
| `utils.py` | 730 | URL normalization, rate limiter, retry handler, helpers |
| `robots.py` | ~200 | robots.txt fetching, parsing, compliance checks |

---

## 4. Key Technical Decisions (Talking Points)

1. **BFS over DFS** — BFS finds high-value pages first; DFS can get trapped in deep hierarchies
2. **Dual-mode architecture** — Standard mode is 3–5× faster, Deep mode handles SPAs; auto-detection bridges them
3. **Scope filtering at enqueue time** — Prevents out-of-scope URLs from ever entering the queue (not just at visit time), saving time and bandwidth
4. **Frontier dedup (`_queued_urls` set)** — Without this, the same URL gets enqueued hundreds of times from different pages
5. **Parser fallback** — lxml is 10× faster than html.parser but can occasionally fail on malformed HTML; automatic fallback ensures we never lose data
6. **Realistic browser headers** — Many sites block requests with default Python User-Agent; we send full Chrome headers including `sec-ch-ua`
7. **`ensure_joinable_base()`** — Fixes a subtle `urljoin()` bug where `/docs/api` + `./endpoint` resolves to `/docs/endpoint` instead of `/docs/api/endpoint`

---

## 5. Quick Demo Script (Run in Terminal)

```bash
# Navigate to project
cd /path/to/web_crawler

# 1. Crawl the site
python3 -m crawler https://books.toscrape.com --depth 2 --pages 60 --no-js --output-json books_crawl.json --output-csv books_crawl.csv

# 2. Quick stats from the JSON
python3 -c "
import json
data = json.load(open('books_crawl.json'))
pages = data['pages']
print(f'Pages crawled: {len(pages)}')
print(f'Unique URLs: {len(set(p[\"url\"] for p in pages))}')
total_words = sum(p.get(\"word_count\", 0) for p in pages)
print(f'Total words extracted: {total_words:,}')
print(f'Avg words/page: {total_words // len(pages):,}')
print()
print('Sample pages:')
for p in pages[:5]:
    print(f'  {p[\"url\"]}')
    print(f'    Title: {p[\"title\"]}')
    print(f'    Words: {p[\"word_count\"]}')
"

# 3. Compare with sitemap (after downloading sitemap.xml)
python3 compare_sitemap.py sitemap.xml books_crawl.json
```

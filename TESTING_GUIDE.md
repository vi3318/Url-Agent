# 🧪 Web Crawler — Comprehensive Testing Guide

> **Audience:** QA / Manager / Developer evaluating the crawler  
> **Last updated:** June 2025

---

## Table of Contents

1. [Environment Setup](#1-environment-setup)
2. [Architecture Overview](#2-architecture-overview)
3. [CLI Quick Reference](#3-cli-quick-reference)
4. [Test Matrix](#4-test-matrix)
5. [Standard Mode Test Cases](#5-standard-mode-test-cases)
6. [Deep Mode Test Cases](#6-deep-mode-test-cases)
7. [Enterprise / JS-Heavy Site Tests](#7-enterprise--js-heavy-site-tests)
8. [Output Verification](#8-output-verification)
9. [Unit Tests](#9-unit-tests)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Environment Setup

### Prerequisites

| Requirement | Version | Purpose |
|---|---|---|
| Python | 3.11+ | Runtime |
| pip | latest | Package manager |
| Git | any | Source control |

### Installation (one-time)

```bash
# Clone and navigate
cd web_crawler

# Create virtual environment
python -m venv venv
source venv/bin/activate        # macOS / Linux
# venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser (required for Deep Mode and JS fallback)
playwright install chromium

# Install test runner
pip install pytest
```

### Verify Installation

```bash
# Quick sanity check — should print help text
python -m crawler --help

# Verify Playwright is working
python -c "from playwright.sync_api import sync_playwright; print('Playwright OK')"
```

---

## 2. Architecture Overview

```
┌──────────────────────────────────────────────────┐
│                 python -m crawler                │
│               (__main__.py — CLI)                 │
├────────────────────┬─────────────────────────────┤
│   Standard Mode    │         Deep Mode            │
│   (crawler.py)     │    (deep_crawler.py)          │
│                    │                               │
│  requests + BS4    │  Playwright (Chromium)         │
│  Optional JS       │  + interaction_policy.py       │
│  fallback          │  (expand TOCs, accordions)     │
├────────────────────┴─────────────────────────────┤
│                  Shared Layer                     │
│  scope_filter.py — strict subtree enforcement     │
│  scraper.py       — HTML parsing / link extraction│
│  utils.py         — URL normalization             │
│  robots.py        — robots.txt compliance         │
│  run_config.py    — unified configuration         │
└──────────────────────────────────────────────────┘
```

### Standard Mode
- Uses `requests` for HTTP + BeautifulSoup for parsing
- Optional Playwright fallback for JS-heavy pages
- Fastest; best for static documentation sites

### Deep Mode
- Full Playwright-driven rendering for every page
- **Phase 0 bulk-expand:** Clicks "Expand All" buttons (e.g., Oracle sidebar)
- **Phase 1 / 2 granular expansion:** Clicks individual accordions, collapsibles, tree nodes
- Supports 20+ JS frameworks (Docusaurus, MkDocs, Confluence, React, SAP Fiori, etc.)
- Skips redundant expansion when the BFS queue is already full

---

## 3. CLI Quick Reference

### Entry Point

```bash
python -m crawler [URL] [OPTIONS]
```

If `URL` is omitted, the interactive menu launches.

### Flags

| Flag | Default | Description |
|---|---|---|
| `URL` | *(interactive)* | Starting URL to crawl |
| `--deep` | off | Enable Deep Mode (Playwright + interactions) |
| `--depth N` | `5` | Maximum link-following depth |
| `--pages N` | `150` | Maximum pages to crawl |
| `--timeout N` | `20` | Timeout per page in seconds |
| `--max-interactions N` | `50` | Max expandable clicks per page |
| `--rate N` | `1.0` | Delay between pages (seconds) |
| `--output-json FILE` | auto | JSON output path |
| `--output-csv FILE` | *(none)* | CSV output path |
| `--output-docx FILE` | *(none)* | DOCX output path |
| `--no-js` | off | Disable JS rendering (Standard Mode only) |
| `--deny-pattern REGEX` | *(none)* | URL deny pattern (repeatable) |
| `--strip-query` | off | Strip all query strings from URLs |

### Output Defaults
- If **no** `--output-*` flags are given → JSON is auto-generated as `<domain>_<path>.json`
- If **any** `--output-*` flag is given → only the specified formats are produced

---

## 4. Test Matrix

### Quick Reference: Which test proves what

| Capability | Test Case | Mode | Expected |
|---|---|---|---|
| Basic crawl works | TC-1 | Standard | 1 page, JSON exists |
| Scope enforcement | TC-2 | Standard | Only `/3/library/` pages |
| JS rendering | TC-3 | Standard | Pages with JS content |
| Deep expansion | TC-5 | Deep | Sidebar links discovered |
| Bulk-expand (Phase 0) | TC-7 | Deep | "Expand All" clicked once |
| Enterprise JS frameworks | TC-7–TC-10 | Deep | Pages crawled, content scraped |
| Export all formats | TC-4 | Both | JSON + CSV + DOCX files |
| Deny patterns | TC-11 | Both | Matching URLs excluded |
| Per-page logging | All | Both | `[SCRAPE]` and `[PAGE OK]` lines |
| Rate limiting | TC-1 | Both | ~1s gap between page fetches |
| robots.txt | TC-2 | Standard | Disallowed paths skipped |

---

## 5. Standard Mode Test Cases

### TC-1: Minimal Static Crawl

**Purpose:** Verify basic crawl + JSON export works end-to-end.

```bash
python -m crawler https://example.com --depth 1 --pages 5 --no-js
```

**Expected:**
- Terminal shows `[SCRAPE]` line with title, word count, headings, links
- `[Page 1/5]` progress line printed
- `✅ Exported: example_com.json` appears at the end
- `example_com.json` file exists with `pages` array containing 1+ entries
- Each page entry has: `url`, `title`, `text_content`, `word_count`
- `stats.stop_reason` is `"Queue exhausted"` or `"MAX_PAGES limit reached"`

---

### TC-2: Scoped Subtree Crawl (Python Docs)

**Purpose:** Verify strict scope enforcement — only pages under `/3/library/` are crawled.

```bash
python -m crawler https://docs.python.org/3/library/ --depth 2 --pages 20 --no-js
```

**Expected:**
- All crawled URLs start with `https://docs.python.org/3/library/`
- No URLs like `https://docs.python.org/3/tutorial/` appear in output
- `[SCRAPE]` log shows content for each page (word count > 0)
- JSON file contains 20 pages (or fewer if queue exhausted)
- Open the JSON and verify every `url` field is within `/3/library/`

---

### TC-3: JS-Enabled Crawl (MDN)

**Purpose:** Verify Playwright JS fallback renders dynamic content.

```bash
python -m crawler https://developer.mozilla.org/en-US/docs/Web/JavaScript --depth 2 --pages 15
```

**Expected:**
- Pages render fully (titles are not empty)
- `[SCRAPE]` logs show reasonable word counts (100+ words per page)
- No `Playwright not installed` errors
- JSON export contains structured content

---

### TC-4: All Export Formats

**Purpose:** Verify JSON, CSV, and DOCX all export correctly.

```bash
python -m crawler https://example.com --depth 1 --pages 3 \
  --output-json test_out.json \
  --output-csv test_out.csv \
  --output-docx test_out.docx
```

**Expected:**
- Three `✅ Exported:` lines appear
- `test_out.json` — valid JSON with `pages` array
- `test_out.csv` — opens in Excel/Numbers with column headers
- `test_out.docx` — opens in Word with page titles and content
- All three files have the same number of pages

---

## 6. Deep Mode Test Cases

### TC-5: Deep Mode — React Docs

**Purpose:** Verify Deep Mode renders React's client-side site and extracts content.

```bash
python -m crawler https://react.dev/learn/ --deep --pages 20 --depth 3
```

**Expected:**
- `[Page 1/20]` through `[Page 20/20]` progress lines (or fewer if queue exhausts)
- `[SCRAPE]` log for each page with `text=X chars` (X > 100)
- `[PAGE OK]` log with word count and link count
- JSON auto-exported as `react_dev_learn.json`
- Open JSON: each page has `title`, `text_content` (not empty), `internal_links`

---

### TC-6: Deep Mode — Docusaurus Site

**Purpose:** Verify Docusaurus sidebar selectors work.

```bash
python -m crawler https://docusaurus.io/docs --deep --pages 15 --depth 3
```

**Expected:**
- Sidebar links discovered (check `[FRONTIER]` log: `discovered=` > 5)
- Pages crawled under `/docs/` scope only
- Content extracted with headings and code blocks

---

## 7. Enterprise / JS-Heavy Site Tests

### TC-7: Oracle Documentation (Oracle JET + oj-treeview)

**Purpose:** Verify Phase 0 bulk-expand on Oracle's "Expand All" sidebar button.

```bash
python -m crawler \
  https://docs.oracle.com/en/cloud/saas/human-resources/oedmh/ \
  --deep --pages 30 --depth 3 --timeout 30
```

**Expected:**
- First page takes ~60-70s (bulk-expand + link extraction from 11,000+ TOC items)
- Log shows `[PHASE-0] Bulk expand successful` or similar
- Pages 2-30 take ~8-10s each (expansion skipped)
- `[FRONTIER]` on page 1 shows `discovered=` in the thousands
- All crawled URLs stay within `/en/cloud/saas/human-resources/oedmh/`
- `stats.stop_reason` → `"MAX_PAGES limit reached (30)"`

---

### TC-8: ServiceNow Documentation (Service-Based Enterprise)

**Purpose:** Verify crawling of a service-based enterprise documentation site.

```bash
python -m crawler \
  https://docs.servicenow.com/bundle/xanadu-it-service-management/page/product/incident-management/concept/c_IncidentManagement.html \
  --deep --pages 20 --depth 3 --timeout 25
```

**Expected:**
- Pages crawled within the ServiceNow docs subtree
- Content extracted (ServiceNow uses dynamic JS rendering)
- `[SCRAPE]` shows reasonable word counts for each page
- JSON export contains structured incident management documentation

---

### TC-9: SAP Help Portal (SAP Fiori / UI5)

**Purpose:** Verify SAP Fiori/UI5 selectors and tree-based navigation.

```bash
python -m crawler \
  https://help.sap.com/docs/SAP_S4HANA_ON-PREMISE/latest \
  --deep --pages 20 --depth 3 --timeout 25
```

**Expected:**
- SAP Help uses collapsible tree navigation
- Interaction policy should expand tree nodes
- Content pages scraped with headings and text
- All URLs stay within SAP Help scope

---

### TC-10: Atlassian/Confluence Documentation

**Purpose:** Verify Confluence expand selectors and page tree.

```bash
python -m crawler \
  https://confluence.atlassian.com/doc/confluence-documentation-135922.html \
  --deep --pages 20 --depth 3 --timeout 25
```

**Expected:**
- Confluence page tree links discovered
- `expand-content` sections expanded if present
- Documentation pages crawled with structured content
- URLs stay within the Confluence docs subtree

---

### TC-11: Deny Patterns

**Purpose:** Verify URL deny patterns exclude matching pages.

```bash
python -m crawler https://docs.python.org/3/library/ \
  --depth 2 --pages 30 \
  --deny-pattern "/__pycache__" \
  --deny-pattern "/test" \
  --no-js
```

**Expected:**
- No URLs containing `/__pycache__` or `/test` in the JSON output
- Denied URLs logged (at debug level)
- Other pages crawled normally

---

## 8. Output Verification

### JSON Structure (Deep Mode)

```json
{
  "stats": {
    "pages_crawled": 20,
    "pages_failed": 0,
    "expandables_clicked": 15,
    "links_discovered": 250,
    "elapsed_time": 180.5,
    "pages_per_second": 0.11,
    "stop_reason": "MAX_PAGES limit reached (20)",
    "scope": "https://react.dev/learn"
  },
  "hierarchy": { ... },
  "pages": [
    {
      "url": "https://react.dev/learn",
      "title": "Quick Start",
      "breadcrumb": [],
      "section_path": [],
      "headings": { "h1": ["Quick Start"], "h2": ["..."] },
      "text_content": "Welcome to the React documentation...",
      "tables": [],
      "code_blocks": ["const element = ..."],
      "internal_links": ["https://react.dev/learn/thinking-in-react", ...],
      "parent_url": "",
      "depth": 0,
      "word_count": 1250
    }
  ],
  "errors": []
}
```

### JSON Structure (Standard Mode)

```json
{
  "metadata": { ... },
  "stats": {
    "pages_crawled": 10,
    "pages_failed": 0,
    "stop_reason": "Queue exhausted"
  },
  "pages": [
    {
      "url": "https://example.com",
      "title": "Example Domain",
      "text_content": "...",
      "word_count": 50,
      "internal_links": [...],
      "headings": { "h1": ["Example Domain"] }
    }
  ]
}
```

### What to Check in Any Output

| Check | Where | Pass Criteria |
|---|---|---|
| Pages crawled | `stats.pages_crawled` | > 0, ≤ `--pages` value |
| No failures | `stats.pages_failed` | 0 (or very low) |
| Stop reason | `stats.stop_reason` | `"MAX_PAGES"` or `"Queue exhausted"` |
| Scope correct | Every `pages[*].url` | Starts with the root URL path |
| Content exists | `pages[*].text_content` | Non-empty strings |
| Word count | `pages[*].word_count` | > 0 for each page |
| Title present | `pages[*].title` | Non-empty for most pages |
| File on disk | Terminal output | `✅ Exported: <filename>` printed |

---

## 9. Unit Tests

### Running Tests

```bash
cd web_crawler
python -m pytest tests/ -v
```

### Current Test Suite

| Test Class | Count | What it Tests |
|---|---|---|
| `TestCanonicalise` | 15 | URL normalization (encoding, ports, fragments, www) |
| `TestStrictBoundary` | 6 | Scope enforcement (sibling paths, exact match, cross-host) |
| `TestSchemePolicy` | 5 | HTTP ↔ HTTPS handling |
| `TestScopePathFromRoot` | 5 | Scope path derivation from file vs directory URLs |
| `TestScopeFilterBehavior` | 10 | Deny patterns, query stripping, score_url, descriptions |
| `TestDecodeUnreserved` | 4 | Percent-encoding normalization |
| `TestConfigHardening` | 6* | Config field propagation (*skipped if deps unavailable) |
| **Total** | **52** | |

All tests should pass with 0 failures. Some may skip if optional dependencies aren't installed.

---

## 10. Troubleshooting

### Common Issues

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: playwright` | Playwright not installed | `pip install playwright && playwright install chromium` |
| `No pages were crawled` | URL unreachable or scope too narrow | Check URL is accessible; try without `--deep` first |
| `No output format was configured` | Bug in format resolution | Pass `--output-json out.json` explicitly |
| `HTTP 403 / 429` | Site blocking the bot | Lower `--rate` to `2.0` or `3.0`; some sites block bots entirely |
| Deep mode very slow | Re-expanding sidebar on every page | Already fixed — queue-skip logic prevents this; verify pages 2+ take < 15s |
| `lxml not installed` | Missing parser | `pip install lxml` (falls back to `html.parser` automatically) |
| Crawl stops at 1 page | Scope too restrictive or no in-scope links found | Check `[FRONTIER]` log — if `discovered=0`, the page has no in-scope links |
| DOCX export fails | `python-docx` not installed | `pip install python-docx` |

### Reading the Logs

The crawler emits structured log lines at INFO level. Key log prefixes:

| Log Prefix | Meaning |
|---|---|
| `[1/20] Depth:0` | BFS progress: page N of max, at depth D |
| `[SCRAPE]` | Per-page content extraction summary (title, chars, headings, tables, code) |
| `[PAGE OK]` | Page successfully scraped (word count, link count, depth) |
| `[FRONTIER]` | Link discovery stats (discovered, enqueued, already visited, scope rejected) |
| `[LINKS]` | Detailed link extraction (total href, kept, rejected by type) |
| `[PHASE-0]` | Bulk-expand attempt (e.g., "Expand All" button) |
| `[SCOPE]` | URL accepted or rejected by scope filter |
| `STOPPING:` | Crawl termination with reason |
| `✅ Exported:` | Output file written successfully |

### Performance Expectations

| Site Type | Mode | Pages | Expected Time |
|---|---|---|---|
| Static (example.com) | Standard | 5 | < 10s |
| Python docs | Standard | 20 | 30-60s |
| React docs | Deep | 20 | 2-5 min |
| Oracle docs (11K TOC) | Deep | 30 | 5-6 min (page 1 slow, rest fast) |
| Enterprise (ServiceNow, SAP) | Deep | 20 | 3-8 min |

---

## Appendix: Quick Smoke Test (Copy & Paste)

Run these three commands in sequence to verify the crawler is working:

```bash
# 1. Standard mode — static site (should finish in <10 seconds)
python -m crawler https://example.com --depth 1 --pages 3 --no-js --output-json smoke_standard.json

# 2. Standard mode — scoped crawl (should finish in <60 seconds)
python -m crawler https://docs.python.org/3/library/ --depth 1 --pages 10 --no-js --output-json smoke_scoped.json

# 3. Deep mode — JS-rendered site (should finish in <3 minutes)
python -m crawler https://react.dev/learn/ --deep --depth 2 --pages 10 --output-json smoke_deep.json
```

**Pass criteria:** All three commands produce JSON files with `pages_crawled > 0` and `pages_failed == 0`.

```bash
# Verify outputs exist and have content
for f in smoke_standard.json smoke_scoped.json smoke_deep.json; do
  echo "--- $f ---"
  python -c "import json; d=json.load(open('$f')); print(f'  Pages: {d[\"stats\"][\"pages_crawled\"]}, Failed: {d[\"stats\"][\"pages_failed\"]}, Reason: {d[\"stats\"][\"stop_reason\"]}')"
done
```

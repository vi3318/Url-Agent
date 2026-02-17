# Async Crawler Architecture — v2.0

## Overview

The web crawler has been redesigned from a **sequential sync Playwright** engine 
to a **high-performance async architecture** with worker pool, RAG pipeline 
integration, and structured Word export.

**Performance target:** 300 pages in <10 minutes (was ~50 minutes).  
**Scale target:** 5,000+ pages with bounded memory.

---

## Architecture Diagram

```
                    ┌─────────────────────────────────┐
                    │          CLI / Streamlit         │
                    │   (__main__.py / app.py)         │
                    └──────────┬──────────────────────┘
                               │
                    ┌──────────▼──────────────────────┐
                    │       CrawlerRunConfig           │
                    │   (run_config.py — single        │
                    │    source of truth for all       │
                    │    defaults and limits)           │
                    └──────────┬──────────────────────┘
                               │ .to_async_config()
                    ┌──────────▼──────────────────────┐
                    │     AsyncDocCrawler              │
                    │   (async_crawler.py)             │
                    │                                  │
                    │  ┌─────────────────────────────┐ │
                    │  │   asyncio.Queue (BFS)       │ │
                    │  │   ┌──────────────────────┐  │ │
                    │  │   │ (url, depth, parent,  │  │ │
                    │  │   │  section_path)        │  │ │
                    │  │   └──────────────────────┘  │ │
                    │  └────────────┬────────────────┘ │
                    │               │                  │
                    │  ┌────────────▼────────────────┐ │
                    │  │  Worker Pool (Semaphore)    │ │
                    │  │  ┌────┐┌────┐┌────┐┌────┐  │ │
                    │  │  │ W1 ││ W2 ││ W3 ││ W4 │  │ │
                    │  │  └──┬─┘└──┬─┘└──┬─┘└──┬─┘  │ │
                    │  │     │     │     │     │     │ │
                    │  │     └─────┴─────┴─────┘     │ │
                    │  │            │                 │ │
                    │  │    ┌───────▼───────┐        │ │
                    │  │    │  Playwright   │        │ │
                    │  │    │  (1 browser,  │        │ │
                    │  │    │  1 context,   │        │ │
                    │  │    │  N pages)     │        │ │
                    │  │    └───────────────┘        │ │
                    │  └─────────────────────────────┘ │
                    └──────────┬──────────────────────┘
                               │ pages[]
              ┌────────────────▼────────────────────┐
              │       RAG Pipeline                  │
              │   (pipeline.py)                     │
              │                                     │
              │  Clean → Section → Chunk → Enrich   │
              │                                     │
              │  Output: RAGCorpus                  │
              │    └─ RAGDocument[]                  │
              │         └─ RAGChunk[]               │
              └────────────────┬────────────────────┘
                               │
              ┌────────────────▼────────────────────┐
              │         Exporters                   │
              │                                     │
              │  • JSON (legacy page-based)         │
              │  • RAG JSON (corpus + chunks)       │
              │  • JSONL (one chunk per line)        │
              │  • CSV                               │
              │  • DOCX (structured Word + TOC)     │
              └─────────────────────────────────────┘
```

---

## New Modules

| Module | Purpose | Lines |
|--------|---------|-------|
| `async_crawler.py` | Async Playwright engine with worker pool | ~800 |
| `rag_model.py` | RAG data model (RAGChunk, RAGDocument, RAGCorpus) | ~230 |
| `pipeline.py` | Transformation pipeline (clean → section → chunk → enrich) | ~300 |
| `word_exporter.py` | Structured Word export with TOC, tables, headings | ~280 |
| `monitor.py` | Real-time performance metrics (pages/sec, P95, queue) | ~270 |

## Modified Modules

| Module | Changes |
|--------|---------|
| `__main__.py` | Async engine as default, `--workers`, `--sync`, RAG export flags |
| `__init__.py` | Exports for all new modules |
| `run_config.py` | `to_async_config()` factory method, `_workers` field |
| `requirements.txt` | Added `aiohttp>=3.9.0` (optional) |

## Preserved Modules (unchanged)

| Module | Purpose |
|--------|---------|
| `deep_crawler.py` | Legacy sync engine (still works with `--sync` flag) |
| `scope_filter.py` | URL scope enforcement (used by both engines) |
| `interaction_policy.py` | Interactive element expansion (called from async via executor) |
| `utils.py` | URL normalization |
| `scraper.py` | PageScraper (legacy) |
| `robots.py` | robots.txt handling |

---

## Performance Design

### Concurrency Model
```
Browser (1 instance)
  └─ BrowserContext (1 shared session — cookies/auth)
       ├─ Page 1  ←  Worker 1
       ├─ Page 2  ←  Worker 2
       ├─ Page 3  ←  Worker 3
       ├─ Page 4  ←  Worker 4
       ├─ Page 5  ←  Worker 5
       └─ Page 6  ←  Worker 6
```

- **Single browser** — launched once, reused for entire crawl
- **Single context** — shared cookies, session state, auth
- **N pages** — each worker gets its own page (tab), max N concurrent
- **asyncio.Semaphore(6)** — prevents resource exhaustion

### Resource Blocking
Blocked at the route level (never downloaded):
- Images, fonts, media, stylesheets
- Analytics: Google Analytics, Tag Manager, Facebook, Hotjar, etc.

### Wait Strategy
```
wait_until='domcontentloaded'     # fast — don't wait for all resources
networkidle timeout=3000          # best-effort, 3s cap
```
No `wait_for_timeout()` calls in critical path.

### Expected Performance
| Metric | Sync (old) | Async (new) |
|--------|-----------|-------------|
| Pages/sec | 0.1 | 0.5–2.0 |
| 300 pages | ~50 min | ~3–10 min |
| Memory | Linear | Bounded (queue maxsize) |
| Workers | 1 | 6 (configurable) |

---

## RAG Data Model

### Chunk Schema
```json
{
  "doc_id": "a3f8c2...",          // SHA-256 prefix of URL
  "chunk_id": "a3f8c2..._0001",  // doc_id + index
  "chunk_index": 1,
  "source_url": "https://...",
  "parent_url": "https://...",
  "crawl_timestamp": "2024-...",
  "domain": "docs.example.com",
  "page_title": "Installation Guide",
  "section_title": "Prerequisites",
  "heading_path": ["H1: Guide", "H2: Installation", "H3: Prerequisites"],
  "breadcrumb": ["Docs", "Guide", "Install"],
  "depth": 2,
  "chunk_type": "text",           // text | table | code
  "content": "Before installing...",
  "word_count": 342,
  "char_count": 1856,
  "token_estimate": 445,
  "metadata": {}
}
```

### Chunking Strategy
- **Target:** ~400 words (~520 tokens) per chunk
- **Max:** 600 words hard ceiling
- **Overlap:** 40 words between sequential chunks
- **Tables:** separate chunks (preserved structure)
- **Code blocks:** separate chunks (preserved formatting)
- **Section splits:** by heading boundaries (H1–H6)

---

## CLI Usage

```bash
# Async crawl (default) — 6 workers, 300 pages
python -m crawler https://docs.example.com

# Custom workers and limits
python -m crawler https://docs.example.com --workers 8 --pages 500

# RAG-specific exports
python -m crawler https://docs.example.com \
  --output-rag-json corpus.json \
  --output-rag-jsonl chunks.jsonl \
  --output-docx report.docx

# Legacy sync mode
python -m crawler https://docs.example.com --sync

# All formats
python -m crawler https://docs.example.com \
  --output-json pages.json \
  --output-csv pages.csv \
  --output-docx report.docx \
  --output-rag-json rag_corpus.json \
  --output-rag-jsonl rag_chunks.jsonl
```

---

## Migration Notes

- **Backward compatible:** `--sync` flag uses the original `DeepDocCrawler`
- **Default changed:** async engine is now the default (was sync)
- **Config compatible:** `CrawlerRunConfig` still works, plus `to_async_config()`
- **Export compatible:** `export_json`, `export_csv`, `export_docx` produce same format
- **New exports:** `export_rag_json` (hierarchical), `export_rag_jsonl` (flat chunks)

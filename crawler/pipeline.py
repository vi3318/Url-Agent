"""
RAG Transformation Pipeline
=============================
Converts raw crawled page data into chunked, metadata-enriched RAG documents.

Pipeline stages:
1. **Clean**: Strip boilerplate, normalize whitespace, remove nav/footer noise
2. **Section**: Split content by headings into logical sections
3. **Chunk**: Split large sections into token-budget-safe chunks
4. **Enrich**: Attach heading path, section title, metadata to each chunk
5. **Emit**: Produce RAGDocument with fully populated RAGChunk list

Design decisions:
- Chunk size targets ~500 tokens (configurable) for optimal retrieval
- Overlap of ~50 tokens between sequential chunks for context continuity
- Tables become standalone chunks (preserving structure)
- Code blocks become standalone chunks (preserving formatting)
- Heading path is inherited downward until overridden
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .rag_model import ChunkType, RAGChunk, RAGDocument

logger = logging.getLogger(__name__)

# Approximate tokens per word (conservative for English text)
_TOKENS_PER_WORD = 1.3


@dataclass
class PipelineConfig:
    """Configuration for the transformation pipeline."""
    # Chunking
    target_chunk_words: int = 400      # ~520 tokens at 1.3x
    max_chunk_words: int = 600         # hard ceiling
    overlap_words: int = 40            # context overlap between sequential chunks
    min_chunk_words: int = 20          # discard tiny fragments

    # Cleaning
    strip_boilerplate: bool = True     # remove repeated nav/footer text
    normalize_whitespace: bool = True
    remove_urls_from_text: bool = False # strip inline URLs
    max_heading_depth: int = 6         # track H1-H6

    # Table / code handling
    table_as_separate_chunk: bool = True
    code_as_separate_chunk: bool = True
    max_table_rows_per_chunk: int = 50


# ---------------------------------------------------------------------------
# Stage 1: Clean
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\n{3,}")
_SPACES_RE = re.compile(r" {2,}")
_URL_RE = re.compile(r"https?://\S+")


def _clean_text(text: str, config: PipelineConfig) -> str:
    """Normalize whitespace, strip noise."""
    if not text:
        return ""

    text = text.strip()

    if config.normalize_whitespace:
        text = _WHITESPACE_RE.sub("\n\n", text)
        text = _SPACES_RE.sub(" ", text)

    if config.remove_urls_from_text:
        text = _URL_RE.sub("", text)

    return text.strip()


# ---------------------------------------------------------------------------
# Stage 2: Section by headings
# ---------------------------------------------------------------------------

# Matches lines that look like headings (from inner_text extraction)
# e.g. lines that are short, titlecased, and followed by content
_HEADING_PATTERN = re.compile(
    r"^(#{1,6})\s+(.+)$", re.MULTILINE
)


@dataclass
class _Section:
    """Internal representation of a document section."""
    heading: str = ""
    heading_level: int = 0  # 0 = no heading (preamble)
    content: str = ""
    tables: List[Dict] = field(default_factory=list)
    code_blocks: List[str] = field(default_factory=list)


def _split_by_headings(
    text: str,
    headings_dict: Dict[str, List[str]],
) -> List[_Section]:
    """Split text into sections based on known headings.

    We use the heading strings extracted by the crawler (H1-H6) to find
    section boundaries in the flat text.  This is more reliable than
    regex-based heading detection on inner_text output.
    """
    # Build ordered list of (heading_text, level) from the headings dict
    heading_markers: List[Tuple[str, int]] = []
    for level in range(1, 7):
        key = f"h{level}"
        for h_text in headings_dict.get(key, []):
            h_clean = h_text.strip()
            if h_clean:
                heading_markers.append((h_clean, level))

    if not heading_markers or not text:
        return [_Section(content=text)]

    # Find positions of each heading in the text
    positions: List[Tuple[int, int, str, int]] = []  # (start, end, text, level)
    for h_text, level in heading_markers:
        # Escape special regex chars in heading text
        pattern = re.escape(h_text)
        for m in re.finditer(pattern, text):
            positions.append((m.start(), m.end(), h_text, level))

    if not positions:
        return [_Section(content=text)]

    # Sort by position and deduplicate overlapping matches
    positions.sort(key=lambda x: x[0])
    deduped = []
    last_end = -1
    for start, end, h_text, level in positions:
        if start >= last_end:
            deduped.append((start, end, h_text, level))
            last_end = end
    positions = deduped

    # Split text into sections
    sections: List[_Section] = []

    # Preamble (text before first heading)
    if positions[0][0] > 0:
        preamble = text[:positions[0][0]].strip()
        if preamble:
            sections.append(_Section(content=preamble))

    for i, (start, end, h_text, level) in enumerate(positions):
        # Section content = text between this heading end and next heading start
        if i + 1 < len(positions):
            section_text = text[end:positions[i + 1][0]].strip()
        else:
            section_text = text[end:].strip()

        sections.append(_Section(
            heading=h_text,
            heading_level=level,
            content=section_text,
        ))

    return sections


# ---------------------------------------------------------------------------
# Stage 3: Chunk
# ---------------------------------------------------------------------------

def _chunk_text(
    text: str,
    target_words: int,
    max_words: int,
    overlap_words: int,
    min_words: int,
) -> List[str]:
    """Split text into word-count-bounded chunks with overlap.

    Strategy:
    1. Split on paragraph boundaries (\n\n) first
    2. If a paragraph exceeds max_words, split on sentence boundaries
    3. Merge small paragraphs until target reached
    4. Add overlap from end of previous chunk
    """
    if not text.strip():
        return []

    words = text.split()
    if len(words) <= max_words:
        return [text.strip()] if len(words) >= min_words else []

    # Split into paragraphs
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks: List[str] = []
    current_words: List[str] = []

    for para in paragraphs:
        para_words = para.split()

        # If paragraph itself exceeds max, split by sentences
        if len(para_words) > max_words:
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sent in sentences:
                sent_words = sent.split()
                if len(current_words) + len(sent_words) > target_words and current_words:
                    chunk_text = " ".join(current_words)
                    if len(current_words) >= min_words:
                        chunks.append(chunk_text)
                    # Overlap: keep last N words
                    current_words = current_words[-overlap_words:] if overlap_words else []
                current_words.extend(sent_words)
        elif len(current_words) + len(para_words) > target_words and current_words:
            chunk_text = " ".join(current_words)
            if len(current_words) >= min_words:
                chunks.append(chunk_text)
            current_words = current_words[-overlap_words:] if overlap_words else []
            current_words.extend(para_words)
        else:
            current_words.extend(para_words)

    # Flush remainder
    if current_words and len(current_words) >= min_words:
        chunks.append(" ".join(current_words))

    return chunks


# ---------------------------------------------------------------------------
# Stage 4 + 5: Enrich and Emit
# ---------------------------------------------------------------------------

def _format_table(table: Dict) -> str:
    """Format a table dict into readable text."""
    lines = []
    headers = table.get("headers", [])
    rows = table.get("rows", [])

    if headers:
        lines.append(" | ".join(headers))
        lines.append("-" * (sum(len(h) + 3 for h in headers)))

    for row in rows:
        lines.append(" | ".join(str(cell) for cell in row))

    return "\n".join(lines)


def transform_page(
    page_data: dict,
    config: PipelineConfig = None,
) -> RAGDocument:
    """
    Transform a single crawled page into a RAGDocument with chunks.

    Args:
        page_data: Dict with keys: url, title, breadcrumb, section_path,
                   headings, text_content, tables, code_blocks,
                   parent_url, depth, word_count
        config: Pipeline configuration

    Returns:
        RAGDocument with populated chunks list
    """
    config = config or PipelineConfig()

    url = page_data.get("url", "")
    parsed = urlparse(url)
    domain = parsed.netloc

    doc = RAGDocument(
        source_url=url,
        parent_url=page_data.get("parent_url", ""),
        domain=domain,
        page_title=page_data.get("title", ""),
        breadcrumb=page_data.get("breadcrumb", []) or page_data.get("section_path", []),
        depth=page_data.get("depth", 0),
        headings=page_data.get("headings", {}),
        tables=page_data.get("tables", []),
        code_blocks=page_data.get("code_blocks", []),
    )

    # Stage 1: Clean
    raw_text = page_data.get("text_content", "")
    cleaned = _clean_text(raw_text, config)
    doc.full_text = cleaned
    doc.total_word_count = len(cleaned.split()) if cleaned else 0

    # Stage 2: Section by headings
    sections = _split_by_headings(cleaned, doc.headings)

    # Stage 3 + 4: Chunk and enrich
    chunk_index = 0
    heading_path: List[str] = []  # accumulates as we traverse sections

    for section in sections:
        # Update heading path
        if section.heading and section.heading_level > 0:
            # Trim path to current level
            while heading_path and _extract_level(heading_path[-1]) >= section.heading_level:
                heading_path.pop()
            heading_path.append(f"H{section.heading_level}: {section.heading}")

        section_title = section.heading or doc.page_title

        # Chunk the section text
        if section.content:
            text_chunks = _chunk_text(
                section.content,
                target_words=config.target_chunk_words,
                max_words=config.max_chunk_words,
                overlap_words=config.overlap_words,
                min_words=config.min_chunk_words,
            )

            for chunk_text in text_chunks:
                chunk = RAGChunk(
                    doc_id=doc.doc_id,
                    chunk_index=chunk_index,
                    source_url=url,
                    parent_url=doc.parent_url,
                    domain=domain,
                    page_title=doc.page_title,
                    section_title=section_title,
                    heading_path=list(heading_path),
                    breadcrumb=list(doc.breadcrumb),
                    depth=doc.depth,
                    chunk_type=ChunkType.TEXT,
                    content=chunk_text,
                )
                doc.chunks.append(chunk)
                chunk_index += 1

    # Stage 3b: Tables as separate chunks
    if config.table_as_separate_chunk:
        for table in doc.tables:
            table_text = _format_table(table)
            if table_text and len(table_text.split()) >= config.min_chunk_words:
                chunk = RAGChunk(
                    doc_id=doc.doc_id,
                    chunk_index=chunk_index,
                    source_url=url,
                    parent_url=doc.parent_url,
                    domain=domain,
                    page_title=doc.page_title,
                    section_title="Table",
                    heading_path=list(heading_path),
                    breadcrumb=list(doc.breadcrumb),
                    depth=doc.depth,
                    chunk_type=ChunkType.TABLE,
                    content=table_text,
                    metadata={"headers": table.get("headers", [])},
                )
                doc.chunks.append(chunk)
                chunk_index += 1

    # Stage 3c: Code blocks as separate chunks
    if config.code_as_separate_chunk:
        for code in doc.code_blocks:
            if code and len(code.split()) >= config.min_chunk_words:
                chunk = RAGChunk(
                    doc_id=doc.doc_id,
                    chunk_index=chunk_index,
                    source_url=url,
                    parent_url=doc.parent_url,
                    domain=domain,
                    page_title=doc.page_title,
                    section_title="Code Block",
                    heading_path=list(heading_path),
                    breadcrumb=list(doc.breadcrumb),
                    depth=doc.depth,
                    chunk_type=ChunkType.CODE,
                    content=code,
                )
                doc.chunks.append(chunk)
                chunk_index += 1

    doc.total_chunks = len(doc.chunks)

    logger.debug(
        f"[PIPELINE] {url[:60]} → {doc.total_chunks} chunks, "
        f"{doc.total_word_count} words"
    )

    return doc


def transform_batch(
    pages: List[dict],
    config: PipelineConfig = None,
) -> List[RAGDocument]:
    """Transform a batch of crawled pages into RAGDocuments."""
    config = config or PipelineConfig()
    docs = []
    for page in pages:
        try:
            doc = transform_page(page, config)
            docs.append(doc)
        except Exception as e:
            logger.warning(f"[PIPELINE] Failed to transform {page.get('url', '?')}: {e}")
    return docs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_level(heading_path_entry: str) -> int:
    """Extract heading level from 'H2: Title' format."""
    m = re.match(r"H(\d+):", heading_path_entry)
    return int(m.group(1)) if m else 0

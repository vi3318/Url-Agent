"""
RAG-Ready Data Model
====================
Structured data model for Retrieval-Augmented Generation pipelines.

Every crawled page is decomposed into **chunks** — self-contained units
of text with rich metadata.  The schema is designed for direct ingestion
into vector databases (Pinecone, Weaviate, Chroma, Qdrant, etc.) and
LLM retrieval workflows.

Key design principles:
- Each chunk is independently retrievable with full context
- Heading path preserves document hierarchy (H1 > H2 > H3)
- Tables and code blocks are first-class citizens (separate chunk types)
- Token counts pre-computed for embedding budget control
- Source provenance (URL, crawl timestamp, parent) always available
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ChunkType(str, Enum):
    """Type of content in a chunk."""
    TEXT = "text"
    TABLE = "table"
    CODE = "code"
    HEADING = "heading"      # standalone section header (thin content)
    MIXED = "mixed"          # text + inline code/tables


@dataclass
class RAGChunk:
    """
    A single retrieval unit for RAG pipelines.

    This is the atomic unit that gets embedded and stored in a vector DB.
    Every field is populated — no None values in the final output.
    """
    # Identity
    doc_id: str = ""           # deterministic hash of source_url
    chunk_id: str = ""         # deterministic: doc_id + chunk_index
    chunk_index: int = 0       # position within the document (0-based)

    # Source provenance
    source_url: str = ""
    parent_url: str = ""
    crawl_timestamp: str = ""  # ISO 8601
    domain: str = ""

    # Document structure
    page_title: str = ""
    section_title: str = ""    # nearest heading above this chunk
    heading_path: List[str] = field(default_factory=list)  # e.g. ["H1: Guide", "H2: Install"]
    breadcrumb: List[str] = field(default_factory=list)
    depth: int = 0             # crawl depth from start URL

    # Content
    chunk_type: ChunkType = ChunkType.TEXT
    content: str = ""          # cleaned plain text
    content_html: str = ""     # original HTML if available

    # Metrics
    word_count: int = 0
    char_count: int = 0
    token_estimate: int = 0    # rough: word_count * 1.3

    # Metadata bag (extensible)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.doc_id and self.source_url:
            self.doc_id = _make_doc_id(self.source_url)
        if not self.chunk_id and self.doc_id:
            self.chunk_id = f"{self.doc_id}_{self.chunk_index:04d}"
        if not self.crawl_timestamp:
            self.crawl_timestamp = datetime.now(timezone.utc).isoformat()
        if self.content and not self.word_count:
            self.word_count = len(self.content.split())
        if self.content and not self.char_count:
            self.char_count = len(self.content)
        if self.word_count and not self.token_estimate:
            self.token_estimate = int(self.word_count * 1.3)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict for JSON export."""
        return {
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "source_url": self.source_url,
            "parent_url": self.parent_url,
            "crawl_timestamp": self.crawl_timestamp,
            "domain": self.domain,
            "page_title": self.page_title,
            "section_title": self.section_title,
            "heading_path": self.heading_path,
            "breadcrumb": self.breadcrumb,
            "depth": self.depth,
            "chunk_type": self.chunk_type.value,
            "content": self.content,
            "content_html": self.content_html,
            "word_count": self.word_count,
            "char_count": self.char_count,
            "token_estimate": self.token_estimate,
            "metadata": self.metadata,
        }


@dataclass
class RAGDocument:
    """
    A fully processed document — one per crawled URL.

    Contains the original page data plus all derived chunks.
    """
    doc_id: str = ""
    source_url: str = ""
    parent_url: str = ""
    domain: str = ""
    page_title: str = ""
    breadcrumb: List[str] = field(default_factory=list)
    crawl_timestamp: str = ""
    depth: int = 0

    # Raw content (pre-chunking)
    full_text: str = ""
    full_html: str = ""
    headings: Dict[str, List[str]] = field(default_factory=dict)
    tables: List[Dict] = field(default_factory=list)
    code_blocks: List[str] = field(default_factory=list)

    # Derived chunks
    chunks: List[RAGChunk] = field(default_factory=list)

    # Stats
    total_word_count: int = 0
    total_chunks: int = 0

    def __post_init__(self):
        if not self.doc_id and self.source_url:
            self.doc_id = _make_doc_id(self.source_url)
        if not self.crawl_timestamp:
            self.crawl_timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "source_url": self.source_url,
            "parent_url": self.parent_url,
            "domain": self.domain,
            "page_title": self.page_title,
            "breadcrumb": self.breadcrumb,
            "crawl_timestamp": self.crawl_timestamp,
            "depth": self.depth,
            "total_word_count": self.total_word_count,
            "total_chunks": self.total_chunks,
            "headings": self.headings,
            "chunks": [c.to_dict() for c in self.chunks],
        }


@dataclass
class RAGCorpus:
    """
    Complete RAG corpus — all documents from a crawl run.

    Provides corpus-level statistics and serialization.
    """
    documents: List[RAGDocument] = field(default_factory=list)
    crawl_config: Dict[str, Any] = field(default_factory=dict)
    crawl_stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_documents(self) -> int:
        return len(self.documents)

    @property
    def total_chunks(self) -> int:
        return sum(d.total_chunks for d in self.documents)

    @property
    def total_words(self) -> int:
        return sum(d.total_word_count for d in self.documents)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "corpus_stats": {
                "total_documents": self.total_documents,
                "total_chunks": self.total_chunks,
                "total_words": self.total_words,
            },
            "crawl_config": self.crawl_config,
            "crawl_stats": self.crawl_stats,
            "documents": [d.to_dict() for d in self.documents],
        }

    def to_flat_chunks(self) -> List[Dict[str, Any]]:
        """Return a flat list of all chunks (for vector DB ingestion)."""
        chunks = []
        for doc in self.documents:
            for chunk in doc.chunks:
                chunks.append(chunk.to_dict())
        return chunks

    def export_json(self, filepath: str, *, flat_chunks: bool = False) -> str:
        """Export to JSON file. Use flat_chunks=True for vector DB format."""
        from pathlib import Path
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_flat_chunks() if flat_chunks else self.to_dict()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return str(path.absolute())

    def export_jsonl(self, filepath: str) -> str:
        """Export chunks as JSONL (one chunk per line) — ideal for streaming."""
        from pathlib import Path
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for doc in self.documents:
                for chunk in doc.chunks:
                    f.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")
        return str(path.absolute())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_doc_id(url: str) -> str:
    """Deterministic document ID from URL (SHA-256 prefix)."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]

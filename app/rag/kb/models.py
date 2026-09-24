from datetime import datetime
from typing import Literal, Self

import asyncpg
from pydantic import BaseModel, Field

from app.auth.levels import Level


Status = Literal["processing", "ready", "failed"]
Match_tag = Literal["any", "all"]

# Field descriptions below become each MCP tool's `outputSchema` (and the REST OpenAPI schema).
# --- Search -------------------------------------------------------------------------

# One retrieved chunk.
class Hit(BaseModel):
    rank: int = Field(description="1 = best match.")
    score: float = Field(description="Cosine similarity to the query, 0-1; hits below the relevance floor are already dropped.")
    document_name: str = Field(description="The document's unique name, for `search_by_document` or `get_document_outline`.")
    tags: list[str]
    heading: str | None = Field(description="Section path, e.g. 'Card Terms > Fees'; null before the first heading.")
    page_start: int | None = Field(description="First page of the passage, from 1; null for formats without pages.")
    page_end: int | None = Field(description="Last page of the passage; null for formats without pages.")
    chunk_id: str = Field(description="Pass to `get_chunk_context` to read the passages around this one.")
    chunk_index: int = Field(description="Position of the passage in its document, from 0.")
    text: str


# The filter a search ran with. All `None` means an unfiltered search.
class SearchFilters(BaseModel):
    tags: list[str] | None = None
    match: Match_tag | None = None
    documents: list[str] | None = None


class SearchResult(BaseModel):
    query: str
    filters: SearchFilters
    result_count: int
    results: list[Hit] = Field(description="Best first.")
    hint: str | None = Field(default=None, description="Only when nothing was found: what to try next.")


# --- Tags -------------------------------------------------------------------------

class TagInfo(BaseModel):
    tag: str = Field(description="Exact tag name for `search_by_tag` or `list_documents`.")
    description: str = Field(description="What the tag covers.")
    document_count: int

    @classmethod
    def from_row(cls, row: asyncpg.Record):
        """Maps a `tags` query row onto the shape the API sends."""
        return cls(
            tag=row["name"],
            description=row["description"],
            document_count=row["document_count"]
        )


class TagsResult(BaseModel):
    tags: list[TagInfo]


# --- Documents -------------------------------------------------------------------------

# MCP shape of a document
class DocumentSummary(BaseModel):
    document_id: str
    name: str = Field(description="Unique document name (its filename); works wherever a document id does.")
    tags: list[str]
    uploaded_at: datetime
    page_count: int | None = Field(description="Null for formats without pages.")
    chunk_count: int | None = Field(description="Number of searchable passages.")


# REST shape
class DocumentRecord(DocumentSummary):
    status: Status
    required_level: Level
    error: str | None = None

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Self:
        """Maps a `documents` row onto the shape the API sends"""
        return cls(
            document_id=str(row["id"]),
            name=row["filename"],
            tags=list(row["tags"]),
            uploaded_at=row["uploaded_at"],
            page_count=row["page_count"],
            chunk_count=row["chunk_count"],
            status=row["status"],
            required_level=row["required_level"],
            error=row["error"],
        )


class DocumentsResult(BaseModel):
    total: int = Field(description="Every matching document; more than returned means the list was cut off.")
    documents: list[DocumentSummary] = Field(description="Newest first.")
    hint: str | None = Field(default=None, description="When the list was cut off or nothing matched: how to narrow or widen it.")


class ListDocumentsResult(BaseModel):
    total: int
    documents: list[DocumentRecord]
    hint: str | None = None


# --- Chunk context -------------------------------------------------------------------------

# A neighbour chunk of a search hit.
class ContextChunk(BaseModel):
    chunk_id: str
    chunk_index: int
    heading: str | None
    page_start: int | None
    page_end: int | None
    text: str


# Finish a hit that was cut off mid-sentence or mid-table without another search.
class ChunkContextResult(BaseModel):
    chunk_id: str = Field(description="The passage the context was read around.")
    document_id: str
    document_name: str
    chunks: list[ContextChunk] = Field(description="In reading order, the anchor included.")


# --- Document outline -------------------------------------------------------------------------

# One section of a document: a heading path and where it sits.
class OutlineSection(BaseModel):
    heading: str | None = Field(description="Section path; null for text before the first heading or a document without headings.")
    page_start: int | None
    page_end: int | None
    chunk_count: int
    first_chunk_id: str = Field(description="Pass to `get_chunk_context` to read the section.")


# A document's table of contents, built from the headings its chunks were split at.
class DocumentOutline(BaseModel):
    document_id: str
    document_name: str
    page_count: int | None
    sections: list[OutlineSection] = Field(description="In reading order.")


# --- Ingestion -------------------------------------------------------------------------

# What `KnowledgeBase.stage` and `.ingest` return.
# REST reads it to choose `200` (Duplicate) or `202` (queued) and builds its own response body.
class IngestResult(BaseModel):
    document_id: str
    name: str
    status: Status
    tags: list[str]
    already_present: bool = False
    page_count: int | None = None
    chunk_count: int | None = None
    error: str | None = None

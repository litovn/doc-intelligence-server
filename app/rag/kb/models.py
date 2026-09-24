from datetime import datetime
from typing import Literal, Self

import asyncpg
from pydantic import BaseModel

from app.auth.levels import Level


Status = Literal["processing", "ready", "failed"]
Match_tag = Literal["any", "all"]


# --- Search -------------------------------------------------------------------------

# One retrieved chunk.
class Hit(BaseModel):
    rank: int  
    score: float  
    document_id: str
    document_name: str
    tags: list[str]  
    heading: str | None 
    page_start: int | None 
    page_end: int | None
    chunk_id: str   
    chunk_index: int 
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
    results: list[Hit]
    hint: str | None = None #suggest next steps to agent if no results


# --- Tags -------------------------------------------------------------------------

class TagInfo(BaseModel):
    tag: str
    description: str
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
    name: str
    tags: list[str]
    uploaded_at: datetime
    page_count: int | None 
    chunk_count: int | None


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
    total: int
    documents: list[DocumentSummary]


class ListDocumentsResult(BaseModel):
    total: int
    documents: list[DocumentRecord]


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
    chunk_id: str  # the anchor
    document_id: str
    document_name: str
    chunks: list[ContextChunk]


# --- Document outline -------------------------------------------------------------------------

# One section of a document: a heading path and where it sits.
class OutlineSection(BaseModel):
    heading: str | None  
    page_start: int | None
    page_end: int | None
    chunk_count: int
    first_chunk_id: str  
    

# A document's table of contents, built from the headings its chunks were split at.
class DocumentOutline(BaseModel):
    document_id: str
    document_name: str
    page_count: int | None
    sections: list[OutlineSection]


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

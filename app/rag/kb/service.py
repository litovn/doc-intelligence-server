import difflib
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import asyncpg

from app.auth.levels import Level, viewer_level, visible_levels
from app.config import settings
from app.rag import queries
from app.rag.ingestion.chunk import chunk_document
from app.rag.ingestion.clean import clean
from app.rag.ingestion.embed import OpenAIEmbedder
from app.rag.ingestion.parse import parse
from app.rag.kb.models import (
    ChunkContextResult,
    ContextChunk,
    DocumentOutline,
    DocumentRecord,
    Hit,
    IngestResult,
    ListDocumentsResult,
    Match_tag,
    OutlineSection,
    SearchFilters,
    SearchResult,
    TagInfo,
)

# HTTP status error 404
class UnknownDocumentError(ValueError):
    pass

# HTTP status error 409
class TagExistsError(ValueError):
    pass

# HTTP status error 403
class ForbiddenError(Exception):
    pass


def _decided_level(requested: Level | None) -> Level | None:
    """ Called only by `manager` to decide which access level an upload to set. 

    Args:
        requested: the level the upload asked for, or None if it asked for none.

    Returns:
        `requested` for a manager. None for anyone else.
    """
    return requested if viewer_level.get() == "manager" else None


def _suggest(name: str, known: Sequence[str], call: str) -> str:
    """ Suggest model to retry with a close match, when unknown-name error occurs.

    Args:
        name: the tag or document name that wasn't found.
        known: the valid names to compare it against.
        call: the tool that lists the valid names, e.g. "list_tags".

    Returns:
        E.g. " Did you mean XX? Call YY for the current list." 
    """
    close = difflib.get_close_matches(name, known, n=1)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    return f"{hint} Call `{call}` for the current list."


def _empty_hint(filters: SearchFilters) -> str:
    """ Tell the model what to try next when a search returns nothing, instead of a bare empty list."""

    if filters.tags:
        return (
            f"No chunks matched tag(s) {', '.join(filters.tags)} with match='{filters.match}'. "
            "Retry with `search` (no filter), `match='any'`, or check `list_tags`."
        )
    
    if filters.documents:
        return (
            f"No chunks matched in {', '.join(filters.documents)}. Retry with `search` (no filter) "
            "or check the exact name via `list_documents`."
        )

    return (
        f"Nothing scored above the relevance floor ({settings.relevance_floor}). Try other wording, "
        "or check `list_documents` — the knowledge base may not cover this topic."
    )


def _documents_hint(total: int, shown: int, *, filtered: bool) -> str | None:
    """ Tell the model how to narrow a cut-off document list, or what to try when a filter matched nothing."""
    if total == 0 and filtered:
        return "No document matches. Try fewer words in `name`, another `tag`, or `search` the topic instead."

    if shown < total:
        return f"Showing {shown} of {total}. Narrow with `name` or `tag`, or raise `limit` (max 200)."

    return None


def _name_words(name: str | None) -> list[str]:
    """ Split a `name` filter into words, so 'personal loan' matches 'contoso-personal-loan-manual.docx'."""
    return [w for w in re.split(r"[\s_.\-]+", name.lower()) if w] if name else []


def _body(text: str, section: str | None) -> str:
    """ A chunk's text without the heading path ingestion prefixed to it for embedding; hits carry it as `heading`."""
    return text.removeprefix(f"{section}\n\n") if section else text


def above_floor(rows: Sequence[Mapping[str, Any]]) -> list:
    """ Drop weak matches: keep hits scoring at least the relevance floor and hits containing every query word."""
    return [r for r in rows if r["score"] >= settings.relevance_floor or r["lexical"]]


# The one service REST and MCP both call.
class KnowledgeBase:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool  # Postgres connections, reused across requests
        self._embedder = OpenAIEmbedder()  # embeds chunks at ingest and queries at search time

    async def _search(self, query: str, *, top_k: int, filters: SearchFilters, document_ids: Sequence[UUID] | None = None) -> SearchResult:
        """ Search engine. Embed query, run one SQL filtered by level, drops hits below the relevance floor, and number.

        Args:
            query: the question as the user or model phrased it.
            top_k: maximum number of hits.
            filters: tags and match mode, or document names; echoed back in the result.
            document_ids: the resolved ids behind `filters.documents`, or None for no document filter.

        Returns:
            Hits best first, ranked from 1. When nothing is left, an empty list plus a `hint` saying what to try next.
        """

        vector = (await self._embedder.embed([query]))[0]

        rows = await queries.search(
            self._pool,
            vector,
            levels=visible_levels(),  
            top_k=top_k,
            tags=filters.tags,  
            match=filters.match or "any",
            document_ids=document_ids,  
            hybrid_text=query if settings.hybrid_search else None  
        )
        
        kept = above_floor(rows)

        hits = [
            Hit(
                rank=rank,
                score=round(r["score"], 4),
                document_name=r["filename"],
                tags=list(r["tags"]),
                heading=r["section"],
                page_start=r["page_start"],
                page_end=r["page_end"],
                chunk_id=r["id"],
                chunk_index=r["chunk_index"],
                text=_body(r["text"], r["section"])
            )
            for rank, r in enumerate(kept, start=1)  # rows arrive best first; rank from 1
        ]

        # Echo the query and filters back, so the model sees exactly what ran.
        return SearchResult(
            query=query,
            filters=filters,
            result_count=len(hits),
            results=hits,
            hint=None if hits else _empty_hint(filters) 
        )


    async def _document_row(self, id_or_name: str) -> asyncpg.Record | None:
        """ Find a document by id or filename.

        Args:
            id_or_name: a document id (UUID string) or a filename.

        Returns:
            The document row, or None if nothing matches.
        """
        try:
            return await queries.get_by_id(self._pool, UUID(id_or_name))
        except ValueError:
            return await queries.get_by_filename(self._pool, id_or_name)


    async def _resolve_documents(self, documents: Sequence[str]) -> list[UUID]:
        """ Turn document names or ids into ids, or raise naming the closest readable document.

        Args:
            documents: filenames or document ids, as the caller gave them.

        Returns:
            The ids, in the order given.

        Raises:
            UnknownDocumentError: a name matches no document the viewer may read.
        """
        ids, unknown = await queries.resolve_document_ids(self._pool, documents, levels=visible_levels())

        if unknown:
            known = [d.name for d in (await self.list_documents(limit=200)).documents]
            raise UnknownDocumentError(
                f"Unknown document {unknown[0]!r}." + _suggest(unknown[0], known, "list_documents")
            )
        return ids


    async def _check_tags(self, tags: Sequence[str]):
        """ Make sure at least one tag is given and every tag exists in the vocabulary."""
        if not tags:
            raise ValueError("At least one tag is required. Call `list_tags` for the vocabulary.")

        known = await queries.tag_names(self._pool)

        for tag in tags:
            if tag not in known:
                raise ValueError(
                    f"Unknown tag {tag!r}.{_suggest(tag, sorted(known), 'list_tags')} "
                    f"Valid tags: {', '.join(sorted(known))}."
                )


    async def _unknown_tag(self, name: str) -> ValueError:
        """ Build the error for a tag that doesn't exist, with the closest match."""
        known = sorted(await queries.tag_names(self._pool))
        return ValueError(f"Unknown tag {name!r}.{_suggest(name, known, 'list_tags')}")


    # --- Documents ---------------------------------------------------------------------

    async def stage(self, *, filename: str, content: bytes, tags: list[str], required_level: Level | None = None) -> IngestResult:
        """Phase 1 of `ingest`. Fast DB work, so REST runs it inside the request. 
        Checks the tags, hashes the content, and either finds a Duplicate or inserts a `processing` row. 
        
        Args:
            filename: the uploaded name; it identifies the document (same name = Replacement).
            content: the raw file bytes; their hash detects a Duplicate.
            tags: at least one, all from the vocabulary.
            required_level: the requested access level; only a manager's is applied.

        Returns:
            `status="ready"` with `already_present=True` for a Duplicate (nothing left to do),
            else `status="processing"` with the `document_id` for phase 2 to use.
        """
        await self._check_tags(tags)  
        content_hash = hashlib.sha256(content).hexdigest() 
        level = _decided_level(required_level) 

        # Duplicate: these exact bytes are already `ready` (under any filename). 
        # Nothing to parse or embed again; only the new tags (and a manager's level) are applied.
        duplicate = await queries.get_by_hash_ready(self._pool, content_hash, levels=visible_levels())
        if duplicate is not None:
            await queries.replace_tags(self._pool, duplicate["id"], tags)

            if level is not None:
                await queries.set_required_level(self._pool, duplicate["id"], level)

            return IngestResult(
                document_id=str(duplicate["id"]),
                name=duplicate["filename"],
                status="ready",
                tags=tags,
                already_present=True,
                page_count=duplicate["page_count"],
                chunk_count=duplicate["chunk_count"]
            )

        # Otherwise look up for the filename identity.
        existing = await queries.get_by_filename(self._pool, filename)

        # Same rule as `delete_document`: only a reader of the document may replace it, in any status.
        if existing is not None and existing["required_level"] not in visible_levels():
            raise ForbiddenError(f"Can't replace {filename!r}. Rename the file and upload it again.")

        if existing is not None and existing["status"] == "ready":
            document_id = existing["id"] # replacement: same name, new bytes.

        else:
            document_id = await queries.insert_processing(
                self._pool, filename=filename, content_hash=content_hash, required_level=level
            )
            await queries.replace_tags(self._pool, document_id, tags)
        
        return IngestResult(
            document_id=str(document_id), name=filename, status="processing", tags=tags
        )


    async def ingest(self, *, filename: str, content: bytes, tags: list[str], required_level: Level | None = None, document_id: str | None = None) -> IngestResult:
        """ Phase 2 of `ingest`. Turn an upload into searchable chunks. 
        Slow part, REST runs it in a BackgroundTask after answering 202.

        Args:
            filename: the uploaded name; same name as a `ready` document = Replacement.
            content: the raw file bytes.
            tags: at least one, all from the vocabulary.
            required_level: the requested access level; only a manager's is applied.
            document_id: the row phase 1 already staged; None runs `stage` first.

        Returns:
            `status="ready"` with page and chunk counts; 
            `already_present=True` for a Duplicate;
            `status="failed"` with `error`.
        """
        if document_id is None:
            staged = await self.stage(filename=filename, content=content, tags=tags, required_level=required_level)
            if staged.already_present:
                return staged
            document_id = staged.document_id

        document_id = UUID(document_id)
        content_hash = hashlib.sha256(content).hexdigest()

        row = await queries.get_by_id(self._pool, document_id)
        live = row is not None and row["status"] == "ready"

        try:
            # parse -> clean -> chunk -> embed, then build one `ChunkRow` per chunk with id `{document_id}:{hash8}:{chunk_index}`.
            parsed = clean(await parse(filename, content))
            chunks = chunk_document(parsed.markdown, parsed.pages)
            if not chunks: 
                raise ValueError("No text found in the file.")
            vectors = await self._embedder.embed([c.text for c in chunks])
            rows = [
                queries.ChunkRow(
                    id=f"{document_id}:{content_hash[:8]}:{c.chunk_index}",
                    chunk_index=c.chunk_index,
                    text=c.text,
                    embedding=vector,
                    page_start=c.page_start,
                    page_end=c.page_end,
                    section=c.section or None
                )
                for c, vector in zip(chunks, vectors, strict=True)
            ]

            await queries.replace_chunks(
                self._pool,
                document_id,
                rows,
                tags=tags,
                content_hash=content_hash,
                page_count=parsed.page_count,
                # A live Replacement's level only changes here, in the swap (01-architecture.md).
                required_level=_decided_level(required_level)
            )

            if live:  # deferred so the old chunks keep their tags until the swap commits
                await queries.replace_tags(self._pool, document_id, tags)

        except Exception as exc:  
            # On any failure document is marked `failed` and the error is logged.
            error = str(exc) or type(exc).__name__
            if not live:  
                await queries.mark_failed(self._pool, document_id, error)

            return IngestResult(
                document_id=str(document_id),
                name=filename,
                status="failed",
                tags=tags,
                error=error
            )

        return IngestResult(
            document_id=str(document_id),
            name=filename,
            status="ready",
            tags=tags,
            page_count=parsed.page_count,
            chunk_count=len(rows)
        )


    async def list_documents(self, *, tag: str | None = None, name: str | None = None, ready_only: bool = True, limit: int = 50) -> ListDocumentsResult:
        """ List documents, newest first, limited to what the viewer may read.

        Args:
            tag: only documents carrying this tag; must exist in the vocabulary.
            name: only documents whose filename contains every word of it, case-insensitive.
            ready_only: False to include `processing` and `failed` rows (the UI polls them).
            limit: maximum number of documents returned.

        Returns:
            `total` matching documents and up to `limit` of them;
            `total` above the number returned means the list was cut off, and `hint` says how to narrow it.
        """
        if tag is not None:
            await self._check_tags([tag])

        words = _name_words(name)
        total, rows = await queries.list_documents(
            self._pool,
            levels=visible_levels(),
            tag=tag,
            name_words=words,
            ready_only=ready_only,
            limit=limit,
        )

        return ListDocumentsResult(
            total=total,
            documents=[DocumentRecord.from_row(r) for r in rows],
            hint=_documents_hint(total, len(rows), filtered=tag is not None or bool(words)),
        )


    async def delete_document(self, id_or_name: str):
        """ Delete a document at cascade."""

        row = await self._document_row(id_or_name)
        if row is None:
            raise UnknownDocumentError(
                f"Unknown document {id_or_name!r}. Call `list_documents` for the current list."
            )
        
        if row["required_level"] not in visible_levels():
            raise ForbiddenError(f"Only a manager can delete {row['filename']!r}.")

        await queries.delete_document(self._pool, row["id"])


    # --- Tag vocabulary ----------------------------------------------------------------

    async def list_tags(self, *, only_in_use: bool = True) -> list[TagInfo]:
        """ List the tag vocabulary with how many documents carry each tag.

        Args:
            only_in_use: 
                True (MCP) hides tags with no readable document, so the agent is only offered tags that can return results; 
                False (the REST tags page) lists every tag, including new ones not used yet.

        Returns:
            One entry per tag, sorted by name: tag, description, document count.
        """
        rows = await queries.list_tags(
            self._pool,
            levels=visible_levels(),  
            only_in_use=only_in_use,
        )

        return [TagInfo.from_row(r) for r in rows]


    async def create_tag(self, name: str, description: str) -> TagInfo:
        """ Add a tag to the vocabulary under a normalised name."""
        slug = re.sub(r"[\W_]+", "-", name.lower()).strip("-")  # Unicode letters kept: "Qualità" -> "qualità"
        if not slug:
            raise ValueError(f"Tag name {name!r} has no usable characters.")

        if not await queries.create_tag(self._pool, slug, description):
            raise TagExistsError(f"Tag {slug!r} already exists. Edit its description instead.")

        return TagInfo.from_row(await queries.get_tag(self._pool, slug))


    async def update_tag_description(self, name: str, description: str) -> TagInfo:
        """ Change what a tag's description says; its name stays the same."""
        if not await queries.update_tag_description(self._pool, name, description):
            raise await self._unknown_tag(name)

        return TagInfo.from_row(await queries.get_tag(self._pool, name))


    async def delete_tag(self, name: str):
        """ Remove a tag from the vocabulary, only if no document carries it."""
        if not await queries.delete_tag(self._pool, name):
            raise await self._unknown_tag(name)

    # --- Search ------------------------------------------------------------------------

    async def search(self, query: str, *, top_k: int = 5) -> SearchResult:
        """ Search top_k number of hits for the given query in every document the viewer may read, with no filter."""
        return await self._search(query, top_k=top_k, filters=SearchFilters())


    async def search_by_tag(self, query: str, tags: list[str], *, match: Match_tag = "any", top_k: int = 5) -> SearchResult:
        """ Search top_k number of hits for the given query in only documents carrying the given tags."""
        await self._check_tags(tags)
        return await self._search(query, top_k=top_k, filters=SearchFilters(tags=tags, match=match))


    async def search_by_document(self, query: str, documents: list[str], *, top_k: int = 5) -> SearchResult:
        """ Search top_k number of hits for the given query in only the named documents."""
        if not documents:
            raise ValueError("Give at least one document name or id.")

        ids = await self._resolve_documents(documents)

        return await self._search(
            query,
            top_k=top_k,
            filters=SearchFilters(documents=documents),
            document_ids=ids,
        )


    async def get_chunk_context(self, chunk_id: str, *, before: int = 1, after: int = 1) -> ChunkContextResult:
        """ Return a search hit together with its neighbouring chunks, in reading order."""

        anchor = await queries.get_chunk(self._pool, chunk_id, levels=visible_levels())
        if anchor is None:
            raise ValueError(
                f"Unknown chunk_id {chunk_id!r}. Chunk IDs come from search results."
            )
        
        rows = await queries.get_chunk_context(
            self._pool, anchor["document_id"], anchor["chunk_index"], before=before, after=after
        )

        return ChunkContextResult(
            chunk_id=chunk_id,
            document_id=str(anchor["document_id"]),
            document_name=anchor["filename"],
            chunks=[
                ContextChunk(
                    chunk_id=r["id"],
                    chunk_index=r["chunk_index"],
                    heading=r["section"],
                    page_start=r["page_start"],
                    page_end=r["page_end"],
                    text=_body(r["text"], r["section"]),
                )
                for r in rows
            ],
        )


    async def get_document_outline(self, document: str) -> DocumentOutline:
        """ Return a document's sections (heading paths) in reading order, with pages and chunk counts."""

        [document_id] = await self._resolve_documents([document])
        row = await queries.get_by_id(self._pool, document_id)
        sections = await queries.get_document_outline(self._pool, document_id)

        return DocumentOutline(
            document_id=str(document_id),
            document_name=row["filename"],
            page_count=row["page_count"],
            sections=[
                OutlineSection(
                    heading=s["section"] or None,  # "" = text before the first heading
                    page_start=s["page_start"],
                    page_end=s["page_end"],
                    chunk_count=s["chunk_count"],
                    first_chunk_id=s["first_chunk_id"],
                )
                for s in sections
            ],
        )

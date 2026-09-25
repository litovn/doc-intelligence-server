from collections.abc import Sequence
from typing import NamedTuple
from uuid import UUID

import asyncpg

from app.rag.kb.models import Match_tag

_DOC_COLUMNS = """
  d.id, d.filename, d.content_hash, d.uploaded_at, d.page_count, d.chunk_count, d.status, d.required_level, d.error,
  COALESCE(ARRAY_AGG(dt.tag ORDER BY dt.tag) FILTER (WHERE dt.tag IS NOT NULL), '{}') AS tags
"""
_DOC_FROM = """
  FROM documents d LEFT JOIN document_tags dt ON dt.document_id = d.id
"""


class ChunkRow(NamedTuple):
    id: str
    chunk_index: int
    text: str
    embedding: list[float]
    page_start: int | None
    page_end: int | None
    section: str | None


class TagInUseError(RuntimeError):

    def __init__(self, tag: str, documents: list[str]):
        super().__init__(f"Tag {tag!r} is still used by: {', '.join(documents)}")
        self.tag = tag
        self.documents = documents


# --- Documents -------------------------------------------------------------------------


async def insert_processing(pool: asyncpg.Pool, *, filename: str, content_hash: str, required_level: str | None = None) -> UUID:
    """ Phase 1:
    Commit a pollable `processing` row for a new document, or re-upload one that `failed`.
    If the filename already exists, update its content_hash and reset it to `processing`.

    Args:
        filename: the document's unique name.
        content_hash: hash of the uploaded bytes, used later to spot duplicate uploads.
        required_level: who may see the document, "employee" or "manager".

    Returns:
        The document's id: a new one for a new filename, the existing one on a re-upload.
        Phase 2 uses it to attach the chunks.
    """

    doc_id: UUID = await pool.fetchval(
        """
        INSERT INTO documents (filename, content_hash, status, required_level)
        VALUES ($1, $2, 'processing', COALESCE($3::text, 'employee'))
        ON CONFLICT (filename) DO UPDATE
          SET content_hash = EXCLUDED.content_hash, status = 'processing',
              uploaded_at = now(), error = NULL, chunk_count = NULL, page_count = NULL,
              required_level = COALESCE($3::text, documents.required_level)
        RETURNING id
        """,
        filename,
        content_hash,
        required_level
    )
    return doc_id


async def mark_failed(pool: asyncpg.Pool, document_id: UUID, error: str):
    """ Phase 2 failed: Turn `processing` row to `failed` with the message the UI shows."""
    await pool.execute(
        "UPDATE documents SET status = 'failed', error = $2 WHERE id = $1", 
        document_id, 
        error
    )


async def set_required_level(pool: asyncpg.Pool, document_id: UUID, level: str):
    await pool.execute(
        "UPDATE documents SET required_level = $2 WHERE id = $1", 
        document_id, 
        level
    )


async def get_by_filename(pool: asyncpg.Pool, filename: str):
    return await pool.fetchrow(
        f"SELECT {_DOC_COLUMNS} {_DOC_FROM} WHERE d.filename = $1 GROUP BY d.id", 
        filename
    )


async def get_by_id(pool: asyncpg.Pool, document_id: UUID):
    return await pool.fetchrow(
        f"SELECT {_DOC_COLUMNS} {_DOC_FROM} WHERE d.id = $1 GROUP BY d.id", 
        document_id
    )


async def get_by_hash_ready(pool: asyncpg.Pool, content_hash: str, *, levels: Sequence[str]):
    """Duplicate check. `ready` rows only, so re-uploading a `failed` document retries it;
    readable ones only, so an upload never learns of, or retags, a document its uploader can't see."""

    return await pool.fetchrow(
        f"SELECT {_DOC_COLUMNS} {_DOC_FROM} "
        "WHERE d.content_hash = $1 AND d.status = 'ready' AND d.required_level = ANY($2::text[]) "
        "GROUP BY d.id LIMIT 1",
        content_hash,
        list(levels)
    )


async def list_documents(pool: asyncpg.Pool, *, levels: Sequence[str], tag: str | None = None,
                         name_words: Sequence[str] = (), ready_only: bool = True, limit: int = 50
    ) -> tuple[int, list[asyncpg.Record]]:
    """List of the documents the viewer may see, newest first, plus the total number of matches.

    Args:
        levels: access levels the viewer may read, documents above the viewer's level are never considered.
        tag: only documents carrying this tag.
        name_words: only documents whose filename contains every one of these words, case-insensitive.
        ready_only: True to hide `processing` and `failed` documents. False, otherwhise
        limit: maximum number of rows returned.

    Returns:
        (total, rows). Ordered by upload time, newest first, then filename.
        `total` counts every match and ignores `limit`. 
        `rows` has one row per document. 
        if len(total) != len(rows), caller knows the list was cut off.
    """

    args: list[object] = [list(levels)]
    where = ["d.required_level = ANY($1::text[])"]  # show only documents the viewer is allowed to see

    if tag is not None:
        args.append(tag)
        where.append(
            f"EXISTS (SELECT 1 FROM document_tags f WHERE f.document_id = d.id AND f.tag = ${len(args)})"
        )

    for word in name_words:  #a word matches only itself
        args.append("%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        where.append(f"d.filename ILIKE ${len(args)}")

    if ready_only:
        where.append("d.status = 'ready'")

    clause = " AND ".join(where)
    total: int = await pool.fetchval(f"SELECT count(*) FROM documents d WHERE {clause}", *args)
    rows = await pool.fetch(
        f"SELECT {_DOC_COLUMNS} {_DOC_FROM} WHERE {clause} GROUP BY d.id "
        f"ORDER BY d.uploaded_at DESC, d.filename LIMIT ${len(args) + 1}",
        *args,
        limit
    )

    return total, rows


async def delete_document(pool: asyncpg.Pool, document_id: UUID):
    await pool.execute(
        "DELETE FROM documents WHERE id = $1", 
        document_id
    )


async def resolve_document_ids(pool: asyncpg.Pool, names_or_ids: Sequence[str], *, levels: Sequence[str]) -> tuple[list[UUID], list[str]]:
    """Caller may call filename or document id, report the ids that were found and the names that weren't.
    A document the viewer isn't allowed to read is treated as not found.

    Args:
        names_or_ids: filename ("handbook.pdf") or a document id as a string.
        levels: access levels the viewer may read. A document above them counts as unknown.

    Returns:
        (ids, unknown). 
        `ids` are the matched document ids, in the order they were given.
        `unknown` are the inputs that matched no document the viewer may read.
    """

    rows = await pool.fetch(
        "SELECT id, filename FROM documents WHERE required_level = ANY($2::text[]) "
        "AND (filename = ANY($1::text[]) OR id::text = ANY($1::text[]))",
        list(names_or_ids),
        list(levels)
    )

    # Each document is findable by its id string and by its filename.
    found = {str(r["id"]): r["id"] for r in rows} | {r["filename"]: r["id"] for r in rows}

    ids: list[UUID] = []
    unknown: list[str] = []

    for name_or_id in names_or_ids:
        if name_or_id in found:
            ids.append(found[name_or_id])
        else:
            unknown.append(name_or_id)

    return ids, unknown


# --- Tags ------------------------------------------------------------------------------


async def list_tags(pool: asyncpg.Pool, *, levels: Sequence[str], only_in_use: bool = True) -> list[asyncpg.Record]:
    """List the tags, each with how many documents use it. Counts only the `ready` documents the viewer may read.

    Args:
        levels: access levels the viewer may read. Documents above them aren't counted.
        only_in_use: True, to leave out tags with a count of 0. False, to list every tag.

    Returns:
        One row per tag, sorted by name: `name`, `description`, `document_count`.
    """

    having = "HAVING count(d.id) > 0" if only_in_use else ""

    return await pool.fetch(
        f"""
        SELECT t.name, t.description, count(d.id) AS document_count
        FROM tags t
        LEFT JOIN document_tags dt ON dt.tag = t.name
        LEFT JOIN documents d ON d.id = dt.document_id AND d.status = 'ready'
                             AND d.required_level = ANY($1::text[])
        GROUP BY t.name, t.description {having}
        ORDER BY t.name
        """,
        list(levels)
    )


async def get_tag(pool: asyncpg.Pool, name: str) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        SELECT t.name, t.description, count(d.id) AS document_count
        FROM tags t
        LEFT JOIN document_tags dt ON dt.tag = t.name
        LEFT JOIN documents d ON d.id = dt.document_id AND d.status = 'ready'
        WHERE t.name = $1 GROUP BY t.name, t.description
        """,
        name
    )


async def tag_names(pool: asyncpg.Pool) -> set[str]:
    rows = await pool.fetch("SELECT name FROM tags")

    return {row["name"] for row in rows}


async def create_tag(pool: asyncpg.Pool, name: str, description: str) -> bool:
    """ Insert a new tag; False if the name is taken, leaving the existing tag untouched."""
    result = await pool.execute(
        "INSERT INTO tags (name, description) VALUES ($1, $2) ON CONFLICT (name) DO NOTHING",
        name,
        description
    )
    return result == "INSERT 0 1"


async def update_tag_description(pool: asyncpg.Pool, name: str, description: str) -> bool:
    result = await pool.execute(
        "UPDATE tags SET description = $2 WHERE name = $1", 
        name, 
        description
    )
    
    return result != "UPDATE 0" # True if tag was updated, False if tag not found


async def delete_tag(pool: asyncpg.Pool, name: str) -> bool:
    using = await pool.fetch(
        "SELECT d.filename FROM document_tags dt JOIN documents d ON d.id = dt.document_id "
        "WHERE dt.tag = $1 ORDER BY d.filename",
        name
    )

    if using:
        raise TagInUseError(name, [row["filename"] for row in using])

    result = await pool.execute(
        "DELETE FROM tags WHERE name = $1", 
        name
    )

    return result != "DELETE 0"  # True if tag was deleted, False no tag with that name


async def replace_tags(pool: asyncpg.Pool, document_id: UUID, tags: Sequence[str]):
    """Replace a document's old tags with new `tags`.

    Args:
        document_id: the document to retag.
        tags: the complete new set of tag names; they must already exist in `tags`.
    """

    async with pool.acquire() as conn: #borrow connection from the pool
        async with conn.transaction(): #run all statements in a single transaction, so a reader never sees a mix of old and new tags

            #remove all current tags for the document
            await conn.execute(
                "DELETE FROM document_tags WHERE document_id = $1", 
                document_id
            )

            #insert the new tags for the document, one link per tag
            await conn.executemany(
                "INSERT INTO document_tags (document_id, tag) VALUES ($1, $2)",
                [(document_id, t) for t in tags]
            )

            #update the document's tags in the chunks table
            await conn.execute(
                "UPDATE chunks SET tags = $2::text[] WHERE document_id = $1", 
                document_id, 
                list(tags)
            )


# --- Chunks ----------------------------------------------------------------------------

async def replace_chunks(pool: asyncpg.Pool, document_id: UUID, chunks: Sequence[ChunkRow], *, tags: Sequence[str], 
                         content_hash: str, page_count: int | None, required_level: str | None = None
    ):
    """Phase 2: 
    Save a document's chunks and mark it `ready`. Used for a new upload and for replacing an existing document.
    Delete the document's old chunks (if any), insert new ones, and update the document row. 
    Search never sees a document half-saved: no chunks without `ready`, no `ready` without chunks, old and new chunks never mixed.

    Args:
        document_id: the document the chunks belong to (from phase 1).
        chunks: the new chunks, already embedded.
        tags: the document's tags.
        content_hash: hash of the uploaded bytes, stored on the document.
        page_count: number of pages, or None for formats without pages.
        required_level: new access level, or None to keep the current one.
    """

    async with pool.acquire() as conn: #borrow connection from the pool
        async with conn.transaction(): #run all statements in a single transaction, so a reader never sees a mix of old and new chunks

            #delete any existing chunks for the document, if any
            await conn.execute(
                "DELETE FROM chunks WHERE document_id = $1", 
                document_id
            )

            #insert the new chunks for the document, one row per chunk
            await conn.executemany(
                """
                INSERT INTO chunks
                (id, document_id, chunk_index, text, embedding, page_start, page_end, section, tags)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::text[])
                """,
                [
                    (c.id, document_id, c.chunk_index, c.text, c.embedding, c.page_start, c.page_end,
                    c.section, list(tags))
                    for c in chunks
                ],
            )

            #update the document's metadata
            await conn.execute(
                "UPDATE documents SET status = 'ready', error = NULL, content_hash = $2, "
                "chunk_count = $3, page_count = $4, "
                "required_level = COALESCE($5::text, required_level) WHERE id = $1",
                document_id,
                content_hash,
                len(chunks),
                page_count,
                required_level
            )


async def get_chunk(pool: asyncpg.Pool, chunk_id: str, *, levels: Sequence[str]) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT c.id, c.document_id, c.chunk_index, d.filename FROM chunks c "
        "JOIN documents d ON d.id = c.document_id "
        "WHERE c.id = $1 AND d.required_level = ANY($2::text[])",
        chunk_id,
        list(levels)
    )


async def get_chunk_context(pool: asyncpg.Pool, document_id: UUID, chunk_index: int, *, before: int, after: int) -> list[asyncpg.Record]:
    """Fetch a chunk together with its neighbours in the same document, in reading order.

    Args:
        document_id: the document the chunk belongs to.
        chunk_index: position of the chunk in the document (0, 1, 2...).
        before: how many chunks before it to include.
        after: how many chunks after it to include.

    Returns:
        The chunks from `chunk_index - before` to `chunk_index + after`, ordered by position
    """
    return await pool.fetch(
        "SELECT id, chunk_index, text, page_start, page_end, section FROM chunks "
        "WHERE document_id = $1 AND chunk_index BETWEEN $2 AND $3 ORDER BY chunk_index",
        document_id,
        chunk_index - before,
        chunk_index + after
    )


async def get_document_outline(pool: asyncpg.Pool, document_id: UUID) -> list[asyncpg.Record]:
    """One row per section (heading path) of a document, in reading order.

    Args:
        document_id: the document to outline.

    Returns:
        Rows of `section`, `page_start`, `page_end`, `chunk_count` and `first_chunk_id`.
        `section` is empty for text before the first heading.
    """
    return await pool.fetch(
        "SELECT section, min(page_start) AS page_start, max(page_end) AS page_end, "
        "count(*) AS chunk_count, (array_agg(id ORDER BY chunk_index))[1] AS first_chunk_id "
        "FROM chunks WHERE document_id = $1 GROUP BY section ORDER BY min(chunk_index)",
        document_id
    )


async def count_chunks(pool: asyncpg.Pool, document_id: UUID) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM chunks WHERE document_id = $1", 
        document_id
    )


# --- Retrieval core ----------------------------------------------------------------------------

async def search(pool: asyncpg.Pool, embedding: list[float], *, levels: Sequence[str], top_k: int = 5, tags: Sequence[str] | None = None, 
                 match: Match_tag = "any", document_ids: Sequence[UUID] | None = None, hybrid_text: str | None = None,
    ) -> list[asyncpg.Record]:
    """Find the chunks most relevant to a question. Dense (default) or hybrid (dense + hybrid_text) search. 
    The caller may filter by access level, tags, and document.

    Args:
        embedding: the question, embedded with the same model as the chunks.
        levels: access levels the viewer may read. Chunks of other documents are never returned.
        top_k: maximum number of chunks returned.
        tags: only chunks carrying these tags (None = no tag filter).
        match: "any" = the chunk has at least one of `tags`; "all" = it has every one.
        document_ids: only chunks from these documents (None = all documents).
        hybrid_text: the question as plain text; turns on hybrid mode. None = dense only.

    Returns:
        Up to `top_k` rows, best first. Each row has the chunk (id, ..., tags), the document's filename, and:
        - `score`: cosine similarity to the question, higher is better.
        - `lexical`: True if the chunk contains every word of the question, False otherwise (dense mode).
    """

    args: list[object] = [embedding, list(levels)]
    where = ["d.status = 'ready'", "d.required_level = ANY($2::text[])"]

    if tags:
        args.append(list(tags))
        where.append(f"c.tags {'&&' if match == 'any' else '@>'} ${len(args)}::text[]")

    if document_ids:
        args.append(list(document_ids))
        where.append(f"c.document_id = ANY(${len(args)}::uuid[])")

    filters = " AND ".join(where)

    # Returned columns; <=> is cosine distance, so 1 - distance = similarity (higher is better).
    columns = """c.id, c.document_id, c.chunk_index, c.text, c.page_start, c.page_end, c.section, c.tags, d.filename, 1 - (c.embedding <=> $1) AS score"""

    # If Dense only (HNSW index).
    if hybrid_text is None:
        args.append(top_k)
        return await pool.fetch(
            f"""
            SELECT {columns}, FALSE AS lexical
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE {filters}
            ORDER BY c.embedding <=> $1
            LIMIT ${len(args)}
            """,
            *args
        )

    # Hybrid: three more parameters, the question text, candidates per side (3x top_k), final count.
    args += [hybrid_text, top_k * 3, top_k]
    text, pool_size, k = (f"${i}" for i in range(len(args) - 2, len(args) + 1))

    return await pool.fetch(
        f"""
        -- DENSE: best candidates by meaning, with their rank (1, 2, 3...)
        WITH dense AS (
          SELECT c.id, RANK() OVER (ORDER BY c.embedding <=> $1) AS r
          FROM chunks c JOIN documents d ON d.id = c.document_id
          WHERE {filters}
          ORDER BY c.embedding <=> $1 LIMIT {pool_size}

        -- SPARSE: best candidates by words; the & -> | swap matches ANY word of the question
        ), sparse AS (
          SELECT c.id, RANK() OVER (ORDER BY ts_rank(c.tsv, q) DESC) AS r
          FROM chunks c JOIN documents d ON d.id = c.document_id,
               to_tsquery('simple', replace(plainto_tsquery('simple', {text})::text, '&', '|')) q
          WHERE {filters} AND c.tsv @@ q
          ORDER BY ts_rank(c.tsv, q) DESC LIMIT {pool_size}

        -- FUSED: reciprocal rank fusion; a chunk found by both sides scores highest
        ), fused AS (
          SELECT COALESCE(dense.id, sparse.id) AS id,
                 COALESCE(1.0 / (60 + dense.r), 0) + COALESCE(1.0 / (60 + sparse.r), 0) AS rrf
          FROM dense FULL OUTER JOIN sparse ON dense.id = sparse.id
        )

        -- FINAL: full rows, best fused rank first; lexical = the chunk has EVERY question word
        SELECT {columns}, c.tsv @@ plainto_tsquery('simple', {text}) AS lexical
        FROM fused f JOIN chunks c ON c.id = f.id JOIN documents d ON d.id = c.document_id
        ORDER BY f.rrf DESC, score DESC
        LIMIT {k}
        """,
        *args
    )


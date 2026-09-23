import re
from bisect import bisect_right
from collections.abc import Sequence
from typing import Literal, NamedTuple

import tiktoken

from app.rag.ingestion.parse import PageSpan

_ENC = tiktoken.get_encoding("cl100k_base")  # tokenizer family of text-embedding-3-*
MAX_TOKENS = 300 
OVERLAP_TOKENS = 60  

# Whole HTML table (DI output). Found first so blank lines inside it can't split it.
_TABLE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
# Tables bigger than one chunk, row boundary so split falls between rows, and each piece gets the header row repeated
_ROW = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)
# Paragraphs longer than one chunk, cut after a full stop.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


# Actual output chunk.
class Chunk(NamedTuple):
    chunk_index: int
    text: str
    section: str
    page_start: int
    page_end: int


# Structural piece of the Markdown, before chunking.
class _Block(NamedTuple):
    kind: Literal["heading", "table", "para"]
    text: str
    start: int
    end: int


def _count(text: str) -> int:
    """ Token count as the embedding model sees it."""
    return len(_ENC.encode(text, disallowed_special=()))


def _blocks(text: str) -> list[_Block]:
    """ Split Markdown into headings, tables, and paragraphs, keeping exact offsets."""

    blocks: list[_Block] = []
    pos = 0

    for m in _TABLE.finditer(text):
        blocks += _text_blocks(text, pos, m.start())  # the text before this table
        blocks.append(_Block("table", m.group(0), m.start(), m.end()))
        pos = m.end()

    return blocks + _text_blocks(text, pos, len(text))  # the text after the last table


def _text_blocks(text: str, start: int, end: int) -> list[_Block]:
    """ Split given text into heading and paragraph blocks, keeping exact offsets.

    Args:
        text: the whole Markdown document.
        start: where the table-free stretch to split begins in `text`.
        end: where it ends (exclusive).

    Returns:
        Heading and paragraph blocks in order, with offsets into the whole `text`.
    """

    blocks: list[_Block] = []
    para_lines: list[str] = []  
    para_start = offset = start

    for line in text[start:end].splitlines(keepends=True):
        stripped = line.strip()
        heading = stripped.startswith("#")

        if para_lines and (not stripped or heading): # paragraph is open
            blocks.append(_Block("para", "".join(para_lines).strip(), para_start, offset))
            para_lines = []

        if heading:
            blocks.append(_Block("heading", stripped, offset, offset + len(line)))
        elif stripped:
            para_start = offset if not para_lines else para_start  # first line of a new paragraph
            para_lines.append(line)

        offset += len(line)

    if para_lines:
        blocks.append(_Block("para", "".join(para_lines).strip(), para_start, offset))

    return blocks


def _split_paragraph(body: str, limit: int) -> list[str]:
    """ Make sure a paragraph is small enough to fit in a chunk. Else cut into smaller pieces.

    Args:
        body: one paragraph's text.
        limit: token budget per chunk body (after the heading prefix).

    Returns:
        The paragraph as-is if it fits; else pieces of whole sentences with overlap tail.
    """
    if _count(body) <= limit:
        return [body]
    
    cap = max(limit - OVERLAP_TOKENS, 1)  # leave room for the overlap tail in front
    atoms: list[str] = []
    for sentence in _SENTENCE.split(body): # cut
        atoms += [sentence] if _count(sentence) <= cap else _hard_split(sentence, cap)

    return _pack(atoms, cap, " ") # rejoin


def _split_table(table: str, limit: int) -> list[str]:
    """ Make sure a table fits in a chunk. Else split between rows.

    Args:
        table: one whole HTML table, `<table>…</table>`.
        limit: token budget per chunk body (after the heading prefix).

    Returns:
        The table as-is if it fits; else complete tables, each with the header row repeated.
    """
    if _count(table) <= limit:
        return [table]
    
    header, *rows = _ROW.findall(table) or [table]
    if not rows:  # no row structure to split on
        return _hard_split(table, limit)
    
    cap = max(limit - _count(f"<table>{header}</table>"), 1)

    return [f"<table>{header}{piece}</table>" for piece in _pack(rows, cap, "")] #pack rows, rewrap each piece


def _pack(parts: Sequence[str], cap: int, sep: str) -> list[str]:
    """ Greedily join consecutive parts into pieces of at most `cap` tokens.
    A single part larger than `cap` becomes its own piece.

    Args:
        parts: the units to group, in order: sentences or table rows.
        cap: token budget per piece.
        sep: " " for sentences, "" for table rows.

    Returns:
        The pieces in the original order; empty if `parts` is empty.
    """
    out: list[str] = []
    cur: list[str] = []
    used = 0

    for part in parts:
        n = _count(part)
        if cur and used + n > cap:  # start a new piece
            out.append(sep.join(cur))
            cur, used = [], 0
        cur.append(part)
        used += n

    return out + [sep.join(cur)] if cur else out


def _hard_split(text: str, cap: int) -> list[str]:
    """ Last resort: cut every `cap` tokens, ignoring words and sentences.

    Args:
        text: text with no better place to cut (a huge sentence, a table without rows).
        cap: tokens per piece.

    Returns:
        Pieces of `cap` tokens each, the last one shorter. A cut can fall mid-word.
    """
    ids = _ENC.encode(text, disallowed_special=())
    return [_ENC.decode(ids[i : i + cap]) for i in range(0, len(ids), cap)]


def _tail(body: str) -> str:
    """ The last OVERLAP_TOKENS tokens of a chunk body.

    Args:
        body: the text of the chunk just emitted, without the heading prefix.

    Returns:
        The overlap that starts the next chunk. It can begin mid-word or with a space.
    """
    ids = _ENC.encode(body, disallowed_special=())
    return _ENC.decode(ids[-OVERLAP_TOKENS:]) if len(ids) > OVERLAP_TOKENS else body


def chunk_document(text: str, pages: Sequence[PageSpan]) -> list[Chunk]:
    """ Split a document's Markdown into chunks ready to embed.
    Splits at headings first, then packs paragraphs up to MAX_TOKENS with overlap inside a section.

    Args:
        text: the cleaned Markdown of the whole document.
        pages: where each page sits in `text`, used to give each chunk its pages.

    Returns:
        The chunks in reading order, numbered from 0.
    """

    starts = [p.start for p in pages]
    chunks: list[Chunk] = []
    path: list[str] = []  # heading stack, e.g. ["Card Terms", "Fees"]
    pending: list[_Block] = []  # paragraph pieces waiting to become the next chunk
    prefix, limit, used = "", MAX_TOKENS, 0  # heading text, body budget, tokens in `pending`

    def page_at(offset: int) -> int:
        """ Page number containing a character offset (binary search over page starts)."""
        return pages[max(bisect_right(starts, offset) - 1, 0)].number if pages else 1


    def emit(body: str, start: int, end: int):
        """ Append a chunk: heading prefix + body, current section, pages from the offsets."""
        chunks.append(
            Chunk(len(chunks), prefix + body, " > ".join(path), page_at(start), page_at(max(end - 1, start)))
        )


    def flush(*, overlap: bool):
        """ Turn the waiting paragraph pieces into one chunk, then empty queue or keep the end chunk as start of the next one."""
        nonlocal pending, used
        if not pending:
            return

        body = "\n\n".join(b.text for b in pending) # join the paragraph pieces into one chunk
        emit(body, pending[0].start, pending[-1].end) # emit the chunk
        tail = _tail(body) if overlap else "" # what to carry over to the next chunk if overlap is True
        pending = [_Block("para", tail, pending[-1].start, pending[-1].end)] if tail else [] # reset queue
        used = _count(tail) # reset token counter


    for block in _blocks(text):
        # Heading: close the current chunk and start a new section.
        if block.kind == "heading":
            flush(overlap=False)  # overlap stays inside a section
            level = len(block.text) - len(block.text.lstrip("#"))  # number of `#`
            del path[level - 1 :]  # drop headings at this level and deeper
            path.append(block.text.lstrip("#").strip())  # heading text without the `#`s
            prefix = " > ".join(path) + "\n\n"  # e.g. "Card Terms > Fees", put on every chunk
            limit, used = MAX_TOKENS - _count(prefix), 0  # the prefix takes part of the budget

        # Table: emitted directly, piece by piece.
        elif block.kind == "table":
            flush(overlap=False)  # a table is its own chunk
            for piece in _split_table(block.text, limit):  # one piece unless it's too big
                emit(piece, block.start, block.end)

        # Paragraph: queue its pieces and pack them into chunks up to `limit`.
        else:
            for piece in _split_paragraph(block.text, limit):  # one piece unless it's too big
                n = _count(piece)
                if pending and used + n > limit:  # next piece doesn't fit: close the chunk
                    flush(overlap=True)  # same section continues, so carry the tail over
                pending.append(_Block("para", piece, block.start, block.end))
                used += n

    flush(overlap=False)  # emit whatever is still queued at the end of the document

    return chunks
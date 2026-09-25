"""`KnowledgeBase.ingest` failure rules, with the SQL layer and the parser stubbed out."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.rag.ingestion.parse import PageSpan, ParsedDocument
from app.rag.kb import service
from app.auth.levels import viewer_level
from app.rag.kb.service import ForbiddenError, KnowledgeBase


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True])
async def test_upload_with_no_text_fails_and_keeps_a_live_version(monkeypatch: pytest.MonkeyPatch, live: bool) -> None:
    doc_id = uuid4()
    row = {"id": doc_id, "status": "ready" if live else "processing", "required_level": "employee"}
    calls: list[str] = []

    async def none(*_a, **_k): return None
    async def get_row(*_a, **_k): return row
    async def record(name): calls.append(name)
    async def parse(*_a): return ParsedDocument("   \n", [])

    monkeypatch.setattr(service, "queries", SimpleNamespace(
        tag_names=lambda *_: _async({"policy"}),
        get_by_hash_ready=none,
        get_by_filename=get_row if live else none,
        insert_processing=lambda *_a, **_k: _async(doc_id),
        replace_tags=none,
        get_by_id=get_row,
        replace_chunks=lambda *_a, **_k: record("replace_chunks"),
        mark_failed=lambda *_a, **_k: record("mark_failed"),
    ))
    monkeypatch.setattr(service, "parse", parse)

    result = await KnowledgeBase(pool=None).ingest(filename="blank.pdf", content=b"x", tags=["policy"])  # type: ignore[arg-type]

    assert (result.status, result.error) == ("failed", "No text found in the file.")
    assert calls == ([] if live else ["mark_failed"])  # a live document keeps its chunks and stays `ready`


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_tag_names_keep_non_ascii_letters(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    async def create_tag(_pool, name, _description):
        created.append(name)
        return True
    async def get_tag(_pool, name): return {"name": name, "description": "", "document_count": 0}

    monkeypatch.setattr(service, "queries", SimpleNamespace(create_tag=create_tag, get_tag=get_tag))
    kb = KnowledgeBase(pool=None)  # type: ignore[arg-type]
    for name in ("Qualità", "Über-Policy", "  HR_Rules 2025 ", "規則"):
        await kb.create_tag(name, "")

    assert created == ["qualità", "über-policy", "hr-rules-2025", "規則"]
    with pytest.raises(ValueError):
        await kb.create_tag("--!!--", "")



@pytest.mark.asyncio
async def test_creating_an_existing_tag_is_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "queries", SimpleNamespace(create_tag=lambda *_: _async(False)))

    with pytest.raises(service.TagExistsError, match="'policy' already exists"):
        await KnowledgeBase(pool=None).create_tag("Policy", "new text")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_staged_upload_is_processed_even_if_a_copy_turned_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id = uuid4()
    swapped: list[object] = []

    async def none(*_a, **_k): return None
    async def replace_chunks(_pool, document_id, *_a, **_k): swapped.append(document_id)
    async def parse(*_a): return ParsedDocument("Some text.", [PageSpan(1, 0, 10)])

    monkeypatch.setattr(service, "queries", SimpleNamespace(
        get_by_hash_ready=lambda *_a, **_k: _async({"id": uuid4()}),  # the other copy, now `ready`
        get_by_id=lambda *_: _async({"id": doc_id, "status": "processing"}),
        replace_chunks=replace_chunks,
        ChunkRow=service.queries.ChunkRow,
    ))
    monkeypatch.setattr(service, "parse", parse)
    kb = KnowledgeBase(pool=None)  # type: ignore[arg-type]
    monkeypatch.setattr(kb, "_embedder", SimpleNamespace(embed=lambda texts: _async([[0.0]] * len(texts))))

    result = await kb.ingest(filename="copy.pdf", content=b"x", tags=["policy"], document_id=str(doc_id))

    assert (result.status, swapped) == ("ready", [doc_id])



@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ready", "failed", "processing"])
async def test_an_employee_cannot_replace_a_manager_only_document(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    hash_levels: list[list[str]] = []
    writes: list[str] = []

    async def get_by_hash_ready(_pool, _hash, *, levels):
        hash_levels.append(levels)
        return None  # the SQL hides the manager-only copy from an employee
    async def write(*_a, **_k): writes.append("write")

    monkeypatch.setattr(service, "queries", SimpleNamespace(
        tag_names=lambda *_: _async({"policy"}),
        get_by_hash_ready=get_by_hash_ready,
        get_by_filename=lambda *_: _async({"id": uuid4(), "status": status, "required_level": "manager"}),
        insert_processing=write,
        replace_tags=write,
    ))
    viewer_level.set("employee")

    with pytest.raises(ForbiddenError):
        await KnowledgeBase(pool=None).stage(filename="Salary Bands 2025.pdf", content=b"fake", tags=["policy"])  # type: ignore[arg-type]

    assert (hash_levels, writes) == ([["employee"]], [])

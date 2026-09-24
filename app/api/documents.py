from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, UploadFile, status
from fastapi.responses import JSONResponse

from app.api import KB
from app.auth.levels import Level
from app.rag.kb.models import DocumentRecord


router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def upload(kb: KB, background: BackgroundTasks, file: Annotated[UploadFile, File()], tags: Annotated[list[str], Form()] = [],  
                 required_level: Annotated[Level | None, Form()] = None) -> JSONResponse:
    """ Upload a document and start ingesting it. Ingestion runs in two phases. 

    Args:
        kb: the shared `KnowledgeBase`, injected.
        background: FastAPI's queue of tasks to run once the response is sent.
        file: the uploaded file; its filename identifies the document (same name = Replacement).
        tags: at least one tag from the vocabulary; checked by `kb.stage`.
        required_level: `employee` or `manager`; only applied when a manager uploads.

    Returns:
        200 `{already_present, document_id, filename}` when the exact bytes are already `ready`
        (only the tags and level were updated), otherwise 202 `{document_id, status: "processing"}`;
        the UI then polls `GET /api/documents` until the row turns `ready` or `failed`.
    """
    filename = file.filename or "upload"
    content = await file.read()

    # PHASE 1, inside the request: fast DB work only, and it validates the tags before we answer.
    staged = await kb.stage(filename=filename, content=content, tags=tags, required_level=required_level)

    if staged.already_present:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "already_present": True,
                "document_id": staged.document_id,
                "filename": staged.name,
            }
        )

    # PHASE 2, after the response: `ingest` calls `stage` again (it finds the row just inserted),
    # then parses, chunks and embeds, and marks the row `ready` or `failed` with the error.
    background.add_task(kb.ingest, filename=filename, content=content, tags=tags, required_level=required_level)

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "document_id": staged.document_id, 
            "status": "processing"
        }
    )


@router.get("")
async def list_documents(kb: KB) -> list[DocumentRecord]:
    """ List the documents the viewer may read, newest first, in every status.

    Args:
        kb: the shared `KnowledgeBase`, injected.

    Returns:
        One `DocumentRecord` per document: name, tags, status, level, counts and any ingest error.
    """
    return (await kb.list_documents(ready_only=False, limit=500)).documents


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(kb: KB, document_id: str) -> None:
    """ Delete a document together with its chunks and tag links.

    Args:
        kb: the shared `KnowledgeBase`, injected.
        document_id: the document's id (a filename also works; `KnowledgeBase` resolves either).
    """
    await kb.delete_document(document_id)

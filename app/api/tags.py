from fastapi import APIRouter, status
from pydantic import BaseModel

from app.api import KB
from app.rag.kb.models import TagInfo


router = APIRouter(prefix="/api/tags", tags=["tags"])

# Request body of `POST /api/tags`.
class NewTag(BaseModel):
    name: str
    description: str

# Request body of `PATCH /api/tags/{name}` new description.
class Description(BaseModel):
    description: str


@router.get("")
async def list_tags(kb: KB) -> list[TagInfo]:
    """ List the whole vocabulary with a document count per tag, sorted by name.

    Args:
        kb: the shared `KnowledgeBase`, injected.

    Returns:
        One `TagInfo` per tag: name, description, number of documents carrying it.
    """
    return await kb.list_tags(only_in_use=False)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_tag(kb: KB, body: NewTag) -> TagInfo:
    """ Add a tag to the vocabulary.

    Args:
        kb: the shared `KnowledgeBase`, injected.
        body: the tag's name and description.

    Returns:
        The stored tag, under its normalised name; 409 if a tag with that name already exists.
    """
    return await kb.create_tag(body.name, body.description)


@router.patch("/{name}")
async def update_tag(kb: KB, name: str, body: Description) -> TagInfo:
    """ Change a tag's description.

    Args:
        kb: the shared `KnowledgeBase`, injected.
        name: the tag to edit, as returned by `list_tags`.
        body: the new description.

    Returns:
        The updated tag.
    """
    return await kb.update_tag_description(name, body.description)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(kb: KB, name: str) -> None:
    """ Remove a tag from the vocabulary, only if no document carries it.

    Args:
        kb: the shared `KnowledgeBase`, injected.
        name: the tag to delete.
    """
    await kb.delete_tag(name)

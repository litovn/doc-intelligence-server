from typing import Annotated
from fastapi import Depends, Request

from app.rag.kb.service import KnowledgeBase


def _knowledge_base(request: Request) -> KnowledgeBase:
    """ FastAPI dependency that returns the shared `KnowledgeBase`."""
    kb: KnowledgeBase = request.app.state.kb
    return kb


# Type alias for route parameters: `kb: KB` makes FastAPI inject the shared `KnowledgeBase`,
# without repeating `Annotated[KnowledgeBase, Depends(...)]` in every signature.
KB = Annotated[KnowledgeBase, Depends(_knowledge_base)]

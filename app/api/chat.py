import json
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated, Literal

from agent_framework.openai import OpenAIChatCompletionClient
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent import answer, chat_client


router = APIRouter(prefix="/api/chat", tags=["chat"])

# One turn of the conversation, either a question or an earlier answer.
class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


# Whole conversation.
class ChatRequest(BaseModel):
    messages: list[Turn]


@lru_cache
def _chat_client() -> OpenAIChatCompletionClient:
    """ FastAPI dependency that returns the chat model client.

    Returns:
        The shared `OpenAIChatCompletionClient` from `app.agent.chat_client`.
    """
    return chat_client()


def _reason(exc: BaseException) -> str:
    """ Turn an exception into a message the UI can show.

    Args:
        exc: the exception the agent run raised, possibly an `ExceptionGroup` of groups.

    Returns:
        The inner messages joined with "; ", or the exception's class name when it has no message.
    """
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_reason(inner) for inner in exc.exceptions)
    
    return str(exc) or exc.__class__.__name__


@router.post("")
async def chat(body: ChatRequest, client: Annotated[OpenAIChatCompletionClient, Depends(_chat_client)]) -> StreamingResponse:
    """ Answer the last question in `body`, streaming the agent's progress as server-sent event.

    Args:
        body: the conversation, ending with the new question.
        client: the shared chat model client, injected.

    Returns:
        A `text/event-stream` response; each frame is `event: <name>` + `data: <json>`.
        A failure mid-run is sent as an `error` event, since the 200 status has already gone out.
    """

    async def events() -> AsyncIterator[str]:
        """ Run the agent and format each `(name, data)` event it yields as one server-sent event frame."""
        try:
            async for name, data in answer([t.model_dump() for t in body.messages], client=client):
                yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"event: error\ndata: {json.dumps({'message': _reason(exc)})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

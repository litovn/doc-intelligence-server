import inspect
from collections.abc import AsyncIterator, Sequence
from typing import Any

from agent_framework import Agent, FunctionTool, Message
from agent_framework.openai import OpenAIChatCompletionClient
from mcp.client import Client
from mcp.types import Tool
from openai import AsyncOpenAI

from app.config import settings
from app.mcp_server.tools import INSTRUCTIONS, mcp

MAX_TOOL_CALLS = 6

ANSWERING_RULES = inspect.cleandoc(
    """
    You answer employees' questions about the content in documents in this knowledge base, in a chat.

    Answering:
    - When documents disagree, report both and say which document says what.
    - Use only the text in the hits. Never add a fact from your own knowledge, never fill a gap with a plausible guess, never add facts the documents don't show.
    - If the hits cover only part of the question, answer that part and say which part isn't in the knowledge base.
    - If nothing answers the question, say "not found in the knowledge base" (in the same asked language) and stop.
    - Quote moderately: a short phrase where the exact wording matters, use your own words otherwise.
    - If a request isn't about the documents: say you can only answer from the knowledge base.

    Format:
    - Lead with the answer, then add only the detail the question needs. A few sentences is enough. Do not over-explain, do not give a long summary of the whole document.
    - Plain text: the chat shows your reply as-is, so no Markdown (no **, #, tables, links). Line breaks and "- " lists are fine.
    - Never write document names, page numbers or headings as references, e.g. no "(onboarding-guide.pdf, p. 10)". Don't mention tools, chunk ids or scores. The user sees the sources separately.
    - Answer in the language the question was asked in.
    """
)
SYSTEM_PROMPT = f"{INSTRUCTIONS}\n\n{ANSWERING_RULES}"

Json = dict[str, Any]
Event = tuple[str, Any]


def chat_client() -> OpenAIChatCompletionClient:
    """ The chat model the agent talks to."""
    return OpenAIChatCompletionClient(
        settings.chat_model,
        async_client=AsyncOpenAI(
            base_url=settings.openai_base_url or None,
            api_key=settings.openai_api_key,
            max_retries=4,
        ),
        function_invocation_configuration={
            "max_function_calls": MAX_TOOL_CALLS,
            "allow_concurrent_invocation": False,
            "include_detailed_errors": True
        }
    )


def _hit_count(structured: Json) -> int:
    """ Show the number of hits a tool returned, for the model to decide whether to keep searching."""
    for key in ("result_count", "total"):
        if isinstance(structured.get(key), int):
            return int(structured[key])
        
    return sum(len(value) for value in structured.values() if isinstance(value, list))


def _function(session: Client, tool: Tool, events: list[Event], sources: dict[str, Json]) -> FunctionTool:
    """ Wrap one MCP tool as an Agent Framework function the model can call.

    Args:
        session: the open in-memory MCP client the call is forwarded to.
        tool: the MCP tool definition; its name, description and JSON Schema pass through untouched.
        events: shared queue the call appends its `tool_call` and `tool_result` events to
        sources: shared map of `chunk_id` to citation fields, filled from every search hit.

    Returns:
        `FunctionTool` whose result is the tool's JSON on success, `ToolError` otherwise.
    """

    async def call(**arguments: Any):
        """ Forward one model tool call to the MCP server.

        Args:
            **arguments: the model's arguments, already checked against the tool's JSON Schema.
        """
        events.append(("tool_call", {"name": tool.name, "arguments": arguments}))
        result = await session.call_tool(tool.name, arguments)

        # What the model is shown: the tool's JSON on success, the `ToolError` sentence on failure.
        content = "\n".join(block.text for block in result.content if block.type == "text")

        if result.is_error:
            events.append(("tool_result", {"error": content}))
            return content
        
        structured = result.structured_content or {}
        for hit in structured.get("results") or []:
            sources[hit["chunk_id"]] = {field: hit.get(field) for field in ("document_name", "page_start", "page_end", "heading", "text")}
        events.append(("tool_result", {"hit_count": _hit_count(structured)}))

        return content

    return FunctionTool(name=tool.name, description=tool.description or "", func=call, input_model=tool.input_schema)


async def answer(messages: Sequence[Json], *, client: OpenAIChatCompletionClient | None = None) -> AsyncIterator[Event]:
    """ Run one question to an answer, streaming the chat events as they happen.

    Args:
        messages: the whole browser-held history.
        client: the chat client to use; a new `chat_client()` when `None`.
    """
    events: list[Event] = []
    sources: dict[str, Json] = {}

    async with Client(mcp) as session:
        tools = [_function(session, tool, events, sources) for tool in (await session.list_tools()).tools]
        agent = Agent(client or chat_client(), SYSTEM_PROMPT, tools=tools)
        history = [Message(m["role"], [m["content"]]) for m in messages]

        # Tools run inside stream, between updates, so events are already queued by the time the next update arrives
        async for update in agent.run(history, stream=True):
            if update.text:
                events.append(("token", {"text": update.text}))
            while events:
                yield events.pop(0)
                
        while events:
            yield events.pop(0)

    yield "sources", list(sources.values())
    yield "done", {}

"""Give every question in `eval/golden.jsonl` the `query` the in-app agent would search it with.

The agent doesn't search with the user's words: the `query` parameter of the search tools asks for "a short phrase" in
the words a document would use, so "What fee does Contoso charge for an outgoing domestic wire transfer?" is searched
as "outgoing domestic wire transfer fee". `python -m eval.retrieval --agent-queries` searches with these phrases.
Each one is written once by `CHAT_MODEL`, from the agent's own system prompt and tool definitions with `search`
forced, and then kept, so later runs stay comparable. Only questions without a `query` are sent.

    python -m eval.draft_queries
"""

import asyncio
import json

from mcp.client import Client
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageFunctionToolCall,
)

from app.agent import SYSTEM_PROMPT
from app.config import settings
from app.mcp_server.tools import mcp
from eval.retrieval import GOLDEN


async def main() -> None:
    golden = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]
    todo = [q for q in golden if "query" not in q]

    async with Client(mcp) as session:
        tools: list[ChatCompletionFunctionToolParam] = [
            {"type": "function",
             "function": {"name": t.name, "description": t.description or "", "parameters": t.input_schema}}
            for t in (await session.list_tools()).tools
        ]
    client = AsyncOpenAI(api_key=settings.openai_api_key, max_retries=6)
    gate = asyncio.Semaphore(16)

    async def draft(q: dict) -> None:
        async with gate:
            response = await client.chat.completions.create(
                model=settings.chat_model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": q["question"]}],
                tools=tools,
                tool_choice={"type": "function", "function": {"name": "search"}},
                parallel_tool_calls=False,
            )
        [call] = response.choices[0].message.tool_calls or []
        assert isinstance(call, ChatCompletionMessageFunctionToolCall)  # `search` is forced
        q["query"] = json.loads(call.function.arguments)["query"]

    await asyncio.gather(*(draft(q) for q in todo))
    GOLDEN.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in golden), encoding="utf-8")
    print(f"{len(todo)} queries drafted, {len(golden) - len(todo)} kept")


if __name__ == "__main__":
    asyncio.run(main())

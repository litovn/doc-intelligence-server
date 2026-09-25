"""The MCP surface an agent meets: tool definitions, argument strictness, result shape, and the key guard.

Needs no Postgres or model keys: the tool-selection eval's in-memory corpus stands in for the SQL layer.
Run with `python -m pytest tests`.
"""

import json
from typing import Any

import httpx
import pytest
from mcp.client import Client
from mcp.types import CallToolResult
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.auth.levels import viewer_level
from app.mcp_server.auth import BearerAuthMiddleware
from app.mcp_server.tools import mcp
from eval.tool_selection import install

install()

TOOLS = {
    "list_documents", "list_tags", "search", "search_by_tag", "search_by_document",
    "get_document_outline", "get_chunk_context",
}


def _text(result: CallToolResult) -> str:
    return "".join(block.text for block in result.content if block.type == "text")


def _without_nulls(value: Any) -> Any:
    """ Drop null fields at every depth, as the compact text rendering does."""
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


# --- Tools ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_definitions_are_written_for_the_model() -> None:
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    assert set(tools) == TOOLS
    for name, tool in tools.items():
        description = tool.description or ""
        assert "Args:" not in description  # parameters are described once, in the schema
        # Tool search can load one tool without its siblings, so each description points to the others itself.
        assert any(f"`{other}`" in description for other in TOOLS - {name})
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema is not None


@pytest.mark.asyncio
async def test_unknown_argument_is_an_error_naming_the_valid_ones() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("list_documents", {"tags": ["hr"]})

    assert result.is_error
    assert "Unknown argument 'tags'. Valid arguments: tag, name, limit." in _text(result)


@pytest.mark.asyncio
async def test_results_are_indented_and_match_structured_content() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("search", {"query": "KYC records retention"})

    text = _text(result)
    assert text.startswith("{\n  ")  # indented: the eval measured the model missing cut-off passages in one-line JSON
    assert json.loads(text) == _without_nulls(result.structured_content)

    hit = (result.structured_content or {})["results"][0]
    assert "document_id" not in hit
    assert not hit["text"].startswith(hit["heading"])  # the heading travels once, in `heading`


@pytest.mark.asyncio
async def test_list_documents_filters_by_name_and_says_when_cut_off() -> None:
    async with Client(mcp) as client:
        named = await client.call_tool("list_documents", {"name": "personal loan"})
        cut = await client.call_tool("list_documents", {"limit": 3})

    assert [d["name"] for d in (named.structured_content or {})["documents"]] == ["contoso-personal-loan-product-manual.docx"]
    assert (cut.structured_content or {})["hint"].startswith("Showing 3 of 17.")


# --- Auth ----------------------------------------------------------------------------------------

async def _whoami(scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse({"level": viewer_level.get()})(scope, receive, send)


def _http(rate_limit: int) -> httpx.AsyncClient:
    app = BearerAuthMiddleware(_whoami, keys={"employee": "old-key, new-key", "manager": "boss-key"}, rate_limit=rate_limit)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kb")


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.asyncio
async def test_keys_set_the_level_and_failures_follow_rfc_6750() -> None:
    async with _http(rate_limit=100) as http:
        missing = await http.post("/")
        wrong = await http.post("/", headers=_bearer("nope"))
        rotated = [await http.post("/", headers=_bearer(key)) for key in ("old-key", "new-key")]
        manager = await http.post("/", headers={"Authorization": "bearer boss-key"})

    assert missing.status_code == 401 and missing.headers["www-authenticate"] == 'Bearer realm="kb"'
    assert wrong.status_code == 401 and 'error="invalid_token"' in wrong.headers["www-authenticate"]
    assert [r.json() for r in rotated] == [{"level": "employee"}] * 2
    assert manager.json() == {"level": "manager"}


@pytest.mark.asyncio
async def test_each_key_has_its_own_rate_limit() -> None:
    async with _http(rate_limit=2) as http:
        burst = [await http.post("/", headers=_bearer("old-key")) for _ in range(3)]
        other = await http.post("/", headers=_bearer("boss-key"))

    assert [r.status_code for r in burst] == [200, 200, 429]
    assert int(burst[-1].headers["retry-after"]) > 0
    assert other.status_code == 200

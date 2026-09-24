from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api import auth, chat, documents, tags
from app.auth.session_guard import require_session
from app.config import settings
from app.mcp_server.auth import BearerAuthMiddleware
from app.mcp_server.tools import mcp, set_knowledge_base
from app.rag.db import init_db
from app.rag.kb.service import ForbiddenError, KnowledgeBase, UnknownDocumentError
from app.rag.queries import TagInUseError


TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[settings.public_host, "localhost:*", "127.0.0.1:*"],
    allowed_origins=[
        f"https://{settings.public_host}",
        "http://localhost:*",
        "http://127.0.0.1:*",
    ],
)

mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    json_response=True,
    stateless_http=True,
    transport_security=TRANSPORT_SECURITY,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the shared `KnowledgeBase` before serving and close the DB pool on shutdown."""

    pool = await init_db() # Creates the schema

    # One instance for both sides: 
    # MCP tools get it here...
    kb = KnowledgeBase(pool)
    set_knowledge_base(kb)

    # ...and REST resolves it from app.state (`app/api/__init__.py`)
    app.state.kb = kb
    app.state.pool = pool

    try:
        async with mcp.session_manager.run():
            yield
    finally:
        await pool.close()


MCP_PATH = "/mcp"


def exact_mcp_path(app: ASGIApp) -> ASGIApp:
    """Let the bare `/mcp` reach the mount instead of being redirected to `/mcp/`."""

    async def normalize(scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and scope["path"] == MCP_PATH:
            scope = {**scope, "path": MCP_PATH + "/"}
        await app(scope, receive, send)

    return normalize


# --- App -------------------------------------------------------------------------

app = FastAPI(title="indigo-kb", lifespan=lifespan)
app.add_middleware(exact_mcp_path)
app.mount(
    MCP_PATH,
    BearerAuthMiddleware(
        mcp_app,
        keys={"employee": settings.mcp_api_key_employee, "manager": settings.mcp_api_key_manager},
    ),
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Add login and logout endpoints.
app.include_router(auth.router)
for gated in (documents.router, tags.router, chat.router):
    app.include_router(gated, dependencies=[Depends(require_session)])


# `KnowledgeBase` phrases its failures as sentences. 
# These handlers turn errors from the knowledge base into proper HTTP responses.
@app.exception_handler(UnknownDocumentError)
async def unknown_document(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})

@app.exception_handler(ForbiddenError)
async def forbidden(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": str(exc)})

@app.exception_handler(ValueError)
async def bad_request(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})

@app.exception_handler(TagInUseError)
async def tag_in_use(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, TagInUseError)
    return JSONResponse(
        status_code=409,
        content={"detail": {"message": str(exc), "tag": exc.tag, "documents": exc.documents}},
    )


# The Next.js static export, served by the same process on the same port: one image, one URL, no CORS.
UI_DIR = Path(__file__).resolve().parents[1] / "frontend" / "out"
if UI_DIR.is_dir():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")

import hmac
from collections.abc import Mapping

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.levels import LEVELS, Level, viewer_level

# Sent with every 401, as HTTP requires: tells the client this endpoint wants a bearer token.
CHALLENGE = {"WWW-Authenticate": 'Bearer realm="kb"'}


# ASGI middleware that guards the MCP server with one API key per access level
class BearerAuthMiddleware:

    def __init__(self, app: ASGIApp, *, keys: Mapping[Level, str]):
        """ Wrap the MCP app and remember the keys.

        Args:
            app: the MCP streamable-HTTP app being protected.
            keys: the API key for each level, e.g. `{"employee": "...", "manager": "..."}`.
                An empty or missing key disables that level: no token can match it.
        """
        self._app = app
        self._keys = [(level, keys[level].encode()) for level in LEVELS if keys.get(level)]


    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        """ Check the bearer token, then run the MCP request as that token's level.

        Args:
            scope: the ASGI connection info (type, path, headers, ...).
            receive: ASGI callable that reads the request body.
            send: ASGI callable that writes the response.
        """
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # "Bearer abc123" -> scheme "Bearer", token "abc123". A missing header gives two empty strings.
        scheme, _, token = Headers(scope=scope).get("authorization", "").partition(" ")
        level = self._level_for(token) if scheme.lower() == "bearer" else None

        if level is None:
            unauthorized = JSONResponse(
                {"error": "Missing or invalid bearer token."},
                status_code=401,
                headers=CHALLENGE
            )
            await unauthorized(scope, receive, send)
            return

        # Stateless HTTP runs each request's MCP server inside this call, so the tools see it.
        reset = viewer_level.set(level)
        try:
            await self._app(scope, receive, send)
        finally:
            viewer_level.reset(reset)


    def _level_for(self, token: str) -> Level | None:
        """ Find which level a bearer token belongs to.

        Args:
            token: the token from the `Authorization` header, as the client sent it.

        Returns:
            `employee` or `manager` for a matching key, None when no key matches.
        """
        candidate = token.encode()
        for level, key in self._keys:
            if hmac.compare_digest(candidate, key):
                return level
        return None

import hmac
import time
from collections.abc import Mapping

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.levels import LEVELS, Level, viewer_level



# ASGI middleware that guards the MCP server with API keys, one set per access level
class BearerAuthMiddleware:

    def __init__(self, app: ASGIApp, *, keys: Mapping[Level, str], rate_limit: int):
        """ Wrap the MCP app and remember the keys.

        Args:
            app: the MCP streamable-HTTP app being protected.
            keys: the API keys for each level, e.g. `{"employee": "...", "manager": "..."}`.
                An empty or missing value disables that level: no token can match it.
            rate_limit: requests a minute each key may make; the next one gets `429`.
        """
        self._app = app
        self._keys = [
            (level, key.strip().encode()) for level in LEVELS for key in (keys.get(level) or "").split(",") if key.strip()
        ]
        self._rate_limit = rate_limit
        self._usage: dict[bytes, tuple[int, int]] = {}


    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        """ Check the bearer token and its rate limit, then run the MCP request as that token's level.

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
        if scheme.lower() != "bearer" or not token.strip():
            await self._reject(401, "Missing bearer token. Send `Authorization: Bearer <key>`.", 'Bearer realm="kb"')(scope, receive, send)
            return

        match = self._match(token.strip())
        if match is None:
            await self._reject(401, "Invalid bearer token.", 'Bearer realm="kb", error="invalid_token", error_description="The bearer token is not a valid key."')(scope, receive, send)
            return

        level, key = match
        if (wait := self._seconds_until_allowed(key)) is not None:
            response = self._reject(429, f"Rate limit reached: {self._rate_limit} requests a minute per key.", None)
            response.headers["Retry-After"] = str(wait)
            await response(scope, receive, send)
            return

        # Stateless HTTP runs each request's MCP server inside this call, so the tools see it.
        reset = viewer_level.set(level)
        try:
            await self._app(scope, receive, send)
        finally:
            viewer_level.reset(reset)


    def _match(self, token: str) -> tuple[Level, bytes] | None:
        """ Find which level a bearer token belongs to, comparing in constant time.

        Args:
            token: the token from the `Authorization` header, as the client sent it.

        Returns:
            The level (`employee` or `manager`) and the key that matched, or None when no key matches.
        """
        candidate = token.encode()
        for level, key in self._keys:
            if hmac.compare_digest(candidate, key):
                return level, key
        return None


    def _seconds_until_allowed(self, key: bytes) -> int | None:
        """ Count one request against its key's budget for the current minute.

        Args:
            key: the API key the request authenticated with.

        Returns:
            None while the key is within its limit; otherwise the seconds until the next minute starts.
        """
        now = time.monotonic()
        minute = int(now // 60)
        start, count = self._usage.get(key, (minute, 0))
        count = count + 1 if start == minute else 1
        self._usage[key] = (minute, count)
        return int(60 - now % 60) + 1 if count > self._rate_limit else None


    @staticmethod
    def _reject(status: int, detail: str, challenge: str | None) -> JSONResponse:
        """ An error response in the REST API's `{"detail": ...}` shape, with the challenge a 401 requires."""
        headers = {"WWW-Authenticate": challenge} if challenge else None
        return JSONResponse({"detail": detail}, status_code=status, headers=headers)

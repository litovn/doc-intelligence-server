from typing import Annotated

import asyncpg
from fastapi import Depends, HTTPException, Request, status

from app.auth.levels import viewer_level
from app.auth.sessions import User, get_user_for_token

COOKIE = "kb_session"


def pool_of(request: Request) -> asyncpg.Pool:
    """ FastAPI dependency that returns the asyncpg pool from the current request."""
    pool: asyncpg.Pool = request.app.state.pool
    return pool


async def require_session(request: Request) -> User:
    """ FastAPI dependency that gates the REST API on a logged-in session.
    Reads the `kb_session` cookie, looks the token up in the sessions table, raises 401 if missing 
    or expired, and otherwise sets `viewer_level` to the user's level and returns the `User`.
    """
    token = request.cookies.get(COOKIE)
    user = await get_user_for_token(pool_of(request), token) if token else None
    
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in.")
    viewer_level.set(user.level)
    
    return user


CurrentUser = Annotated[User, Depends(require_session)]

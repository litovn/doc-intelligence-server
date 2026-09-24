from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.auth.levels import Level
from app.auth.session_guard import COOKIE, CurrentUser, pool_of
from app.auth.sessions import SESSION_TTL, authenticate, create_session, delete_session


router = APIRouter(prefix="/api/auth", tags=["auth"])

# Request body of `POST /api/auth/login`
class Credentials(BaseModel):
    username: str
    password: str

# The logged-in user as the UI sees them
class Me(BaseModel):
    username: str
    level: Level


@router.post("/login")
async def login(body: Credentials, request: Request, response: Response) -> Me:
    """ Check the credentials and start a session.
    On success a new session token is stored in the `sessions` table and sent back as the
    `kb_session` cookie, valid for `SESSION_TTL` (24 h).

    Args:
        body: the username and password from the login form.
        request: used to reach the DB pool and to tell http from https.
        response: the response the session cookie is set on.

    Returns:
        The logged-in user's name and level.
    """
    pool = pool_of(request)
    user = await authenticate(pool, body.username, body.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password.")

    response.set_cookie(
        COOKIE,
        await create_session(pool, user.id),
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )

    return Me(username=user.username, level=user.level)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response):
    """ End the current session, if there is one.
    Deletes the session row, tell the browser to drop the cookie. 

    Args:
        request: used to read the session cookie and reach the DB pool.
        response: the response the cookie is cleared on.
    """
    if token := request.cookies.get(COOKIE):
        await delete_session(pool_of(request), token)

    response.delete_cookie(COOKIE)


@router.get("/me")
async def me(user: CurrentUser) -> Me:
    """ Return who is logged in. 

    Args:
        user: resolved by `require_session` from the session cookie; a missing or expired session
            is a 401 before this body runs.

    Returns:
        The logged-in user's name and level.
    """
    return Me(username=user.username, level=user.level)

import hashlib
import hmac
import logging
import secrets
from datetime import timedelta
from typing import NamedTuple
from uuid import UUID

import asyncpg

from app.auth.levels import Level

SESSION_TTL = timedelta(hours=24)

# An authenticated user, as the rest of the app sees them.
class User(NamedTuple):
    id: UUID
    username: str
    level: Level

log = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    """ Hash a plaintext password for storage in `users.password_hash`."""

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)
    password_hash = f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"

    return password_hash


def verify_password(password: str, stored: str) -> bool:
    """ Check a plaintext password against a hash produced by `hash_password`."""
    try:
        _, iterations, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
    except ValueError:
        return False
    
    return hmac.compare_digest(digest, bytes.fromhex(expected))


async def authenticate(pool: asyncpg.Pool, username: str, password: str) -> User | None:
    """ Check credentials, a username and password and return the matching `User`, or None."""

    row = await pool.fetchrow(
        "SELECT id, username, level, password_hash FROM users WHERE username = $1", username
    )

    matches = verify_password(password, row["password_hash"] if row else hash_password(secrets.token_urlsafe()))
    
    if row is None or not matches:
        return None
    
    return User(row["id"], row["username"], row["level"])


async def create_session(pool: asyncpg.Pool, user_id: UUID) -> str:
    """ Start a new session for `user_id` and return its token.
    Also deletes every expired session, so the table doesn't grow without a scheduled cleanup job.
    """
    token = secrets.token_urlsafe(32)

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM sessions WHERE expires_at < now()"
        )
        await conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES ($1, $2, now() + $3)",
            token,
            user_id,
            SESSION_TTL
        )

    return token


async def get_user_for_token(pool: asyncpg.Pool, token: str) -> User | None:
    """ Return the `User` a session token belongs to, or None when it's unknown or expired (401)."""
    
    row = await pool.fetchrow(
        "SELECT u.id, u.username, u.level FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = $1 AND s.expires_at > now()",
        token
    )

    return User(row["id"], row["username"], row["level"]) if row else None


async def delete_session(pool: asyncpg.Pool, token: str):
    """ End a session by deleting its row. """
    await pool.execute("DELETE FROM sessions WHERE token = $1", token)


async def seed_demo_users(pool: asyncpg.Pool):
    """ Create the two demo accounts, `employee`/`demo` and `manager`/`demo`, on an empty database.
    Runs only when the `users` table is empty, called once at startup.
    """
    if await pool.fetchval("SELECT count(*) FROM users"):
        return
    
    demo: list[tuple[str, Level, str]] = [
        ("employee", "employee", "demo"),
        ("manager", "manager", "demo"),
    ]

    await pool.executemany(
        "INSERT INTO users (username, password_hash, level) VALUES ($1, $2, $3) "
        "ON CONFLICT (username) DO NOTHING",
        [(name, hash_password(password), level) for name, level, password in demo],
    )
    
    log.warning("Seeded demo users employee/demo and manager/demo.")

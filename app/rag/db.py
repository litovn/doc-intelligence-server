import asyncpg
from pgvector.asyncpg import register_vector

from app.auth.sessions import seed_demo_users
from app.config import settings

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  filename       TEXT NOT NULL UNIQUE,
  content_hash   TEXT NOT NULL,
  uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  page_count     INTEGER,
  chunk_count    INTEGER,
  status         TEXT NOT NULL CHECK (status IN ('processing','ready','failed')),
  required_level TEXT NOT NULL DEFAULT 'employee' CHECK (required_level IN ('employee','manager')),
  error          TEXT
);

CREATE TABLE IF NOT EXISTS users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  level         TEXT NOT NULL CHECK (level IN ('employee','manager'))
);

CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    UUID REFERENCES users(id) ON DELETE CASCADE,
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
  name        TEXT PRIMARY KEY,
  description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_tags (
  document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
  tag         TEXT REFERENCES tags(name),
  PRIMARY KEY (document_id, tag)
);

CREATE TABLE IF NOT EXISTS chunks (
  id          TEXT PRIMARY KEY,
  document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  text        TEXT NOT NULL,
  embedding   VECTOR(1536) NOT NULL,
  page_start  INTEGER,
  page_end    INTEGER,
  section     TEXT,
  tags        TEXT[] NOT NULL CHECK (cardinality(tags) >= 1),
  tsv         TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED
);

CREATE TABLE IF NOT EXISTS metadata (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin        ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_tags_gin       ON chunks USING gin (tags);
CREATE INDEX IF NOT EXISTS chunks_document_order ON chunks (document_id, chunk_index);
"""

SEED_TAGS: dict[str, str] = {
    "compliance": "Regulatory policies, AML/KYC, data retention, audit and control procedures.",
    "onboarding": "Joining the company or opening an account: setup steps, first-week guides, checklists.",
    "product": "Product terms, manuals, fee and rate schedules, feature descriptions.",
    "hr": "People policies: leave, benefits, conduct, expenses, performance.",
    "faq": "Question-and-answer material aimed at customers or staff.",
}


async def init_db(dsn: str | None = None) -> asyncpg.Pool:
    """ Run once at startup, before anything is served; any failure stops the app."""

    dsn = dsn or settings.database_url 
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(SCHEMA)
        await conn.executemany(
            "INSERT INTO tags (name, description) VALUES ($1, $2) ON CONFLICT (name) DO NOTHING",
            SEED_TAGS.items(),
        )
    finally:
        await conn.close()

    pool = await asyncpg.create_pool(dsn, init=register_vector)
    assert pool is not None 

    await ensure_embedding_model(pool, settings.embedding_model)
    await reconcile_interrupted(pool)
    await seed_demo_users(pool)

    return pool


async def ensure_embedding_model(pool: asyncpg.Pool, model: str):
    """ Record the embedding model on first use; refuse to start if it later changes."""

    stored: str = await pool.fetchval(
        "INSERT INTO metadata (key, value) VALUES ('EMBEDDING_MODEL', $1) "
        "ON CONFLICT (key) DO UPDATE SET value = metadata.value RETURNING value",
        model,
    )

    if stored != model:
        raise RuntimeError(
            f"Database was built with EMBEDDING_MODEL={stored!r} but the app is configured for "
            f"{model!r}. Re-ingest into an empty database or restore the previous model."
        )


async def reconcile_interrupted(pool: asyncpg.Pool) -> int:
    """ Fail rows stuck in `processing` by a crash between ingest phase 1 and phase 2."""

    rows = await pool.fetch(
        "UPDATE documents SET status = 'failed', error = 'interrupted' "
        "WHERE status = 'processing' RETURNING id"
    )

    return len(rows)

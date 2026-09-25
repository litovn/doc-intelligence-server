"""Retrieval eval: which chunk size, overlap and search mode put the answer in the top results?

Every file in `files/` is parsed once (Document Intelligence results are cached in `eval/.cache/`). Then, for each
chunk size and overlap, the real chunker cuts the corpus, the real embedder embeds it (vectors are cached too), the
real `replace_chunks` stores it, and every question in `eval/golden.jsonl` goes through the real `queries.search`,
dense or hybrid as `HYBRID_SEARCH` says. All of it happens in a scratch `kb_eval_<model>` database next to yours, which
is never touched; each `EMBEDDING_MODEL` gets its own, so vectors of two models never mix.

A question is answered when a hit contains its evidence: a short span copied from the source text. The gold is the
text, not a chunk id, so every chunking is scored against the same target. `intact` is the share of evidence spans
left whole in at least one chunk: what the cutting alone preserves, before any ranking. Questions without evidence are
negatives the corpus cannot answer, near misses or off-topic; they show what the relevance floor still rejects.

`--agent-queries` searches with each question's `query` instead: the short phrase the in-app agent sends to `search`,
written by `python -m eval.draft_queries`. Dense and hybrid search rank such phrases very differently from questions.

    python -m eval.retrieval
    python -m eval.retrieval --sizes 200 300 400 --overlaps 0 0.2
    HYBRID_SEARCH=false python -m eval.retrieval --sizes 500 --overlaps 0.2 --agent-queries
    EMBEDDING_MODEL=text-embedding-3-large python -m eval.retrieval --sizes 500 --overlaps 0.2 --dimensions 1536
"""

import argparse
import asyncio
import hashlib
import json
import math
import re
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import urlsplit

import asyncpg

from app.auth.levels import LEVELS
from app.config import settings
from app.rag import queries
from app.rag.db import init_db
from app.rag.ingestion import chunk
from app.rag.ingestion.clean import clean
from app.rag.ingestion.embed import BATCH_SIZE, OpenAIEmbedder
from app.rag.ingestion.parse import PageSpan, ParsedDocument, parse
from app.rag.kb.service import above_floor

FILES = Path("files")
CACHE = Path("eval/.cache")
GOLDEN = Path("eval/golden.jsonl")
TOP_K = 5  # hits the MCP search tools return by default
DEPTH = 10  # hits fetched per question, for MRR and the token budget
BUDGET = 1000  # tokens of hits read: about what TOP_K hits of 300-token chunks cost
PASSAGE = 25  # evidence words from which an answer is a passage (several sentences or steps), not a single fact
FLOORS = (0.20, 0.25, 0.30, 0.35, 0.40, 0.45)  # relevance floors compared at the current chunking

_TYPOGRAPHY = str.maketrans("‘’“”–—\u00a0", "''\"\"-- ")


def norm(text: str) -> str:
    """ Text reduced to what an evidence match should compare: no HTML tags, Markdown escapes or bold, one case, one space."""

    text = re.sub(r"<[^>]+>", " ", text)  # table cells and figures become spaces
    text = re.sub(r"\\(?=[^\w\s])|\*", "", text)  # `1\.` -> `1.`, `**bold**` -> `bold`
    return " ".join(text.translate(_TYPOGRAPHY).lower().split())


# --- Corpus ------------------------------------------------------------------------------------


async def _parsed(path: Path, gate: asyncio.Semaphore) -> ParsedDocument | None:
    """ The cleaned parse of one file, from the cache or Document Intelligence. None if it can't be parsed."""

    content = path.read_bytes()
    cached = CACHE / f"{hashlib.sha256(content).hexdigest()[:24]}.json"

    if cached.exists():
        data = json.loads(cached.read_text(encoding="utf-8"))
        return clean(ParsedDocument(data["markdown"], [PageSpan(*p) for p in data["pages"]]))

    try:
        async with gate:
            parsed = await parse(path.name, content)
    except Exception as exc:  # noqa: BLE001 - a file that can't be parsed is reported and left out, as ingestion would fail it
        print(f"  skipped {path.name}: {type(exc).__name__}: {str(exc).splitlines()[0][:100]}")
        return None

    cached.write_text(json.dumps({"markdown": parsed.markdown, "pages": parsed.pages}), encoding="utf-8")
    return clean(parsed)


async def load_corpus() -> dict[str, ParsedDocument]:
    """ Every file in `files/` that parses, by filename."""

    CACHE.mkdir(parents=True, exist_ok=True)
    paths = sorted(p for p in FILES.iterdir() if p.is_file())
    gate = asyncio.Semaphore(4)
    docs = await asyncio.gather(*(_parsed(p, gate) for p in paths))
    return {p.name: d for p, d in zip(paths, docs, strict=True) if d is not None}


def _family(name: str, doc: ParsedDocument) -> str:
    """ The kind of upload a document stands for: a data table, or prose by length."""

    if Path(name).suffix.lower() in {".csv", ".json", ".xlsx"}:
        return "data tables"
    tokens = chunk._count(doc.markdown)
    return "short docs" if tokens < 3000 else "medium docs" if tokens < 12000 else "long docs"


# --- Scratch database ----------------------------------------------------------------------------


async def _scratch_pool() -> asyncpg.Pool:
    """ A pool on this model's scratch database, `kb_eval_<model>`: created on first use, emptied of documents on every run."""

    name = "kb_eval_" + re.sub(r"\W+", "_", settings.embedding_model.lower())
    conn = await asyncpg.connect(settings.database_url)
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()

    dsn = urlsplit(settings.database_url)._replace(path=f"/{name}").geturl()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS chunks, document_tags, documents")  # the previous run's corpus
    finally:
        await conn.close()

    pool = await init_db(dsn)
    await pool.execute("DROP INDEX chunks_embedding_hnsw")  # exact search: compare chunkings, not approximate recall
    await pool.execute("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector")  # any width, e.g. 3072 for -large
    await pool.execute("CREATE TABLE IF NOT EXISTS eval_vectors (hash TEXT PRIMARY KEY, embedding VECTOR NOT NULL)")
    return pool


async def _vectors(pool: asyncpg.Pool, embedder: OpenAIEmbedder, texts: list[str], dimensions: int | None) -> dict[str, Any]:
    """ A vector for every text, embedding only the ones no earlier run has cached, cut to `dimensions` if given."""

    keys = {t: hashlib.sha256(t.encode()).hexdigest() for t in texts}
    cached = await pool.fetch("SELECT hash FROM eval_vectors WHERE hash = ANY($1::text[])", list(keys.values()))
    missing = [t for t, h in keys.items() if h not in {r["hash"] for r in cached}]

    gate = asyncio.Semaphore(6)  # parallel requests; well inside a 10M tokens/minute quota

    async def batch(part: list[str]) -> list[list[float]]:
        async with gate:
            return await embedder.embed(part)

    parts = await asyncio.gather(*(batch(missing[i : i + BATCH_SIZE]) for i in range(0, len(missing), BATCH_SIZE)))
    embedded = [vector for part in parts for vector in part]
    new = [(keys[t], vector) for t, vector in zip(missing, embedded, strict=True)]
    await pool.executemany("INSERT INTO eval_vectors VALUES ($1, $2) ON CONFLICT DO NOTHING", new)

    # The first n numbers of an OpenAI embedding are what the API returns for `dimensions=n`, up to scale.
    rows = await pool.fetch(
        "SELECT hash, subvector(embedding, 1, COALESCE($2, vector_dims(embedding))) AS embedding "
        "FROM eval_vectors WHERE hash = ANY($1::text[])",
        list(keys.values()), dimensions,
    )
    found = {r["hash"]: r["embedding"] for r in rows}
    return {t: found[h] for t, h in keys.items()}


async def _store(pool: asyncpg.Pool, embedder: OpenAIEmbedder, docs: dict[str, ParsedDocument],
                 loaded: dict[str, list[str]], dimensions: int | None) -> dict[str, list[chunk.Chunk]]:
    """ Chunk every document with the current `chunk` constants; store the ones whose chunks changed since the last setting."""

    chunked = {name: chunk.chunk_document(d.markdown, d.pages) for name, d in docs.items()}
    vectors = await _vectors(pool, embedder, [c.text for cs in chunked.values() for c in cs], dimensions)

    for name, chunks in chunked.items():
        texts = [c.text for c in chunks]
        if loaded.get(name) == texts:
            continue  # e.g. a table-only file: tables ignore the overlap

        doc_id = await queries.insert_processing(pool, filename=name, content_hash=name)
        rows = [
            queries.ChunkRow(f"{doc_id}:{c.chunk_index}", c.chunk_index, c.text, vectors[c.text], c.page_start,
                             c.page_end, c.section or None)
            for c in chunks
        ]
        await queries.replace_chunks(pool, doc_id, rows, tags=["eval"], content_hash=name, page_count=docs[name].page_count)
        loaded[name] = texts

    return chunked


# --- Scoring --------------------------------------------------------------------------------------


def _sign_test(wins: int, losses: int) -> float:
    """ Two-sided exact p-value that the questions only one of two settings answers split this unevenly by chance."""

    n = wins + losses
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n) if n else 1.0


def _floor_effect(golden: list[dict], top_hits: list[list[dict]]) -> tuple[int, int, int]:
    """ What `RELEVANCE_FLOOR` does to the top hits: the answers it removes, and the near-miss and off-topic negatives
    it lets through (their search still returns something)."""

    lost = near_misses = off_topic = 0
    for q, hits in zip(golden, top_hits, strict=True):
        kept = above_floor(hits)
        if q["evidence"] is not None:
            lost += any(h["answer"] for h in hits) and not any(h["answer"] for h in kept)
        elif q.get("off_topic"):
            off_topic += bool(kept)
        else:
            near_misses += bool(kept)
    return lost, near_misses, off_topic


def _score(golden: list[dict], top: list[list[asyncpg.Record]], deep: list[list[asyncpg.Record]],
           chunked: dict[str, list[chunk.Chunk]]) -> dict[str, Any]:
    """ One setting's numbers from each question's TOP_K and DEPTH hits. They come from separate searches, as in
    production: hybrid fuses 3 × top_k candidates per side, so a deeper search can reorder the first TOP_K.
    Ranks are 1-based; None means the evidence is in none of the hits."""

    chunks = [c for cs in chunked.values() for c in cs]
    chunk_texts = {name: [norm(c.text) for c in cs] for name, cs in chunked.items()}
    ranks, top_ranks, hit_scores, fits, first_doc, intact, negative_tops, context, top_hits = [], [], [], 0, 0, 0, [], [], []

    for q, hits, deeper in zip(golden, top, deep, strict=True):
        context.append(sum(chunk._count(h["text"]) for h in hits))
        answers = [q["evidence"] is not None and q["_evidence"] in norm(h["text"]) for h in hits]
        top_hits.append([{"score": h["score"], "lexical": h["lexical"], "answer": a} for h, a in zip(hits, answers)])

        if q["evidence"] is None:
            negative_tops.append(max((h["score"] for h in hits), default=0.0))
            continue

        rank = next((i for i, h in enumerate(deeper, 1) if q["_evidence"] in norm(h["text"])), None)
        tokens = [chunk._count(h["text"]) for h in deeper]
        ranks.append(rank)
        top_ranks.append(answers.index(True) + 1 if any(answers) else None)
        hit_scores.append(deeper[rank - 1]["score"] if rank else None)
        first_doc += bool(hits) and hits[0]["filename"] in q["_docs"]
        intact += any(q["_evidence"] in text for name in q["_docs"] for text in chunk_texts[name])
        fits += rank is not None and (rank == 1 or sum(tokens[:rank]) <= BUDGET)

    n = len(ranks)
    lost, near_misses, off_topic = _floor_effect(golden, top_hits)
    return {
        "chunks": len(chunks),
        "index_tokens": sum(chunk._count(c.text) for c in chunks),
        "intact": intact / n,
        "ans@1": sum(r == 1 for r in top_ranks) / n,
        f"ans@{TOP_K}": sum(r is not None for r in top_ranks) / n,
        f"ans@{DEPTH}": sum(r is not None for r in ranks) / n,
        "mrr": sum(1 / r for r in ranks if r) / n,
        "doc@1": first_doc / n,
        f"ans@{BUDGET}tok": fits / n,
        f"tokens@{TOP_K}": mean(context),
        "floor_lost": lost,
        "near_misses_passed": near_misses,
        "off_topic_passed": off_topic,
        "ranks": ranks,
        "top_ranks": top_ranks,
        "hit_scores": hit_scores,
        "negative_tops": negative_tops,
        "top_hits": top_hits,
    }


# --- Report ---------------------------------------------------------------------------------------


def _report(golden: list[dict], runs: dict[tuple[int, int], dict[str, Any]], baseline: tuple[int, int],
            dimensions: int | None, searched: str):
    """ Print the sweep as Markdown tables: overall, then answered@TOP_K by kind of document and length of answer,
    then the relevance floor at the current chunking."""

    base = runs.get(baseline)
    positives = [q for q in golden if q["evidence"] is not None]
    off_topic = sum(bool(q.get("off_topic")) for q in golden)
    near_misses = len(golden) - len(positives) - off_topic

    def answered(run: dict[str, Any], i: int) -> bool:
        return run["top_ranks"][i] is not None

    print(f"\n{settings.embedding_model}{f' cut to {dimensions} dimensions' if dimensions else ''}, "
          f"{'hybrid' if settings.hybrid_search else 'dense'} search on {searched}; {len(positives)} questions with "
          f"evidence, {near_misses} near misses, {off_topic} off-topic; floor {settings.relevance_floor}; "
          f"baseline {baseline[0]}/{baseline[1]}\n")
    print(f"| size | overlap | chunks | index Mtok | intact | ans@1 | ans@{TOP_K} | ans@{DEPTH} | MRR | doc@1 "
          f"| ans@{BUDGET} tok | tokens@{TOP_K} | vs baseline | lost to floor | near misses past floor "
          f"| off-topic past floor |")
    print("|---" * 16 + "|")

    for (size, overlap), r in runs.items():
        vs = "-"
        if base is not None and (size, overlap) != baseline:
            wins = sum(answered(r, i) and not answered(base, i) for i in range(len(positives)))
            losses = sum(answered(base, i) and not answered(r, i) for i in range(len(positives)))
            vs = f"+{wins} −{losses} (p={_sign_test(wins, losses):.2f})"
        print(f"| {size} | {overlap} | {r['chunks']} | {r['index_tokens'] / 1e6:.2f} | {r['intact']:.3f} "
              f"| {r['ans@1']:.3f} | {r[f'ans@{TOP_K}']:.3f} | {r[f'ans@{DEPTH}']:.3f} | {r['mrr']:.3f} "
              f"| {r['doc@1']:.3f} | {r[f'ans@{BUDGET}tok']:.3f} | {r[f'tokens@{TOP_K}']:.0f} | {vs} "
              f"| {r['floor_lost']} | {r['near_misses_passed']} | {r['off_topic_passed']} |")

    groups = {f: [i for i, q in enumerate(positives) if q["_family"] == f] for f in sorted({q["_family"] for q in positives})}
    groups[f"answers < {PASSAGE} words"] = [i for i, q in enumerate(positives) if len(q["_evidence"].split()) < PASSAGE]
    groups[f"answers {PASSAGE}+ words"] = [i for i, q in enumerate(positives) if len(q["_evidence"].split()) >= PASSAGE]

    print(f"\nans@{TOP_K} by kind of document and length of answer (questions in brackets):\n")
    print("| size | overlap | " + " | ".join(f"{g} ({len(idx)})" for g, idx in groups.items()) + " |")
    print("|---|---|" + "---|" * len(groups))
    for (size, overlap), r in runs.items():
        cells = [f"{sum(answered(r, i) for i in idx) / len(idx):.3f}" for idx in groups.values()]
        print(f"| {size} | {overlap} | " + " | ".join(cells) + " |")

    if base is None:
        return
    found = sum(answered(base, i) for i in range(len(positives)))
    print(f"\nRelevance floor at {baseline[0]}/{baseline[1]}: answers in the top {TOP_K} it removes, negatives it lets through:\n")
    print("| floor | answers lost | near misses past it | off-topic past it |")
    print("|---|---|---|---|")
    configured = settings.relevance_floor
    try:
        for floor in sorted({*FLOORS, configured}):
            settings.relevance_floor = floor  # `above_floor` reads it, as the app does
            lost, near, off = _floor_effect(golden, base["top_hits"])
            print(f"| {floor}{' (current)' if floor == configured else ''} | {lost} of {found} | {near} of {near_misses} "
                  f"| {off} of {off_topic} |")
    finally:
        settings.relevance_floor = configured


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=int, nargs="+", default=[150, 200, 300, 400, 500, 700, 1000],
                        help="MAX_TOKENS values to try")
    parser.add_argument("--overlaps", type=float, nargs="+", default=[0, 0.1, 0.2, 0.3],
                        help="OVERLAP_TOKENS as a fraction of the size")
    parser.add_argument("--dimensions", type=int,
                        help="cut every embedding to this many dimensions, as the API's `dimensions` parameter does")
    parser.add_argument("--agent-queries", action="store_true",
                        help="search with each question's `query`, the phrase the in-app agent sends, not the question")
    args = parser.parse_args()

    field = "query" if args.agent_queries else "question"
    golden = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(field not in q for q in golden):
        parser.error("some questions have no `query`: run `python -m eval.draft_queries` first")

    baseline = (chunk.MAX_TOKENS, chunk.OVERLAP_TOKENS)
    docs = await load_corpus()
    normed = {name: norm(d.markdown) for name, d in docs.items()}
    families = {name: _family(name, d) for name, d in docs.items()}

    for q in golden:
        if q["evidence"] is not None:
            q["_evidence"] = norm(q["evidence"])
            q["_docs"] = {name for name, text in normed.items() if q["_evidence"] in text}
            q["_family"] = families.get(q["doc"], "missing")
            if not q["_docs"]:
                print(f"  evidence no longer in the corpus: {q['question']}")

    pool = await _scratch_pool()
    embedder = OpenAIEmbedder()
    question_vectors = await _vectors(pool, embedder, [q[field] for q in golden], args.dimensions)
    loaded: dict[str, list[str]] = {}
    runs: dict[tuple[int, int], dict[str, Any]] = {}

    async def search(top_k: int) -> list[list[asyncpg.Record]]:
        return await asyncio.gather(*(
            queries.search(pool, question_vectors[q[field]], levels=list(LEVELS), top_k=top_k,
                           hybrid_text=q[field] if settings.hybrid_search else None)
            for q in golden
        ))

    try:
        for size in args.sizes:
            for fraction in args.overlaps:
                chunk.MAX_TOKENS, chunk.OVERLAP_TOKENS = size, round(size * fraction)
                chunked = await _store(pool, embedder, docs, loaded, args.dimensions)
                r = runs[(size, chunk.OVERLAP_TOKENS)] = _score(golden, await search(TOP_K), await search(DEPTH), chunked)
                print(f"  {size}/{chunk.OVERLAP_TOKENS}: {r['chunks']} chunks, ans@{TOP_K} {r[f'ans@{TOP_K}']:.3f}, "
                      f"MRR {r['mrr']:.3f}", flush=True)
    finally:
        await pool.close()

    (CACHE / "runs.json").write_text(json.dumps({f"{s}/{o}": r for (s, o), r in runs.items()}), encoding="utf-8")
    _report(golden, runs, baseline, args.dimensions, "agent queries" if args.agent_queries else "questions")


if __name__ == "__main__":
    asyncio.run(main())

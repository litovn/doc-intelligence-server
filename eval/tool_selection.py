"""Tool-selection eval: does the chat model pick the right MCP tool, with the right arguments, from the definitions alone?

Each case runs through the in-app agent loop (`app.agent.answer`) against the real MCP server and the real
`KnowledgeBase`: the same descriptions, schemas, errors and hints an external client gets. Only the SQL layer is
replaced, by a small in-memory corpus built from the real filenames in `files/`. So the eval needs just the
chat-model settings (no Postgres, Document Intelligence or embeddings) and measures the interface, not retrieval.

    python -m eval.tool_selection --repeat 3
    python -m eval.tool_selection --repeat 3 --no-instructions   # a client that never shows the model `instructions`
"""

import argparse
import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import tiktoken
from mcp.client import Client

from app import agent
from app.agent import answer, chat_client
from app.config import settings
from app.mcp_server.tools import mcp, set_knowledge_base
from app.rag import queries
from app.rag.kb.service import KnowledgeBase

# --- Corpus -------------------------------------------------------------------------------------

TAGS = {
    "compliance": "Regulatory and internal policies: AML and KYC, gifts, record retention, complaints.",
    "faq": "Customer questions and answers, help-centre exports.",
    "hr": "People policies for employees: onboarding, probation, conduct.",
    "onboarding": "Material for new employees' first weeks.",
    "product": "Manuals, guides, fees and rates for Contoso banking products.",
}

# filename -> (tags, page count); the real files in `files/` and `files/it/`, newest first.
DOCUMENTS: dict[str, tuple[list[str], int | None]] = {
    "contoso-global-compliance-policy.pdf": (["compliance"], 12),
    "contoso-everyday-checking-account-guide.pdf": (["product"], 8),
    "contoso-personal-loan-product-manual.docx": (["product"], 9),
    "contoso-rewards-credit-card-overview.pptx": (["product"], 10),
    "contoso-high-yield-savings-faq.txt": (["faq", "product"], None),
    "contoso-fees-rates-and-limits.xlsx": (["product"], 3),
    "contoso-help-payments-and-transfers.html": (["faq", "product"], None),
    "contoso-mobile-banking-user-guide.md": (["product"], None),
    "contoso-product-catalog.csv": (["product"], None),
    "contoso-product-rate-card.json": (["product"], None),
    "onboarding-guide-1.pdf": (["hr", "onboarding"], 9),
    "onboarding-guide-2.pdf": (["hr", "onboarding"], 6),
    "Onboarding Experience Pack.pdf": (["hr", "onboarding"], 14),
    "financial-procedures-manual.pdf": (["compliance"], 30),
    "mansa-finance-accounting-manual.pdf": (["compliance"], 42),
    "contoso-informativa-conservazione-dati.md": (["compliance"], None),
    "contoso-procedura-reclami.md": (["compliance", "faq"], None),
}

LOAN_MANUAL = "contoso-personal-loan-product-manual.docx"

# (filename, heading path, page, body), in reading order within each document.
PASSAGES: list[tuple[str, str, int | None, str]] = [
    ("contoso-global-compliance-policy.pdf", "Contoso Global Compliance Policy > 1. Purpose and scope", 1,
     ("This policy (COMP-POL-001) sets the minimum compliance standards for every Contoso employee, contractor and "
      "subsidiary: anti-money laundering, know-your-customer checks, gifts and entertainment, and record retention.")),
    ("contoso-global-compliance-policy.pdf", "Contoso Global Compliance Policy > 2. Anti-money laundering", 3,
     ("Employees report suspicious activity to the Compliance Office within 24 hours on form AML-7. Cash "
      "transactions above $10,000 are reported to the regulator.")),
    ("contoso-global-compliance-policy.pdf", "Contoso Global Compliance Policy > 3. Gifts and entertainment", 6,
     ("Employees may accept gifts and entertainment worth up to $100 per year from any single client or supplier. "
      "Anything above that needs written approval from the Compliance Office and goes in the gifts register.")),
    ("contoso-global-compliance-policy.pdf", "Contoso Global Compliance Policy > 4. Record retention > 4.2 KYC records", 9,
     ("Know-your-customer (KYC) records, including identity documents and screening results, are retained for five "
      "years after the customer relationship ends. Transaction records are kept for seven years.")),
    ("contoso-global-compliance-policy.pdf", "Contoso Global Compliance Policy > 5. Breaches and escalation", 11,
     "Suspected breaches are escalated to the Head of Compliance. Deliberate breaches may lead to disciplinary action."),
    ("contoso-everyday-checking-account-guide.pdf", "Everyday Checking Account Guide > Fees", 3,
     ("There is no monthly maintenance fee with a $500 minimum daily balance; otherwise it is $12 a month. The "
      "overdraft fee is $35 per item, at most three per day.")),
    # Deliberately cut off mid-list: the rest is in the next chunk, which a search for these words does not return.
    ("contoso-everyday-checking-account-guide.pdf", "Everyday Checking Account Guide > Daily limits", 5,
     ("Every Everyday Checking account has four daily limits, which reset at midnight Eastern Time. "
      "1. ATM cash withdrawals: $500 per day. 2. Debit card purchases: $3,000 per day. 3.")),
    ("contoso-everyday-checking-account-guide.pdf", "Everyday Checking Account Guide > Daily limits", 5,
     ("Outgoing transfers to other banks: $2,500 per day. 4. Mobile check deposits: $5,000 per day. Premier "
      "customers can ask a branch to raise any of these amounts temporarily.")),
    ("contoso-personal-loan-product-manual.docx", "Personal Loan Product Manual > Eligibility", 2,
     "Applicants must be 18 or older, with a credit score of at least 660 and a debt-to-income ratio below 40%."),
    ("contoso-personal-loan-product-manual.docx", "Personal Loan Product Manual > Rates", 4,
     "Fixed APRs range from 7.99% to 24.99% depending on credit profile and term (24 to 60 months)."),
    ("contoso-personal-loan-product-manual.docx", "Personal Loan Product Manual > Fees > Late payment", 6,
     ("A payment received more than 15 days after the due date incurs a late payment fee of $39 or 5% of the "
      "missed payment, whichever is less.")),
    ("contoso-rewards-credit-card-overview.pptx", "Rewards Credit Card Overview > Cash advances", 7,
     "Cash advances cost 5% of the amount (minimum $10) and accrue interest at 29.99% APR from the day they are taken."),
    ("contoso-rewards-credit-card-overview.pptx", "Rewards Credit Card Overview > Late payment", 8,
     "A late card payment incurs a fee of up to $40."),
    ("contoso-high-yield-savings-faq.txt", "High-Yield Savings FAQ > Interest", None,
     ("The High-Yield Savings account earns 4.25% APY on balances of $1 or more. Interest compounds daily and is "
      "credited monthly.")),
    ("contoso-high-yield-savings-faq.txt", "High-Yield Savings FAQ > Withdrawals", None,
     "You can make six free withdrawals per statement cycle; each additional withdrawal costs $10."),
    ("contoso-fees-rates-and-limits.xlsx", "Fees", 1,
     ("<table><tr><th>Product</th><th>Fee</th><th>Amount</th></tr><tr><td>Everyday Checking</td><td>Overdraft</td>"
      "<td>$35</td></tr><tr><td>Rewards Credit Card</td><td>Foreign transaction</td><td>3%</td></tr>"
      "<tr><td>Personal Loan</td><td>Origination</td><td>up to 6%</td></tr></table>")),
    ("contoso-help-payments-and-transfers.html", "Payments and transfers help > Wire transfers", None,
     ("Domestic wires sent before 4 p.m. Eastern arrive the same business day. The outgoing wire fee is $25 "
      "domestic and $45 international.")),
    ("contoso-mobile-banking-user-guide.md", "Mobile Banking User Guide > Mobile check deposit", None,
     ("Endorse the check, open the app, choose Deposit and photograph both sides. Deposits made before 9 p.m. "
      "Eastern are available the next business day.")),
    ("contoso-product-catalog.csv", "", None,
     ("<table><tr><th>product_code</th><th>name</th><th>category</th></tr><tr><td>CHK-01</td>"
      "<td>Everyday Checking</td><td>Deposit</td></tr><tr><td>SAV-02</td><td>High-Yield Savings</td>"
      "<td>Deposit</td></tr><tr><td>LN-05</td><td>Personal Loan</td><td>Lending</td></tr></table>")),
    ("contoso-product-rate-card.json", "", None,
     '{"product": "High-Yield Savings", "apy": "4.25%", "effective": "2025-07-01"}'),
    ("onboarding-guide-1.pdf", "New Joiner Onboarding Guide > Your first week", 3,
     ("In your first week you meet your manager and buddy, complete security and compliance training, and set up "
      "your laptop and accounts. On Friday you join the welcome session with the leadership team.")),
    ("onboarding-guide-1.pdf", "New Joiner Onboarding Guide > Probation", 7,
     "Probation lasts six months, with check-ins at months one, three and six."),
    ("onboarding-guide-2.pdf", "Onboarding Guide for Branch Staff > Branch systems", 4,
     "Branch staff get access to the teller system after completing cash-handling training in week two."),
    ("Onboarding Experience Pack.pdf", "Onboarding Experience Pack > First 30 days", 2,
     ("During the first 30 days new colleagues shadow their team, complete mandatory learning modules and agree "
      "objectives for the first quarter.")),
    ("financial-procedures-manual.pdf", "Financial Procedures Manual > Expense approval", 12,
     "Expenses above $5,000 need a director's approval before the purchase order is raised."),
    ("mansa-finance-accounting-manual.pdf", "Mansa Finance Accounting Manual > Month-end close", 20,
     "The month-end close completes on the fifth working day, with reconciliations signed off by the controller."),
    ("contoso-informativa-conservazione-dati.md", "Informativa sulla conservazione dei dati > Periodi di conservazione", None,
     "I dati dei clienti sono conservati per dieci anni dalla chiusura del rapporto, come previsto dalla normativa."),
    ("contoso-procedura-reclami.md", "Procedura reclami > Tempi di risposta", None,
     "Contoso risponde ai reclami entro 30 giorni dalla ricezione."),
]


def _build() -> tuple[dict[UUID, dict[str, Any]], list[dict[str, Any]]]:
    """ Turn the corpus above into rows shaped like the `documents` and `chunks` query results.

    Returns:
        (documents by id, chunks in document then reading order). Chunk text is stored as the ingestion
        pipeline stores it: heading path, blank line, body.
    """
    start = datetime(2026, 9, 20, 12, tzinfo=UTC)
    documents: dict[UUID, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}

    for age, (filename, (tags, pages)) in enumerate(DOCUMENTS.items()):
        row: dict[str, Any] = {
            "id": uuid5(NAMESPACE_URL, filename), "filename": filename, "tags": tags, "page_count": pages,
            "uploaded_at": start - timedelta(hours=age), "status": "ready", "required_level": "employee",
            "error": None, "chunk_count": 0,
        }
        documents[row["id"]] = by_name[filename] = row

    chunks: list[dict[str, Any]] = []
    for filename, section, page, body in PASSAGES:
        doc = by_name[filename]
        index = doc["chunk_count"]
        doc["chunk_count"] += 1
        chunks.append({
            "id": f"{doc['id']}:{hashlib.sha256(filename.encode()).hexdigest()[:8]}:{index}",
            "document_id": doc["id"], "filename": filename, "tags": doc["tags"], "chunk_index": index,
            "section": section or None, "page_start": page, "page_end": page,
            "text": f"{section}\n\n{body}" if section else body,
            "body": body,  # what the fake search scores; not a real column
        })
    return documents, chunks


DOCS, CHUNKS = _build()


# --- Fake SQL layer: the `app.rag.queries` functions `KnowledgeBase` calls ---------------------------

STOPWORDS = frozenset(  # a word list reads better as one string
    "a an and any are as at be by can could do does for from get give has have how i in is it its me my of on or "  # noqa: SIM905
    "our say says should tell that the their there these this those to us we what when where which who will with "
    "would you your".split()
)


def _words(text: str) -> set[str]:
    """ Lower-cased content words, with a trailing plural `s` dropped ("records" matches "record")."""
    words = re.findall(r"[a-zà-ù0-9]+", text.lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in STOPWORDS}


async def _search(pool: Any, embedding: Any, *, levels: Sequence[str], top_k: int = 5, tags: Sequence[str] | None = None,
                  match: str = "any", document_ids: Sequence[UUID] | None = None, hybrid_text: str | None = None) -> list[dict[str, Any]]:
    # ponytail: word overlap on the body stands in for cosine similarity; it only has to put the obvious passage
    # first. Scoring the body (not the heading) keeps a section's other chunks out, so a cut-off hit stays cut off.
    query = _words(str(embedding))
    rows = []
    for chunk in CHUNKS:
        if tags and not (set(tags) & set(chunk["tags"]) if match == "any" else set(tags) <= set(chunk["tags"])):
            continue
        if document_ids and chunk["document_id"] not in document_ids:
            continue
        overlap = len(query & _words(chunk["body"])) / len(query) if query else 0.0
        rows.append({**chunk, "score": 0.2 + 0.7 * overlap, "lexical": False})
    return sorted(rows, key=lambda r: -r["score"])[:top_k]


async def _tag_names(pool: Any) -> set[str]:
    return set(TAGS)


async def _list_tags(pool: Any, *, levels: Sequence[str], only_in_use: bool = True) -> list[dict[str, Any]]:
    counts = Counter(tag for doc in DOCS.values() for tag in doc["tags"])
    return [
        {"name": name, "description": description, "document_count": counts[name]}
        for name, description in sorted(TAGS.items()) if counts[name] or not only_in_use
    ]


async def _list_documents(pool: Any, *, levels: Sequence[str], tag: str | None = None, name_words: Sequence[str] = (),
                          ready_only: bool = True, limit: int = 50) -> tuple[int, list[dict[str, Any]]]:
    rows = [
        doc for doc in DOCS.values()
        if (tag is None or tag in doc["tags"]) and all(w in doc["filename"].lower() for w in name_words)
    ]
    return len(rows), rows[:limit]  # DOCUMENTS is already newest first


async def _resolve_document_ids(pool: Any, names_or_ids: Sequence[str], *, levels: Sequence[str]) -> tuple[list[UUID], list[str]]:
    found = {doc["filename"]: doc["id"] for doc in DOCS.values()} | {str(i): i for i in DOCS}
    return [found[n] for n in names_or_ids if n in found], [n for n in names_or_ids if n not in found]


async def _get_by_id(pool: Any, document_id: UUID) -> dict[str, Any] | None:
    return DOCS.get(document_id)


async def _get_chunk(pool: Any, chunk_id: str, *, levels: Sequence[str]) -> dict[str, Any] | None:
    return next((c for c in CHUNKS if c["id"] == chunk_id), None)


async def _get_chunk_context(pool: Any, document_id: UUID, chunk_index: int, *, before: int, after: int) -> list[dict[str, Any]]:
    return [
        c for c in CHUNKS
        if c["document_id"] == document_id and chunk_index - before <= c["chunk_index"] <= chunk_index + after
    ]


async def _get_document_outline(pool: Any, document_id: UUID) -> list[dict[str, Any]]:
    sections: dict[str, dict[str, Any]] = {}
    for c in (c for c in CHUNKS if c["document_id"] == document_id):
        s = sections.setdefault(c["section"] or "", {
            "section": c["section"] or "", "page_start": c["page_start"], "page_end": c["page_end"],
            "chunk_count": 0, "first_chunk_id": c["id"],
        })
        s["chunk_count"] += 1
        s["page_end"] = c["page_end"]
    return list(sections.values())


FAKES: dict[str, Callable[..., Any]] = {
    "search": _search, "tag_names": _tag_names, "list_tags": _list_tags, "list_documents": _list_documents,
    "resolve_document_ids": _resolve_document_ids, "get_by_id": _get_by_id, "get_chunk": _get_chunk,
    "get_chunk_context": _get_chunk_context, "get_document_outline": _get_document_outline,
}


class _EchoEmbedder:
    """ Stands in for the embedding model: hands the query text to the fake `search`, which scores words."""

    async def embed(self, texts: Sequence[str]) -> list[str]:
        return list(texts)


def install() -> None:
    """ Swap the SQL layer for the in-memory corpus and bind a real `KnowledgeBase` to the MCP server."""
    for name, fake in FAKES.items():
        setattr(queries, name, fake)
    kb = KnowledgeBase(pool=None)  # type: ignore[arg-type]  # every query it makes is faked above
    kb._embedder = _EchoEmbedder()  # type: ignore[assignment]
    set_knowledge_base(kb)


# --- Cases --------------------------------------------------------------------------------------

Call = dict[str, Any]  # a `tool_call` event: {"name": ..., "arguments": {...}}
SEARCHES = {"search", "search_by_tag", "search_by_document"}


def _first(calls: list[Call]) -> str | None:
    return calls[0]["name"] if calls else None


def _called(calls: list[Call], name: str, where: Callable[[dict[str, Any]], bool] = lambda _: True) -> bool:
    return any(c["name"] == name and where(c["arguments"]) for c in calls)


@dataclass(frozen=True)
class Case:
    name: str
    prompt: str
    passes: Callable[[list[Call]], bool]


CASES = [
    Case("documents -> list_documents", "What documents do you have?",
         lambda c: _first(c) == "list_documents"),
    Case("topics -> list_tags", "What topics does the knowledge base cover?",
         lambda c: _first(c) == "list_tags"),
    Case("tag filter -> list_documents(tag)", "Which documents are tagged hr?",
         lambda c: _called(c, "list_documents", lambda a: a.get("tag") == "hr")),
    Case("topic area -> search_by_tag", "What does our onboarding material say about the first week?",
         lambda c: _first(c) in {"list_tags", "search_by_tag"}
         and _called(c, "search_by_tag", lambda a: "onboarding" in a.get("tags", []))),
    Case("named document -> search_by_document", "In the personal loan manual, what is the late payment fee?",
         lambda c: _called(c, "search_by_document", lambda a: LOAN_MANUAL in a.get("documents", []))),
    Case("fact -> search", "How long do we keep KYC records?",
         lambda c: _first(c) in {"search", "search_by_tag", "list_tags"} and any(x["name"] in SEARCHES for x in c)),
    Case("two questions -> two searches",
         "What is the APY on the high-yield savings account, and what is the overdraft fee on checking?",
         lambda c: sum(x["name"] in SEARCHES for x in c) >= 2),
    Case("typo'd tag -> recovers", "Search the complaince documents for the rules on gifts and entertainment.",
         lambda c: _called(c, "search_by_tag", lambda a: "compliance" in a.get("tags", []))),
    Case("unknown document -> recovers", "What does Card Terms 2025.pdf say about cash advances?",
         lambda c: any(x["name"] != "search_by_document"
                       or "Card Terms 2025.pdf" not in x["arguments"].get("documents", []) for x in c)),
    Case("cut-off hit -> get_chunk_context", "What are the daily limits on the Everyday Checking account?",
         lambda c: _called(c, "get_chunk_context")),
    Case("structure -> get_document_outline", "How is the Contoso Global Compliance Policy organised? List its main sections.",
         lambda c: _called(c, "get_document_outline")),
    Case("off-topic -> no tool", "What's the weather in Rome today?",
         lambda c: not c),
]


async def _run(case: Case, client: Any, gate: asyncio.Semaphore) -> list[Call] | Exception:
    """ Ask one case's question through the in-app agent loop and collect the tool calls it makes."""
    async with gate:
        calls: list[Call] = []
        try:
            async for event, data in answer([{"role": "user", "content": case.prompt}], client=client):
                if event == "tool_call":
                    calls.append(data)
        except Exception as exc:  # noqa: BLE001 - a failed run is reported, not fatal
            return exc
        return calls


def _describe(calls: list[Call] | Exception) -> str:
    if isinstance(calls, Exception):
        return f"error: {calls}"
    return " -> ".join(f"{c['name']}({json.dumps(c['arguments'], ensure_ascii=False)})" for c in calls) or "(no tool)"


# --- Token budget -------------------------------------------------------------------------------

SAMPLES = [
    ("search", {"query": "fee", "top_k": 5}),
    ("list_documents", {}),
    ("list_tags", {}),
    ("get_chunk_context", {"chunk_id": CHUNKS[6]["id"]}),  # the cut-off "Daily limits" chunk
]


async def token_budget() -> None:
    """ Print what the model reads: tool definitions (as most clients forward them) and representative results."""
    enc = tiktoken.get_encoding("o200k_base")

    def count(text: str) -> int:
        return len(enc.encode(text))

    async with Client(mcp) as c:
        tools = (await c.list_tools()).tools
        definitions = sum(
            count(json.dumps({"name": t.name, "description": t.description, "input_schema": t.input_schema}))
            for t in tools
        )
        print(f"\nToken budget (o200k_base)\n  {len(tools)} tool definitions (name + description + inputSchema): "
              f"{definitions}\n  instructions: {count(mcp.instructions or '')}")

        for name, arguments in SAMPLES:
            result = await c.call_tool(name, arguments)
            text = "".join(block.text for block in result.content if block.type == "text")
            structured = json.dumps(result.structured_content, separators=(",", ":"), ensure_ascii=False)
            print(f"  {name}({arguments}): content {count(text)} / structuredContent {count(structured)}")


# --- Main ---------------------------------------------------------------------------------------

async def main(repeat: int, concurrency: int, instructions: bool) -> None:
    logging.disable(logging.INFO)  # the agent and HTTP client log every call
    install()
    if not instructions:
        # MCP clients are free to drop server `instructions`: the model then has only the tool definitions.
        agent.SYSTEM_PROMPT = agent.ANSWERING_RULES
    client = chat_client()
    gate = asyncio.Semaphore(concurrency)
    runs = [(case, asyncio.create_task(_run(case, client, gate))) for case in CASES for _ in range(repeat)]

    passed: Counter[str] = Counter()
    failures: list[str] = []
    for case, task in runs:
        calls = await task
        if not isinstance(calls, Exception) and case.passes(calls):
            passed[case.name] += 1
        else:
            failures.append(f"  {case.name}: {_describe(calls)}")

    total = sum(passed.values())
    mode = "with" if instructions else "WITHOUT"
    print(f"Tool selection, CHAT_MODEL={settings.chat_model}, {len(CASES)} cases x {repeat}, {mode} server instructions\n")
    for case in CASES:
        print(f"  {case.name:<40} {passed[case.name]}/{repeat}")
    print(f"  {'total':<40} {total}/{len(runs)} ({total / len(runs):.0%})")
    if failures:
        print("\nFailed runs (tool calls in order):", *failures, sep="\n")

    await token_budget()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeat", type=int, default=1, help="runs per case (default 1)")
    parser.add_argument("--concurrency", type=int, default=4, help="agent runs in flight at once (default 4)")
    parser.add_argument("--no-instructions", action="store_true", help="leave the server instructions out of the prompt")
    args = parser.parse_args()
    asyncio.run(main(args.repeat, args.concurrency, instructions=not args.no_instructions))

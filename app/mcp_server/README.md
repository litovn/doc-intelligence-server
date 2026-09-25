# 4. MCP server

Chapter 4 of the [project README](../../README.md).

AI agents reach the knowledge base through an MCP server written in Python on the official `mcp` SDK. It is served over stateless Streamable HTTP at `/mcp` and exposes seven read-only tools.

**Principle: the LLM is the user, the tool description is the UI, the input schema is the form.** Every choice below aims to make the right call obvious and a wrong call self-correcting. The choices were checked with a tool-selection eval ([4.5](#45-measured-does-a-model-pick-the-right-tool)), including against a client that never shows the model the server's instructions.

## 4.1 Connect and authenticate

| | Endpoint |
|---|---|
| Azure Container Apps | `https://<azure-containterapp-link>.azurecontainerapps.io/mcp` |
| Local | `http://localhost:8000/mcp` |

- **Transport:** Streamable HTTP, stateless, JSON responses.
- **Authentication:** every request carries `Authorization: Bearer <key>`. There are two keys, and the key decides what the caller can read:

| Key | Environment variable | Reads |
|---|---|---|
| employee | `MCP_API_KEY_EMPLOYEE` | documents visible to everyone |
| manager | `MCP_API_KEY_MANAGER` | every document, manager-only ones included |

A request without a valid key is answered before any MCP code runs:

| Request | Response |
|---|---|
| no `Authorization` header | `401`, `WWW-Authenticate: Bearer realm="kb"` |
| a key that is not one of the two | `401`, `WWW-Authenticate: Bearer realm="kb", error="invalid_token", …` (RFC 6750) |
| more than `MCP_RATE_LIMIT_PER_MINUTE` (default 300) requests in a minute on one key | `429`, with `Retry-After` |
| a valid key | the MCP response, filtered to that key's level |

## 4.2 The seven tools


| Tool | The model should use it when… | Returns |
|---|---|---|
| `list_tags` | the user asks what topics exist; before `search_by_tag`, to turn "our onboarding material" into exact tag names | `{tags: [{tag, description, document_count}]}`, only tags on at least one document the caller may read |
| `list_documents` | the user asks what documents exist or which carry a tag; to find a document's exact name (the `name` filter) | `{total, documents: [{document_id, name, tags, uploaded_at, page_count, chunk_count}], hint?}`, newest first |
| `search` | a factual question: the default | a search result |
| `search_by_tag` | the question names a topic area or department | a search result |
| `search_by_document` | the question names a document, or a hit already showed which document answers it | a search result |
| `get_document_outline` | the user asks what a document covers or how it is organised; to find the right section of a long document | `{document_id, document_name, page_count, sections: [{heading, page_start, page_end, chunk_count, first_chunk_id}]}` |
| `get_chunk_context` | a hit is cut off mid-sentence, mid-list or mid-table; to read a section found in the outline | `{chunk_id, document_id, document_name, chunks: [{chunk_id, chunk_index, heading, page_start, page_end, text}]}`, in reading order |

A search result is the same for all three searches ([4.4](#44-what-comes-back-results-hints-and-errors)).

## 4.3 Input schemas


| Tool | Parameter | Type, default, bounds | Description the model reads |
|---|---|---|---|
| `list_documents` | `tag` | string, optional | Only documents carrying this tag, exactly as `list_tags` gives it: a tag, never a document title or subject. |
| | `name` | string, optional | Only documents whose name contains all of these words, in any order and case, e.g. 'loan manual'. |
| | `limit` | integer, default 50, 1–200 | How many documents to return at most. |
| `list_tags` | none | | |
| `search` | `query` | string, required, at least 3 characters; example `"late payment fee personal loan"` | What to look for, in the words a document would use (a short phrase), not the user's whole message. A question that asks two things gets two searches, one per thing. |
| | `top_k` | integer, default 5, 1–20 | How many passages to return, best first. Raise it when the answer is a list spread over many passages. |
| `search_by_tag` | `query`, `top_k` | as in `search` | |
| | `tags` | array of strings, required, at least 1 | Tag names exactly as `list_tags` gives them. |
| | `match` | `"any"` or `"all"`, default `"any"` | 'any': documents with at least one of the tags. 'all': only documents with every tag. |
| `search_by_document` | `query`, `top_k` | as in `search` | |
| | `documents` | array of strings, required, at least 1 | Exact document names, as `list_documents` or a hit's `document_name` gives them (document ids also work). |
| `get_document_outline` | `document` | string, required | One exact document name, as `list_documents` or a hit's `document_name` gives it (a document id also works). |
| `get_chunk_context` | `chunk_id` | string, required | The `chunk_id` of a search hit, or a section's `first_chunk_id` from `get_document_outline`. |
| | `before` | integer, default 1, 0–5 | How many passages before it to include. |
| | `after` | integer, default 1, 0–5 | How many passages after it to include. |


## 4.4 What comes back: results, hints and errors

**Every result comes twice**, as the MCP spec recommends: as `structuredContent`, validated against the tool's published `outputSchema`, and as the same JSON in the text `content`. Clients differ in which one they give the model.

The `search` call from 4.1:

```json
{
  "query": "daily ATM withdrawal limit",
  "filters": {},
  "result_count": 3,
  "results": [
    {
      "rank": 1,
      "score": 0.5851,
      "document_name": "deposit-account-agreement.pdf",
      "tags": ["product", "compliance"],
      "heading": "DEPOSIT ACCOUNT AGREEMENT AND PRIVACY NOTICE > III. Using Your Checking or Savings Account A. Adding Money to Your Account > 3. Limits on ATM withdrawals, card purchases, and electronic funds transfers",
      "page_start": 13,
      "page_end": 13,
      "chunk_id": "247d8435-26c3-45d5-b4a3-34d1fc66dab1:31dbc011:66",
      "chunk_index": 66,
      "text": "To protect your balance, we place daily dollar limits on ATM withdrawals and card purchases, even if your available balance is higher than the daily limit. …"
    }
  ]
}
```

Each field serves a next step:

| Field | Used for |
|---|---|
| `document_name`, `heading`, `page_start`, `page_end` | citing the source; the name is also what `search_by_document` and `get_document_outline` take |
| `chunk_id` | reading around the hit with `get_chunk_context` |
| `rank`, `score` | `rank` is the hybrid order (vector and keyword rankings fused), `score` the cosine similarity from 0 to 1, so scores are not always in descending order. Hits under the relevance floor (0.25) are dropped unless they contain every query word |
| `tags` | narrowing a follow-up with `search_by_tag` |
| `query`, `filters` | seeing what actually ran, after a rewording or a retry |
| `hint` | present only when nothing was found: what to try next |

The `outputSchema` fields carry descriptions too (the score's range, how pages are numbered, what `chunk_id` is for). 

**Hints and errors name the next step.** An empty or cut-off result carries a `hint`. A mistake the model made comes back with `isError: true` and a sentence that names the fix. Anything else is a bug: it is logged on the server and never leaked as a stack trace. Tested examples:

```text
search(query="weather in Rome today")
→ hint: Nothing scored above the relevance floor (0.25). Try other wording, or check `list_documents` — the knowledge base may not cover this topic.

list_documents(name="zebra unicorn")
→ hint: No document matches. Try fewer words in `name`, another `tag`, or `search` the topic instead.

search_by_tag(query="gifts and entertainment", tags=["complance"])
→ error: Unknown tag 'complance'. Did you mean 'compliance'? Call `list_tags` for the current list. Valid tags: compliance, faq, hr, onboarding, payments, product.
```


## 4.5 Test: does a model pick the right tool?

`python -m eval.tool_selection --repeat 5` sends 12 prompts through the in-app agent loop to this MCP server. `--no-instructions` leaves the server instructions out of the prompt, as some clients do. Run with `gpt-5.4-mini`, 5 runs per prompt, against the definitions in this chapter:

| Prompt | Passes when | With instructions | Without |
|---|---|---|---|
| What documents do you have? | the first call is `list_documents` | 5/5 | 3/5 |
| What topics does the knowledge base cover? | the first call is `list_tags` | 5/5 | 5/5 |
| Which documents are tagged hr? | `list_documents(tag="hr")` | 5/5 | 5/5 |
| What does our onboarding material say about the first week? | `search_by_tag` with `onboarding`, first or after `list_tags` | 5/5 | 5/5 |
| In the personal loan manual, what is the late payment fee? | `search_by_document` with the exact filename | 5/5 | 5/5 |
| How long do we keep KYC records? | a search, first or after `list_tags` | 5/5 | 5/5 |
| What is the APY on the high-yield savings account, and what is the overdraft fee on checking? | two or more searches | 4/5 | 3/5 |
| Search the complaince documents for the rules on gifts and entertainment. | `search_by_tag` with `compliance` | 5/5 | 5/5 |
| What does Card Terms 2025.pdf say about cash advances? (no such document) | a call other than `search_by_document` on the made-up name | 5/5 | 5/5 |
| What are the daily limits on the Everyday Checking account? (the hit stops mid-list) | `get_chunk_context` | 5/5 | 5/5 |
| How is the Contoso Global Compliance Policy organised? List its main sections. | `get_document_outline` | 5/5 | 5/5 |
| What's the weather in Rome today? | no tool call | 5/5 | 5/5 |
| **Total** | | **59/60 (98%)** | **56/60 (93%)** |

**The misses:**
- **A question that asks two things** is the weakest case. In 1 run with instructions and 2 without, the model folded both halves into one query (`"high-yield savings account APY overdraft fee checking"`). This is why the two-searches rule is stated in both the instructions and the `query` description, word for word.
- **"What documents do you have?"** without instructions: in 2 runs the model called `list_tags` first and then `list_documents`. The answer was right, with one extra call; the eval fails it because it checks the first call.


## 4.6 Authentication and access levels

**How it works.** `BearerAuthMiddleware` (`app/mcp_server/auth.py`) is an ASGI middleware around the MCP app. It reads the bearer token, compares it in constant time (`hmac.compare_digest`) with the employee and manager keys, counts the request against that key's rate limit, and runs the request with the key's level set in a request-scoped context variable. [4.1](#41-connect-and-authenticate) lists the responses.
- **Keys come only from the environment** (Container Apps secrets on Azure). Each variable can hold several comma-separated keys, so a new key goes live before the old one is removed (rotation without downtime), and a reviewer can get a key of their own that is revoked alone.
- **The rate limit** answers the spec's "servers MUST rate limit tool invocations" and caps what a leaked key can spend on embedding calls. It counts per process, which fits the single replica this deploys as; with several replicas it belongs in a gateway (Azure API Management) or in Redis.

**Access levels are never a tool parameter.** Which documents a caller may read comes from how it authenticated: its key here, its login session in the web app. The level travels in the context variable into every SQL query. A tool argument is input the model controls, and access control must not be something a model can opt out of. To an employee key, manager-only documents don't exist: searches never return their chunks, `list_documents` and `list_tags` don't count them, their names are unknown documents that are never offered as a "Did you mean", and their chunk ids are unknown.

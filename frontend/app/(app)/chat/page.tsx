"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";

// `EventSource` can only GET, and the history has to go up with the question,
// so the SSE stream is read straight off the `fetch` body. Frames are
// "event: <name>\ndata: <json>\n\n" — three lines of parsing, no dependency.

interface ToolCall {
  name: string;
  arguments: unknown;
  hit_count?: number;
  error?: string;
}

interface Source {
  document_name: string;
  page_start: number | null;
  page_end: number | null;
  heading: string | null;
  text: string;
}

interface Turn {
  role: "user" | "assistant";
  content: string;
  tools: ToolCall[];
  sources: Source[];
  error?: string;
}

function blank(role: Turn["role"], content = ""): Turn {
  return { role, content, tools: [], sources: [] };
}

function chipLabel(source: Source): string {
  const pages =
    source.page_start == null
      ? null
      : source.page_end && source.page_end !== source.page_start
        ? `pages ${source.page_start}–${source.page_end}`
        : `page ${source.page_start}`;
  return [source.document_name, pages, source.heading].filter(Boolean).join(" · ");
}

export default function ChatPage() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [turns]);

  // Every event lands on the assistant turn that is currently streaming — the
  // last one — so one updater covers all of them.
  function patchAnswer(change: (turn: Turn) => Turn) {
    setTurns((prev) => prev.map((t, i) => (i === prev.length - 1 ? change(t) : t)));
  }

  function handleFrame(frame: string) {
    const event = /^event: (.*?)\r?$/m.exec(frame)?.[1];
    const raw = /^data: (.*?)\r?$/m.exec(frame)?.[1];
    if (!event || !raw) return;
    const data = JSON.parse(raw);

    if (event === "token") {
      patchAnswer((t) => ({ ...t, content: t.content + data.text }));
    } else if (event === "tool_call") {
      patchAnswer((t) => ({ ...t, tools: [...t.tools, data as ToolCall] }));
    } else if (event === "tool_result") {
      // Always the call we just appended: the server emits the pair together.
      patchAnswer((t) => ({
        ...t,
        tools: t.tools.map((c, i) => (i === t.tools.length - 1 ? { ...c, ...data } : c)),
      }));
    } else if (event === "sources") {
      patchAnswer((t) => ({ ...t, sources: data as Source[] }));
    } else if (event === "error") {
      patchAnswer((t) => ({ ...t, error: data.message }));
    }
  }

  async function ask(e: FormEvent) {
    e.preventDefault();
    const content = question.trim();
    if (!content || busy) return;

    // The whole history goes up with the question and nothing is kept server
    // side, so this list is the entire conversation state. Only role/content
    // travels; the tool panels and chips are the browser's business.
    const messages = [...turns, blank("user", content)]
      .filter((t) => t.content)
      .map((t) => ({ role: t.role, content: t.content }));

    setQuestion("");
    setBusy(true);
    setTurns((prev) => [...prev, blank("user", content), blank("assistant")]);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages }),
      });
      if (!res.ok || !res.body) throw new Error(`Request failed with status ${res.status}`);

      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += value;
        let end: number;
        while ((end = buffer.indexOf("\n\n")) >= 0) {
          handleFrame(buffer.slice(0, end));
          buffer = buffer.slice(end + 2);
        }
      }
    } catch (err) {
      patchAnswer((t) => ({
        ...t,
        error: err instanceof Error ? err.message : "The answer stream failed.",
      }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="chat">
      <h1>Ask</h1>
      {turns.length === 0 && (
        <p className="hint">
          Questions are answered only from the documents in the knowledge base. Open the tool
          calls above an answer to see how it was found.
        </p>
      )}

      {turns.map((turn, index) => (
        <article key={index} className={`chat-turn chat-${turn.role}`}>
          {turn.tools.length > 0 && (
            <details className="tool-panel">
              <summary>
                {turn.tools.length} tool call{turn.tools.length === 1 ? "" : "s"}
              </summary>
              <ol>
                {turn.tools.map((call, i) => (
                  <li key={i}>
                    <code>{call.name}</code>
                    <pre>{JSON.stringify(call.arguments)}</pre>
                    {call.error ? (
                      <span className="tool-error">{call.error}</span>
                    ) : call.hit_count === undefined ? (
                      <span className="hint">running…</span>
                    ) : (
                      <span className="tool-hits">
                        {call.hit_count} hit{call.hit_count === 1 ? "" : "s"}
                      </span>
                    )}
                  </li>
                ))}
              </ol>
            </details>
          )}

          <p className="chat-text">
            {turn.content || (turn.role === "assistant" && !turn.error ? "…" : "")}
          </p>

          {turn.sources.length > 0 && (
            <div className="chat-sources">
              {turn.sources.map((source, i) => (
                // <details> so a chip reveals the chunk it cites; the originals
                // aren't stored, so the text is all there is to open.
                <details key={i} className="source-chip">
                  <summary>{chipLabel(source)}</summary>
                  <p>{source.text}</p>
                </details>
              ))}
            </div>
          )}

          {turn.error && <p className="form-error">{turn.error}</p>}
        </article>
      ))}
      <div ref={bottom} />

      <form className="chat-form" onSubmit={ask}>
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask the knowledge base…"
          aria-label="Question"
          autoFocus
        />
        <button type="submit" disabled={busy || !question.trim()}>
          {busy ? "Asking…" : "Ask"}
        </button>
      </form>
    </div>
  );
}

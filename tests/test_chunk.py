from app.rag.ingestion import chunk
from app.rag.ingestion.parse import PageSpan


def test_zero_overlap_repeats_nothing(monkeypatch):
    monkeypatch.setattr(chunk, "OVERLAP_TOKENS", 0)
    paragraphs = [f"Paragraph {i} says something about fees." for i in range(200)]
    text = "\n\n".join(paragraphs)

    chunks = chunk.chunk_document(text, [PageSpan(1, 0, len(text))])

    joined = "\n\n".join(c.text for c in chunks)
    assert len(chunks) > 1
    assert all(joined.count(p) == 1 for p in paragraphs)

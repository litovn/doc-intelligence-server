import re

from app.rag.ingestion.parse import PageSpan, ParsedDocument

# `PageHeader="…"` / `PageFooter="…"` / `PageNumber="…"` / `PageBreak`, bare or wrapped in an HTML comment
_NOISE_LINE = re.compile(r"^\s*(?:<!--\s*)?Page(?:Header|Footer|Number|Break)\b.*$", re.IGNORECASE)


def clean(parsed: ParsedDocument) -> ParsedDocument:
    """ Strip the per-page noise Document Intelligence emits, keeping the page map aligned."""

    out: list[str] = []
    pages: list[PageSpan] = []
    cursor = 0

    for page in parsed.pages:
        kept = "\n".join(
            line
            for line in parsed.markdown[page.start : page.end].splitlines()
            if not _NOISE_LINE.match(line)
        ).strip("\n")

        if kept:
            kept += "\n"

        out.append(kept)
        pages.append(PageSpan(page.number, cursor, cursor + len(kept)))
        cursor += len(kept)

    return ParsedDocument("".join(out), pages)

import asyncio
import csv
import html
import io
import json
from pathlib import PurePath
from typing import Any, NamedTuple

from app.config import settings

LOCAL_SUFFIXES = frozenset({".txt", ".md", ".csv", ".json"})

# Range one page occupies in the Markdown string, and its page number. 
class PageSpan(NamedTuple):
    number: int
    start: int
    end: int


class ParsedDocument(NamedTuple):
    markdown: str
    pages: list[PageSpan]

    @property
    def page_count(self) -> int:
        page_numbers = {span.number for span in self.pages}
        return len(page_numbers)


def _one_page(markdown: str) -> ParsedDocument:
    """ Wrap text that has no pages as a single page."""
    return ParsedDocument(markdown, [PageSpan(1, 0, len(markdown))])


def _parse_local(suffix: str, content: bytes) -> ParsedDocument:
    """ Parse a local file type into Markdown, without Document Intelligence."""

    text = content.decode("utf-8", errors="replace")

    if suffix == ".csv":
        return _one_page(_csv_to_html_table(text))
    
    if suffix == ".json":
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Not valid JSON: {exc}") from exc

    return _one_page(text)


def _csv_to_html_table(text: str) -> str:
    """ Rendered as one HTML table, the same handling Document Intelligence output."""

    rows = list(csv.reader(io.StringIO(text)))

    if not rows:
        return ""

    body = "".join(
        "<tr>" + "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in row) + "</tr>"
        for tag, row in [("th", rows[0]), *[("td", r) for r in rows[1:]]]
    )

    return f"<table>{body}</table>"


def _di_client():
    """ Create Azure DocumentIntelligenceClient"""

    if not (settings.azure_di_endpoint and settings.azure_di_key):
        raise RuntimeError(
            "This file type needs Azure Document Intelligence; set AZURE_DI_ENDPOINT and AZURE_DI_KEY."
        )

    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.core.credentials import AzureKeyCredential

    return DocumentIntelligenceClient(
        endpoint=settings.azure_di_endpoint, 
        credential=AzureKeyCredential(settings.azure_di_key)
    )


def _analyze(client: Any, content: bytes) -> ParsedDocument:
    """ Call Document Intelligence to parse a document into Markdown and page spans."""

    from azure.ai.documentintelligence.models import DocumentContentFormat

    poller = client.begin_analyze_document(
        "prebuilt-layout",
        body=io.BytesIO(content),  
        output_content_format=DocumentContentFormat.MARKDOWN,
    )
    result = poller.result()
    
    pages = [
        PageSpan(page.page_number, span.offset, span.offset + span.length)
        for page in (result.pages or [])
        for span in (page.spans or [])
    ]
    return ParsedDocument(result.content, pages or [PageSpan(1, 0, len(result.content))])


async def parse(filename: str, content: bytes) -> ParsedDocument:
    """ Parse uploaded bytes into Markdown.

    Args:
        filename: the uploaded name.
        content: the raw file bytes.

    Returns:
        The Markdown and its page spans. Local files are always a single page 1.
    """

    suffix = PurePath(filename).suffix.lower()

    if suffix in LOCAL_SUFFIXES:
        return _parse_local(suffix, content)

    return await asyncio.to_thread(_analyze, _di_client(), content)
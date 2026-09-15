import os

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

load_dotenv()

SCOPUS_API_KEY = os.environ.get("SCOPUS_API_KEY")
BASE_URL = "https://api.elsevier.com/content"

mcp = MCPServer("scopus-mcp")


def _headers() -> dict[str, str]:
    if not SCOPUS_API_KEY:
        raise RuntimeError("SCOPUS_API_KEY is not set")
    return {"X-ELS-APIKey": SCOPUS_API_KEY, "Accept": "application/json"}


async def _get(path: str, params: dict[str, str]) -> dict:
    """GET a Scopus API endpoint, raising a descriptive error on failure."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}{path}", headers=_headers(), params=params, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Elsevier API returned {resp.status_code} for {path}: {resp.text[:500]}")
    return resp.json()


def _record_url(entry: dict) -> str:
    """Link back to the record's page on scopus.com, per Elsevier's federated-search terms."""
    for link in entry.get("link", []) or []:
        if link.get("@ref") == "scopus":
            return link.get("@href", "")
    eid = entry.get("eid") or f"2-s2.0-{entry.get('dc:identifier', '').replace('SCOPUS_ID:', '')}"
    return f"https://www.scopus.com/record/display.uri?eid={eid}&origin=resultslist"


@mcp.tool()
async def search_scopus(query: str, count: int = 10) -> str:
    """Search Scopus for publications matching a query.

    Returns core bibliographic data only (title, authors, year, DOI, citation
    count, and a link to the record on scopus.com) — no abstracts, per
    Elsevier's federated-search API terms for Scopus.

    Args:
        query: Scopus search query (e.g. "TITLE-ABS-KEY(large language models)").
        count: Number of results to return (max 25).
    """
    params = {"query": query, "count": str(min(count, 25))}
    data = await _get("/search/scopus", params)

    entries = data.get("search-results", {}).get("entry", [])
    if not entries:
        return "No results found."

    lines = []
    for e in entries:
        title = e.get("dc:title", "Untitled")
        authors = e.get("dc:creator", "Unknown")
        year = (e.get("prism:coverDate") or "")[:4]
        doi = e.get("prism:doi", "")
        cited_by = e.get("citedby-count", "0")
        lines.append(
            f"- [Scopus] {title} ({year}) — {authors}\n"
            f"  DOI: {doi or 'n/a'} | Cited by: {cited_by} | {_record_url(e)}"
        )
    return "\n".join(lines)


_SD_ALLOWED_COUNTS = (10, 25, 50, 100)


def _sd_authors(entry: dict) -> str:
    creator = entry.get("dc:creator")
    if creator:
        return creator
    author = (entry.get("authors") or {}).get("author")
    if isinstance(author, list):
        return ", ".join(a.get("name", str(a)) if isinstance(a, dict) else str(a) for a in author)
    return author or "Unknown"


def _sd_link(entry: dict) -> str:
    """Link back to the record's page on sciencedirect.com, per Elsevier's search-widget terms."""
    for link in entry.get("link", []) or []:
        if link.get("@ref") == "scidir":
            return link.get("@href", "")
    return f"https://www.sciencedirect.com/science/article/pii/{entry.get('pii', '')}"


@mcp.tool()
async def search_sciencedirect(query: str, count: int = 10) -> str:
    """Search ScienceDirect for publications matching a query.

    Returns core bibliographic data (title, authors, journal, date, pages,
    DOI/PII, open-access flag) and a link to the record on sciencedirect.com,
    per Elsevier's search-widget API terms. Abstracts/snippets and author
    keywords are not yet included pending confirmation of the ScienceDirect
    Search API's field-alias names.

    Args:
        query: ScienceDirect boolean search (e.g. "title(quantum) AND srctitle(laser)").
        count: Number of results to return; snapped up to 10, 25, 50, or 100.
    """
    snapped_count = next((c for c in _SD_ALLOWED_COUNTS if count <= c), _SD_ALLOWED_COUNTS[-1])
    params = {"query": query, "count": str(snapped_count)}
    data = await _get("/search/sciencedirect", params)

    entries = data.get("search-results", {}).get("entry", [])
    if not entries:
        return "No results found."

    lines = []
    for e in entries:
        title = e.get("dc:title", "Untitled")
        authors = _sd_authors(e)
        year = (e.get("prism:coverDate") or "")[:4]
        journal = e.get("prism:publicationName", "")
        start_page = e.get("prism:startingPage", "")
        end_page = e.get("prism:endingPage", "")
        pages = f"{start_page}-{end_page}" if start_page and end_page else start_page or "n/a"
        doi = e.get("prism:doi", "") or e.get("dc:identifier", "").replace("DOI:", "")
        oa = " [Open Access]" if e.get("openaccess") else ""
        lines.append(
            f"- [ScienceDirect] {title} ({year}) — {authors}{oa}\n"
            f"  {journal} | Pages: {pages} | DOI: {doi or 'n/a'} | {_sd_link(e)}"
        )
    return "\n".join(lines)


class AuthMiddleware:
    """Rejects requests missing the shared-secret bearer token, when one is configured."""

    def __init__(self, app):
        self.app = app
        self.token = os.environ.get("MCP_AUTH_TOKEN")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.token:
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        auth = headers.get(b"authorization", b"").decode()
        if auth != f"Bearer {self.token}":
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    import uvicorn
    from mcp.server.streamable_http import TransportSecuritySettings

    # DNS-rebinding host checks are for browser-facing local servers; this is a
    # public HTTPS endpoint already gated by AuthMiddleware's shared secret.
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = AuthMiddleware(mcp.streamable_http_app(transport_security=security))
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)

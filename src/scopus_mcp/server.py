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


@mcp.tool()
async def search_scopus(query: str, count: int = 10) -> str:
    """Search Scopus for publications matching a query.

    Args:
        query: Scopus search query (e.g. "TITLE-ABS-KEY(large language models)").
        count: Number of results to return (max 25).
    """
    params = {"query": query, "count": min(count, 25)}
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/search/scopus",
            headers=_headers(),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

    entries = data.get("search-results", {}).get("entry", [])
    if not entries:
        return "No results found."

    lines = []
    for e in entries:
        title = e.get("dc:title", "Untitled")
        authors = e.get("dc:creator", "Unknown")
        year = (e.get("prism:coverDate") or "")[:4]
        doi = e.get("prism:doi", "")
        scopus_id = e.get("dc:identifier", "").replace("SCOPUS_ID:", "")
        cited_by = e.get("citedby-count", "0")
        lines.append(
            f"- {title} ({year}) — {authors}\n"
            f"  Scopus ID: {scopus_id} | DOI: {doi or 'n/a'} | Cited by: {cited_by}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_abstract(scopus_id: str) -> str:
    """Fetch the abstract and metadata for a single Scopus publication.

    Args:
        scopus_id: The Scopus document ID (e.g. "85123456789").
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/abstract/scopus_id/{scopus_id}",
            headers=_headers(),
            params={"view": "META_ABS"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

    core = data.get("abstracts-retrieval-response", {}).get("coredata", {})
    title = core.get("dc:title", "Untitled")
    abstract = core.get("dc:description", "No abstract available.")
    doi = core.get("prism:doi", "n/a")
    return f"Title: {title}\nDOI: {doi}\n\nAbstract:\n{abstract}"


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

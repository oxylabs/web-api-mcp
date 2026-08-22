"""Self-hostable MCP server exposing the Oxylabs Web API to agents.

Transports:
    stdio           (default) — local clients: Claude Code, Claude Desktop, Cursor
    streamable-http           — self-hosting behind your own network and auth

Run:
    OXYLABS_API_KEY=... oxylabs-web-api-mcp
    OXYLABS_API_KEY=... oxylabs-web-api-mcp --transport streamable-http --port 8080
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

BASE_URL = os.environ.get("OXYLABS_BASE_URL", "https://webapi.oxylabs.io").rstrip("/")
TIMEOUT = float(os.environ.get("OXYLABS_TIMEOUT", "120"))

mcp = MCPServer(
    name="oxylabs-web-api",
    version="0.1.0",
    instructions=(
        "Access the live web through the Oxylabs Web API. Use `search` to find URLs for a "
        "question, then `scrape` to read the full content of the URLs worth reading. "
        "Prefer search+scrape over answering from memory whenever freshness matters."
    ),
)


class ApiError(RuntimeError):
    """Raised with a message an agent can act on rather than a raw stack trace."""


def _api_key() -> str:
    key = os.environ.get("OXYLABS_API_KEY", "").strip()
    if not key:
        raise ApiError(
            "OXYLABS_API_KEY is not set. Set it in the server environment "
            "(see .env.example) and restart the MCP server."
        )
    return key


def _detail(resp: httpx.Response) -> str:
    """Pull the useful part out of an error body; fall back to raw text."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict):
        # Validation errors carry `extra`; gateway errors carry `message`.
        problems = body.get("extra")
        if problems:
            return "; ".join(
                f"{p.get('key')}: {p.get('message')}" for p in problems if isinstance(p, dict)
            )
        return str(body.get("detail") or body.get("message") or body)[:500]
    return str(body)[:500]


def _check(resp: httpx.Response, path: str) -> dict[str, Any]:
    if resp.status_code == 401:
        raise ApiError("Authentication failed (401). Check OXYLABS_API_KEY.")
    if resp.status_code == 429:
        raise ApiError("Rate limited (429). Back off and retry with fewer concurrent requests.")
    if resp.status_code >= 400:
        raise ApiError(f"{path} returned {resp.status_code}: {_detail(resp)}")
    return resp.json()


async def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call the Web API, turning transport failures into agent-readable errors."""
    headers = {"Authorization": f"Bearer {_api_key()}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, f"{BASE_URL}{path}", json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise ApiError(
            f"Request to {path} timed out after {TIMEOUT:.0f}s. Retry, or narrow the request."
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Oxylabs Web API at {BASE_URL}{path}: {exc}") from exc
    return _check(resp, path)


@mcp.tool()
async def search(query: str, max_results: int = 10, location: str | None = None) -> dict[str, Any]:
    """Search the live web and return ranked organic results.

    Returns titles, short descriptions and URLs — not full page content. Follow up with
    `scrape` on the URLs you actually need to read.

    Args:
        query: What to search for, as a natural search phrase.
        max_results: Number of results to return, 1-20 (default 10).
        location: Optional geo context, e.g. "Germany" or "New York,New York,United States".
    """
    if not query.strip():
        raise ApiError("`query` must not be empty.")
    if not 1 <= max_results <= 20:
        raise ApiError("`max_results` must be an integer between 1 and 20.")

    payload: dict[str, Any] = {"query": query, "max_results": max_results}
    if location:
        payload["location"] = location
    return await _request("POST", "/v1/search", payload)


@mcp.tool()
async def scrape(url: str) -> dict[str, Any]:
    """Fetch and parse a single URL, including JavaScript-heavy and bot-protected pages.

    Use this to read a page's actual content after `search` has told you which URL to read,
    or whenever the user gives you a URL directly.

    Args:
        url: Absolute http(s) URL of the page to read.
    """
    if not url.startswith(("http://", "https://")):
        raise ApiError(f"`url` must be an absolute http(s) URL, got: {url!r}")
    return await _request("POST", "/v1/scrape", {"url": url})


@mcp.tool()
async def list_scrapers() -> dict[str, Any]:
    """List the scrape endpoints this API implements, to discover target-specific support.

    Call this before assuming a dedicated scraper does or does not exist for a target.
    """
    return await _request("GET", "/v1/scrapers")


def _transport_security() -> TransportSecuritySettings:
    """Build Host/Origin allowlists for HTTP self-hosting.

    The SDK enables DNS-rebinding protection by default with an empty allowlist, which
    rejects every request — so a self-hosted deployment must declare its own hostname.
    """
    hosts = os.environ.get("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*")
    origins = os.environ.get("MCP_ALLOWED_ORIGINS", "")
    return TransportSecuritySettings(
        allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
        allowed_origins=[o.strip() for o in origins.split(",") if o.strip()],
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="oxylabs-web-api-mcp", description=__doc__)
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="stdio for local clients, streamable-http when self-hosting (default: stdio)",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()

    if args.transport == "streamable-http":
        mcp.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            transport_security=_transport_security(),
        )
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()

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
import hashlib
import os
import os.path
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

BASE_URL = os.environ.get("OXYLABS_BASE_URL", "https://webapi.oxylabs.io").rstrip("/")
TIMEOUT = float(os.environ.get("OXYLABS_TIMEOUT", "120"))

# Content larger than this is offloaded to disk (local) or truncated (remote).
MAX_INLINE_CHARS = int(os.environ.get("OXYLABS_MAX_INLINE_CHARS", "40000"))
PREVIEW_CHARS = 2000
SPILL_TTL_SECONDS = float(os.environ.get("OXYLABS_SPILL_TTL_HOURS", "6")) * 3600

# Writing scraped pages to disk only makes sense when the agent shares a filesystem with
# this server, i.e. stdio. A remote HTTP server hands back a path nobody can open, so it
# truncates instead. main() flips this on; the safe default is off.
_SPILL_ENABLED = False

# The API renders the requested formats server-side via `output`, which takes a list.
OUTPUT_PARAM = "output"

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


# --------------------------------------------------------------------------- content


def _spill_dir() -> Path:
    path = Path(
        os.environ.get("OXYLABS_SPILL_DIR", Path(tempfile.gettempdir()) / "oxylabs-web-api-mcp")
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prune_spills(directory: Path) -> None:
    """Delete spill files older than the TTL. Cheap enough to run on every write."""
    cutoff = time.time() - SPILL_TTL_SECONDS
    for stale in directory.glob("*.txt"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass  # Another process got there first, or the file is not ours to remove.


def _spill(text: str, url: str, fmt: str) -> Path:
    """Write oversized content to the spill dir and return its path."""
    directory = _spill_dir()
    _prune_spills(directory)
    stem = hashlib.sha256(url.encode()).hexdigest()[:16]
    path = directory / f"{stem}.{'md' if fmt == 'markdown' else 'html'}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _offload(result: dict[str, Any], text: str, url: str, fmt: str) -> None:
    """Replace oversized `content` in place with a pointer or a truncation notice."""
    if _SPILL_ENABLED:
        path = _spill(text, url, fmt)
        result["content"] = text[:PREVIEW_CHARS]
        result["content_offloaded"] = {
            "path": str(path),
            "format": fmt,
            "total_chars": len(text),
            "preview_chars": min(PREVIEW_CHARS, len(text)),
            "note": (
                "`content` above is only the first "
                f"{min(PREVIEW_CHARS, len(text))} characters. The full page is on disk. "
                "Read it with the `read_scraped` tool: read_scraped(path, offset=0), then "
                "keep calling it with the `next_offset` it returns until `eof` is true. "
                "Read only as far as you need — do not pull the whole file in by reflex."
            ),
        }
    else:
        result["content"] = text[:MAX_INLINE_CHARS]
        result["content_truncated"] = {
            "total_chars": len(text),
            "returned_chars": MAX_INLINE_CHARS,
            "note": (
                "Content exceeded the inline limit and was truncated. This server runs over "
                "HTTP, so it cannot hand back a file path the caller could open. Run the "
                "server over stdio to get full pages offloaded to disk instead."
            ),
        }


def _process_content(payload: dict[str, Any], fmt: str) -> dict[str, Any]:
    """Offload or truncate oversized page content. No reformatting happens here —
    the API renders the requested format server-side."""
    results = payload.get("results")
    if not isinstance(results, list):
        return payload

    requested_url = str(payload.get("params", {}).get("url", ""))
    for result in results:
        if not isinstance(result, dict):
            continue
        content = result.get("content")
        if isinstance(content, str) and len(content) > MAX_INLINE_CHARS:
            _offload(result, content, str(result.get("url") or requested_url), fmt)
    return payload


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
async def scrape(
    url: str,
    format: str = "markdown",
    location: str | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    """Fetch and read a single URL, including JavaScript-heavy and bot-protected pages.

    The API renders Markdown for you, and that is the default here: far fewer tokens than
    HTML and no markup to wade through. Ask for HTML only when you need the markup itself,
    e.g. to inspect attributes or embedded data.

    Very large pages are not returned inline. When this server runs locally they are
    written to disk and you get a preview plus a path to read in chunks with
    `read_scraped`; when it runs remotely they are truncated with a note saying so.

    Args:
        url: Absolute http(s) URL of the page to read.
        format: "markdown" (default) or "html".
        location: Two-letter country code to fetch the page from, e.g. "DE". Use it when
            the page varies by country — pricing, availability, language.
        device: "desktop" (default upstream) or "mobile".
    """
    if not url.startswith(("http://", "https://")):
        raise ApiError(f"`url` must be an absolute http(s) URL, got: {url!r}")
    if format not in ("markdown", "html"):
        raise ApiError(f'`format` must be "markdown" or "html", got: {format!r}')
    if device is not None and device not in ("desktop", "mobile"):
        raise ApiError(f'`device` must be "desktop" or "mobile", got: {device!r}')

    payload: dict[str, Any] = {"url": url, OUTPUT_PARAM: [format]}
    if location:
        payload["location"] = location
    if device:
        payload["device"] = device

    return _process_content(await _request("POST", "/v1/scrape", payload), format)


@mcp.tool()
async def read_scraped(path: str, offset: int = 0, length: int = MAX_INLINE_CHARS) -> dict[str, Any]:
    """Read a chunk of a scraped page that was offloaded to disk.

    Use the `path` from a scrape result's `content_offloaded`. Start at offset 0 and keep
    calling with the returned `next_offset` until `eof` is true — and stop as soon as you
    have what you need rather than reading the whole file by reflex.

    Args:
        path: Path from `content_offloaded.path`.
        offset: Character offset to start at (default 0).
        length: How many characters to return (default matches the inline limit).
    """
    if offset < 0 or length <= 0:
        raise ApiError("`offset` must be >= 0 and `length` must be > 0.")

    # Only files this server wrote are readable — this tool is not a general file reader.
    directory = _spill_dir().resolve()
    target = Path(path).expanduser().resolve()
    if directory != target.parent or not target.name.endswith(".txt"):
        raise ApiError(
            f"Refusing to read {path!r}: only scraped pages under {directory} can be read "
            "with this tool. Use the exact path from `content_offloaded.path`."
        )
    if not target.is_file():
        raise ApiError(
            f"{path!r} no longer exists. Offloaded pages are temporary — scrape the URL again."
        )

    text = target.read_text(encoding="utf-8", errors="replace")
    chunk = text[offset : offset + length]
    next_offset = offset + len(chunk)
    return {
        "path": str(target),
        "offset": offset,
        "returned_chars": len(chunk),
        "total_chars": len(text),
        "next_offset": next_offset,
        "eof": next_offset >= len(text),
        "text": chunk,
    }


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

    # Offloading to disk is only useful when the agent can open the file, i.e. same host.
    global _SPILL_ENABLED
    _SPILL_ENABLED = args.transport == "stdio" and os.environ.get("OXYLABS_SPILL", "1") != "0"

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

"""Self-hostable MCP server exposing the Oxylabs Web API to agents.

Transports:
    stdio           (default) — local clients: Claude Code, Claude Desktop, Cursor
    streamable-http           — self-hosting behind your own network and auth

Run:
    OXYLABS_API_KEY=... oxylabs-web-api-mcp
    OXYLABS_API_KEY=... oxylabs-web-api-mcp --transport streamable-http --port 8080

Over HTTP the key can also arrive per request as `Authorization: Bearer <key>`, so one
deployment can serve several callers on their own keys.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import tempfile
import time
import uuid
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path
from platform import python_version
from typing import Annotated, Any, Literal

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

BASE_URL = os.environ.get("OXYLABS_BASE_URL", "https://webapi.oxylabs.io").rstrip("/")
TIMEOUT = float(os.environ.get("OXYLABS_TIMEOUT", "120"))

# JavaScript rendering routinely runs past a normal request timeout, so those calls are
# run as background jobs with a timeout of their own.
JS_TIMEOUT = float(os.environ.get("OXYLABS_JS_TIMEOUT", "300"))
JOB_TTL_SECONDS = float(os.environ.get("OXYLABS_JOB_TTL_MINUTES", "60")) * 60

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

# Endpoint names come from `GET /v1/scrapers`; this keeps a caller from walking the path.
ENDPOINT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

try:
    VERSION = _package_version("oxylabs-web-api-mcp")
except PackageNotFoundError:  # running from a checkout that was never installed
    VERSION = "0.0.0+local"

# Every tool here only reads: nothing it calls creates, changes or deletes anything the
# caller owns. Clients use readOnlyHint to decide what can run without a prompt.
NETWORK_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

mcp = MCPServer(
    name="oxylabs-web-api",
    version=VERSION,
    instructions=(
        "Access the live web through the Oxylabs Web API. Use `search` to find URLs for a "
        "question, then `scrape` to read the full content of the URLs worth reading, or "
        "`extract` when you want specific fields back as JSON rather than a page to read. "
        "Prefer search+scrape over answering from memory whenever freshness matters. "
        "Anything rendered with JavaScript comes back as a job id to poll with "
        "`check_scrape`."
    ),
)


class ApiError(RuntimeError):
    """Raised with a message an agent can act on rather than a raw stack trace."""


# ------------------------------------------------------------------------------ params

URL_PARAM = Annotated[str, Field(description="Absolute http(s) URL of the page to read.")]
LOCATION_CODE_PARAM = Annotated[
    str | None,
    Field(
        description=(
            "Two-letter country code to fetch the page from. Use it when the page varies "
            "by country — pricing, availability, language."
        ),
        examples=["DE", "US", "LT"],
    ),
]
RUN_JS_PARAM = Annotated[
    bool,
    Field(
        description=(
            "Execute the page's JavaScript. Needed for pages that render client-side and "
            "arrive empty otherwise. Slow: this returns a job id to poll with "
            "`check_scrape` instead of the content. Try without it first."
        )
    ),
]
CHECK_EMPTY_GEO_PARAM = Annotated[
    bool,
    Field(
        description=(
            "Fail the request rather than return content that ignored `location`. Use it "
            "when content for the wrong market would be worse than an error."
        )
    ),
]


# -------------------------------------------------------------------------------- http


def _api_key(ctx: Context | None = None) -> str:
    """The Bearer key, from the request headers over HTTP or the environment on stdio.

    Header auth is what lets a single HTTP deployment serve several callers on their own
    keys; stdio carries no headers, so it reads the environment.
    """
    if ctx is not None:
        header = (ctx.headers or {}).get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()

    key = os.environ.get("OXYLABS_API_KEY", "").strip()
    if not key:
        raise ApiError(
            "No Oxylabs Web API key. Send it as an 'Authorization: Bearer <key>' header, "
            "or set OXYLABS_API_KEY in the server environment (see .env.example) and "
            "restart the MCP server."
        )
    return key


def _sdk_header(ctx: Context | None) -> str:
    """Identify this server, and the client driving it, to the API."""
    client = "oxylabs-web-api-mcp"
    try:
        name = ctx.request_context.session.client_params.clientInfo.name  # type: ignore[union-attr]
        client = f"{client}-{name}"
    except Exception:  # noqa: BLE001 — telemetry must never be the reason a call fails
        pass
    return f"{client}/{VERSION} (python {python_version()})"


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
        raise ApiError("Authentication failed (401). Check the API key.")
    if resp.status_code == 429:
        raise ApiError("Rate limited (429). Back off and retry with fewer concurrent requests.")
    if resp.status_code >= 400:
        raise ApiError(f"{path} returned {resp.status_code}: {_detail(resp)}")
    try:
        return resp.json()
    except ValueError:
        # OPTIONS on an endpoint is not guaranteed to answer with JSON.
        return {"raw": resp.text[:20000]}


async def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    ctx: Context | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Call the Web API, turning transport failures into agent-readable errors."""
    headers = {
        "Authorization": f"Bearer {api_key or _api_key(ctx)}",
        "x-oxylabs-sdk": _sdk_header(ctx),
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    seconds = TIMEOUT if timeout is None else timeout
    try:
        async with httpx.AsyncClient(timeout=seconds) as client:
            resp = await client.request(method, f"{BASE_URL}{path}", json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise ApiError(
            f"Request to {path} timed out after {seconds:.0f}s. Retry, or narrow the request."
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiError(f"Could not reach the Oxylabs Web API at {BASE_URL}{path}: {exc}") from exc
    return _check(resp, path)


async def _note(ctx: Context | None, message: str) -> None:
    """Tell the client what is happening. Never worth failing a call over."""
    if ctx is None:
        return
    try:
        await ctx.info(message)
    except Exception:  # noqa: BLE001
        pass


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


def _trim(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop envelope fields the agent gains nothing from re-reading.

    `params` echoes the request that was just sent, and `metadata` is mostly timestamps.
    The request id stays — it is what support needs to trace a call.
    """
    if not isinstance(payload, dict):
        return payload
    trimmed = {k: v for k, v in payload.items() if k not in ("params", "metadata")}
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata.get("request_id"):
        trimmed["request_id"] = metadata["request_id"]
    return trimmed


# ------------------------------------------------------------------------------- jobs

# ponytail: jobs live in this process only — they do not survive a restart and are not
# shared between HTTP replicas. Move them to a store the day this runs behind more than
# one replica; until then a dict is the whole feature.
_JOBS: dict[str, dict[str, Any]] = {}


def _prune_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    for job_id in [j for j, record in _JOBS.items() if record["created"] < cutoff]:
        _JOBS.pop(job_id, None)


async def _run_job(job_id: str, path: str, payload: dict[str, Any], fmt: str, key: str) -> None:
    record = _JOBS[job_id]
    try:
        response = await _request("POST", path, payload, api_key=key, timeout=JS_TIMEOUT)
        record["result"] = _trim(_process_content(response, fmt))
        record["status"] = "done"
    except ApiError as exc:
        record["status"] = "error"
        record["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — a crashed task must not stay "running" forever
        record["status"] = "error"
        record["error"] = f"Unexpected failure: {exc}"
    record["finished"] = time.time()


def _submit_job(path: str, payload: dict[str, Any], fmt: str, key: str) -> dict[str, Any]:
    """Start a slow render in the background and hand the agent something to poll."""
    _prune_jobs()
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {
        "status": "running",
        "created": time.time(),
        "url": payload.get("url", ""),
        "result": None,
        "error": None,
    }
    # Keep the reference: a bare create_task can be garbage-collected mid-flight.
    _JOBS[job_id]["task"] = asyncio.create_task(_run_job(job_id, path, payload, fmt, key))
    return {
        "job_id": job_id,
        "status": "running",
        "url": payload.get("url", ""),
        "note": (
            "JavaScript rendering takes at least 30 seconds. Wait ~30s, then call "
            f"check_scrape('{job_id}') and keep polling every ~15s while it says running. "
            f"The result is kept for {JOB_TTL_SECONDS / 60:.0f} minutes. Get on with other "
            "work in the meantime rather than idling on the poll."
        ),
    }


# ------------------------------------------------------------------------------- tools


@mcp.tool(annotations=NETWORK_READ)
async def search(
    query: Annotated[
        str,
        Field(
            description=(
                "What to search for, as a natural search phrase. Keyword-shaped queries "
                "beat sentence-shaped ones. One question per search."
            ),
            examples=["eu ai act compliance deadlines", "figma pricing per seat"],
        ),
    ],
    max_results: Annotated[
        int, Field(description="Number of results to return.", ge=1, le=20)
    ] = 10,
    location: Annotated[
        str | None,
        Field(
            description=(
                "Geographic context for the search, from a country to a full locality. "
                "Pass it whenever the answer is geographic — an unrecognised value is not "
                "validated upstream and silently falls back to the default geo."
            ),
            examples=["Germany", "New York,New York,United States"],
        ),
    ] = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Search the live web and return ranked organic results.

    Returns titles, short descriptions and URLs — not full page content. Follow up with
    `scrape` on the URLs you actually need to read.
    """
    if not query.strip():
        raise ApiError("`query` must not be empty.")
    if not 1 <= max_results <= 20:
        raise ApiError("`max_results` must be an integer between 1 and 20.")

    payload: dict[str, Any] = {"query": query, "max_results": max_results}
    if location:
        payload["location"] = location

    await _note(ctx, f"Searching: {query!r}" + (f" ({location})" if location else ""))
    return _trim(await _request("POST", "/v1/search", payload, ctx=ctx))


@mcp.tool(annotations=NETWORK_READ)
async def scrape(
    url: URL_PARAM,
    format: Annotated[
        Literal["markdown", "html"],
        Field(
            description=(
                "markdown to read the page — far fewer tokens, structure intact. html "
                "only when you need the markup itself: attributes, embedded JSON-LD."
            )
        ),
    ] = "markdown",
    location: LOCATION_CODE_PARAM = None,
    device: Annotated[
        Literal["desktop", "mobile"] | None,
        Field(description="Viewport to fetch as. Upstream default is desktop."),
    ] = None,
    run_js: RUN_JS_PARAM = False,
    check_empty_geo: CHECK_EMPTY_GEO_PARAM = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Fetch and read a single URL, including JavaScript-heavy and bot-protected pages.

    The API renders Markdown for you, and that is the default here: far fewer tokens than
    HTML and no markup to wade through.

    With `run_js` this returns a job id rather than content — rendering is too slow to
    hold a tool call open. Poll it with `check_scrape`.

    Very large pages are not returned inline. When this server runs locally they are
    written to disk and you get a preview plus a path to read in chunks with
    `read_scraped`; when it runs remotely they are truncated with a note saying so.
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
    if check_empty_geo:
        payload["check_empty_geo"] = True

    if run_js:
        payload["run_js"] = True
        await _note(ctx, f"Rendering {url} with JavaScript in the background")
        return _submit_job("/v1/scrape", payload, format, _api_key(ctx))

    await _note(ctx, f"Scraping {url} as {format}")
    return _trim(_process_content(await _request("POST", "/v1/scrape", payload, ctx=ctx), format))


class _ExtractApproval(BaseModel):
    """What the user is asked before a billed structured extraction runs."""

    proceed: bool = Field(
        description="Run the structured extraction? It is billed above a plain scrape."
    )


@mcp.tool(annotations=NETWORK_READ)
async def extract(
    url: URL_PARAM,
    prompt: Annotated[
        str,
        Field(
            description=(
                "The fields to pull out of the page, in plain words. Name them and say "
                "what shape you want."
            ),
            examples=["Parse the product title, price, and a list of image links"],
        ),
    ],
    location: LOCATION_CODE_PARAM = None,
    run_js: RUN_JS_PARAM = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Pull named fields off a page as JSON, without writing selectors.

    Costs more than `scrape` — the page is parsed by a model, per call — so the user is
    asked to approve each run. Scrape the page and read it yourself when a page you were
    going to read anyway would answer the question; use this when you want the fields
    themselves, in a shape you can compute on.
    """
    if not url.startswith(("http://", "https://")):
        raise ApiError(f"`url` must be an absolute http(s) URL, got: {url!r}")
    if not prompt.strip():
        raise ApiError("`prompt` must describe the fields to extract.")

    await _confirm_extract(url, prompt, ctx)

    payload: dict[str, Any] = {"url": url, OUTPUT_PARAM: ["json"], "json": {"prompt": prompt}}
    if location:
        payload["location"] = location

    if run_js:
        payload["run_js"] = True
        await _note(ctx, f"Extracting from {url} with JavaScript in the background")
        return _submit_job("/v1/scrape", payload, "json", _api_key(ctx))

    await _note(ctx, f"Extracting from {url}")
    return _trim(_process_content(await _request("POST", "/v1/scrape", payload, ctx=ctx), "json"))


async def _confirm_extract(url: str, prompt: str, ctx: Context | None) -> None:
    """Get the user's go-ahead. Structured extraction is billed above a plain scrape."""
    if os.environ.get("OXYLABS_EXTRACT_APPROVAL", "1") == "0":
        return

    capabilities = ctx.client_capabilities if ctx is not None else None
    if getattr(capabilities, "elicitation", None) is None:
        raise ApiError(
            "`extract` needs the user's approval because it is billed above a plain "
            "scrape, and this client cannot ask them (no elicitation support). Use "
            "`scrape` and read the page, or set OXYLABS_EXTRACT_APPROVAL=0 in the server "
            "environment once the user has agreed to the cost."
        )

    result = await ctx.elicit(  # type: ignore[union-attr]
        message=f"Structured extraction of {url} ({prompt!r}) is billed above a plain scrape.",
        schema=_ExtractApproval,
    )
    if result.action != "accept" or not result.data.proceed:
        raise ApiError(
            "The user declined the extraction. Do not retry it — use `scrape` to read the "
            "page instead, or ask them what they would prefer."
        )


@mcp.tool(annotations=LOCAL_READ)
async def check_scrape(
    job_id: Annotated[str, Field(description="The `job_id` returned by a `run_js` call.")],
) -> dict[str, Any]:
    """Check a JavaScript-rendering job started by `scrape` or `extract`.

    While it says running, poll again in ~15 seconds rather than immediately — and do
    other work between polls. The finished result is returned in full, once it is ready.
    """
    record = _JOBS.get(job_id)
    if record is None:
        raise ApiError(
            f"No job {job_id!r}. Either it expired (results are kept for "
            f"{JOB_TTL_SECONDS / 60:.0f} minutes), or the server restarted. Start it again."
        )

    elapsed = round(time.time() - record["created"])
    if record["status"] == "running":
        return {
            "job_id": job_id,
            "status": "running",
            "url": record["url"],
            "elapsed_seconds": elapsed,
            "note": "Not finished. Poll again in ~15s.",
        }
    if record["status"] == "error":
        raise ApiError(f"Job {job_id} failed after {elapsed}s: {record['error']}")

    return {
        "job_id": job_id,
        "status": "done",
        "elapsed_seconds": elapsed,
        "result": record["result"],
    }


@mcp.tool(annotations=LOCAL_READ)
async def read_scraped(
    path: Annotated[str, Field(description="Path from a result's `content_offloaded.path`.")],
    offset: Annotated[int, Field(description="Character offset to start at.", ge=0)] = 0,
    length: Annotated[
        int, Field(description="How many characters to return.", ge=1)
    ] = MAX_INLINE_CHARS,
) -> dict[str, Any]:
    """Read a chunk of a scraped page that was offloaded to disk.

    Use the `path` from a scrape result's `content_offloaded`. Start at offset 0 and keep
    calling with the returned `next_offset` until `eof` is true — and stop as soon as you
    have what you need rather than reading the whole file by reflex.
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


@mcp.tool(annotations=NETWORK_READ)
async def list_scrapers(
    endpoint: Annotated[
        str | None,
        Field(
            description=(
                "Name a scrape endpoint to get its parameters and their types instead of "
                "the list. Omit it to list what exists."
            ),
            examples=["scrape"],
        ),
    ] = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List the scrape endpoints this API implements, or describe one of them.

    Call this before assuming a dedicated scraper does or does not exist for a target,
    then call it again with the endpoint name to see what that endpoint accepts. That is
    the authoritative parameter list — more current than any documentation. Run the
    endpoint itself with `scrape_target`.
    """
    if endpoint is None:
        return await _request("GET", "/v1/scrapers", ctx=ctx)

    name = _endpoint_name(endpoint)
    await _note(ctx, f"Describing /v1/{name}")
    return await _request("OPTIONS", f"/v1/{name}", ctx=ctx)


@mcp.tool(annotations=NETWORK_READ)
async def scrape_target(
    endpoint: Annotated[
        str,
        Field(description="A scrape endpoint name from `list_scrapers`, without the /v1/ prefix."),
    ],
    params: Annotated[
        dict[str, Any],
        Field(
            description=(
                "The endpoint's own request body. Get the accepted keys and types from "
                "`list_scrapers(endpoint)` rather than guessing them."
            )
        ),
    ],
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Call a target-specific scrape endpoint with its own parameters.

    The generic `scrape` tool reads any URL. This runs the dedicated scrapers instead —
    the ones with pagination, store context, sort order and the rest. Look the endpoint up
    with `list_scrapers()`, read its parameters with `list_scrapers(endpoint)`, then call
    it here. A `run_js` in `params` returns a job id to poll, same as `scrape`.
    """
    name = _endpoint_name(endpoint)
    if not isinstance(params, dict):
        raise ApiError("`params` must be an object of request fields for the endpoint.")

    fmt = "markdown"
    output = params.get(OUTPUT_PARAM)
    if isinstance(output, list) and output:
        fmt = str(output[0])

    if params.get("run_js"):
        await _note(ctx, f"Running /v1/{name} with JavaScript in the background")
        return _submit_job(f"/v1/{name}", params, fmt, _api_key(ctx))

    await _note(ctx, f"Running /v1/{name}")
    return _trim(_process_content(await _request("POST", f"/v1/{name}", params, ctx=ctx), fmt))


def _endpoint_name(endpoint: str) -> str:
    """Accept an endpoint name, not a path — this must not become a URL builder."""
    name = endpoint.strip().strip("/")
    if name.startswith("v1/"):
        name = name[3:]
    if not ENDPOINT_RE.match(name):
        raise ApiError(
            f"{endpoint!r} is not an endpoint name. Pass one of the names from "
            "`list_scrapers()`, e.g. 'scrape'."
        )
    return name


# ---------------------------------------------------------------------------- transport


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

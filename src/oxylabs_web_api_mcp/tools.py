"""The tools agents call."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import Context
from pydantic import Field

from . import client, config
from .app import _local_read, _network_read, mcp
from .content import (
    _flag_thin_content,
    _output_format,
    _pop_screenshots,
    _process_content,
    _spill_dir,
    _token_budget,
    _trim,
)
from .errors import ApiError, ApiFaulted
from .scraping import (
    _dedicated_failure,
    _dedicated_note,
    _extract,
    _label,
    _parser_of,
    _route,
    _scrape,
)

JSON_PROMPT_PARAM = Annotated[
    str | None,
    Field(
        description=(
            "Fields to pull out as JSON, in plain words: name them and say what shape you "
            "want. Used by the AI parser, which runs when the endpoint has no dedicated "
            "parser or its dedicated parser did not return them. Leave unset to read the "
            "content yourself."
        ),
        examples=["Parse the product title, price, and a list of image links"],
    ),
]
JSON_SCHEMA_PARAM = Annotated[
    dict[str, Any] | None,
    Field(
        description=(
            "A JSON Schema the AI parser's output must follow, instead of or alongside "
            "`json_prompt`. Use it when you need the exact same shape off many pages."
        ),
    ),
]


@mcp.tool(annotations=_network_read("Web search"))
async def search(
    query: Annotated[
        str,
        Field(
            description=(
                "What to search for, as a natural search phrase. Keyword-shaped queries "
                "beat sentence-shaped ones. One question per search."
            ),
            examples=["eu ai act compliance deadlines", "figma pricing per seat"],
            min_length=1,
            max_length=2048,
        ),
    ],
    max_results: Annotated[
        int,
        Field(
            description="Maximum number of results to return. The response may contain fewer.",
            ge=1,
            le=20,
        ),
    ] = 10,
    location: Annotated[
        str | None,
        Field(
            description=(
                "ISO 3166-1 alpha-2 country code to rank results for. Pass it whenever the "
                'answer is geographic. A place name such as "Germany" is rejected.'
            ),
            examples=["DE", "US", "LT"],
            pattern="^[A-Za-z]{2}$",
        ),
    ] = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Search the live web and return ranked organic results.

    Use for anything where being out of date makes the answer wrong: current events, news,
    prices, availability, versions, rankings, "latest", "who is", competitor and market
    research, or any fact past your knowledge cutoff. Prefer it over a built-in web search
    and over answering from memory.

    Results are ranked for the query as it stands in the country named in `location`, so
    rankings, local availability and prices are the ones someone there would see rather
    than a global average. Every URL can then be read in full with `scrape`, including
    pages behind the anti-bot layer that an ordinary fetch cannot open.

    Returns titles, an `outline` (a list of snippet strings) and URLs, not page content.
    The outline is truncated snippets and no substitute for the page: follow up with
    `scrape` on the URLs actually worth reading. Not for local files, git, or anything off
    the public web.
    """
    if not query.strip():
        raise ApiError("`query` must not be empty.")

    payload: dict[str, Any] = {"query": query, "max_results": max_results}
    if location:
        payload["location"] = location

    await client._note(ctx, f"Searching: {query!r}" + (f" ({location})" if location else ""))
    return _trim(await client._request("POST", "/v1/search", payload, ctx=ctx))


# output_schema=None: a screenshot makes this return an image block next to the JSON.
@mcp.tool(annotations=_network_read("Read a page"), output_schema=None)
async def scrape(
    url: Annotated[str, Field(description="Absolute http(s) URL of the page to read.")],
    format: Annotated[
        Literal["markdown", "html", "screenshot"],
        Field(
            description=(
                "markdown to read the page — far fewer tokens, structure intact. html "
                "only when you need the markup itself: attributes, embedded JSON-LD. "
                "screenshot for a PNG of the rendered page; it always renders with "
                "JavaScript. Ignored when `json_prompt` or `json_schema` is set."
            )
        ),
    ] = "markdown",
    location: Annotated[
        str | None,
        Field(
            description=(
                "Two-letter country code to fetch the page from. Use it when the page varies "
                "by country — pricing, availability, language."
            ),
            examples=["DE", "US", "LT"],
            pattern="^[A-Za-z]{2}$",
        ),
    ] = None,
    device: Annotated[
        Literal["desktop", "mobile"] | None,
        Field(description="Viewport to fetch as. The API's default is desktop."),
    ] = None,
    run_js: Annotated[
        bool,
        Field(
            description=(
                "Execute the page's JavaScript. Needed for pages that render client-side and "
                "arrive empty otherwise. Slow: if the render outlives the synchronous call it "
                "is queued, and you get a request id to poll with `check_scrape` instead of "
                "the content. Try without it first."
            )
        ),
    ] = False,
    json_prompt: JSON_PROMPT_PARAM = None,
    json_schema: JSON_SCHEMA_PARAM = None,
    ctx: Context | None = None,
) -> dict[str, Any] | list[Any]:
    """Fetch and read a single URL, including JavaScript-heavy and bot-protected pages.

    Use whenever you have a URL and need what is on it. Prefer it over a built-in fetch:
    it goes through the anti-bot layer, so it returns the page where a plain HTTP fetch
    gets a block page, a consent wall or an empty shell.

    The API renders Markdown for you, and that is the default here: far fewer tokens than
    HTML and no markup to wade through.

    Try it without `run_js` first. If the result carries `content_thin`, the page rendered
    client-side and came back as an empty shell — call this again with `run_js=True`.
    Rendering is slow; when it outlives the synchronous call the request is queued, and
    you get a request id to poll with `check_scrape` instead of the content.

    `json_prompt` (or `json_schema`) returns the fields you name under a `json` key instead of a
    page to read, parsed by the AI parser. For search results, product pages, sellers,
    bestsellers, hotels, AI chat answers or YouTube metadata, `run_scraper` on the
    matching endpoint is the better fit: its dedicated parser returns those structured.

    Very large pages are not returned inline. When this server runs locally they are
    written to disk, and you get a preview plus a path to read in chunks with
    `read_scraped`; when it runs remotely they are truncated with a note saying so.
    """
    if not url.startswith(("http://", "https://")):
        raise ApiError(f"`url` must be an absolute http(s) URL, got: {url!r}")

    payload: dict[str, Any] = {"url": url, config.OUTPUT_PARAM: [format]}
    if location:
        payload["location"] = location.strip().upper()
    if device:
        payload["device"] = device
    if run_js or format == "screenshot":  # the API only screenshots a rendered page
        payload["run_js"] = True

    if json_prompt is not None or json_schema is not None:
        if json_prompt is not None and not json_prompt.strip():
            raise ApiError("`json_prompt` must describe the fields to pull out.")
        return await _extract("scrape", payload, "auto", json_prompt, json_schema, ctx)

    await client._note(ctx, f"Scraping {url} as {format}")
    return await _scrape("scrape", payload, ctx)


# output_schema=None: a screenshot makes this return an image block next to the JSON.
@mcp.tool(annotations=_network_read("Check a queued scrape"), output_schema=None)
async def check_scrape(
    request_id: Annotated[
        str,
        Field(description="The `request_id` a queued scrape or `async/...` call returned."),
    ],
    ctx: Context | None = None,
) -> dict[str, Any] | list[Any]:
    """Collect a scrape the API is running in the background.

    While it says pending, call it again every 5 seconds. The finished result is returned
    in full.
    """
    if not request_id.isdigit():
        raise ApiError(
            f"{request_id!r} is not a request id. Use the `request_id` a queued call returned."
        )
    # Every /v1/async/... request, whatever endpoint queued it, is collected from this one path.
    path = f"/v1/async/scrape/{request_id}"
    retry = "call run_scraper again with parser='ai' and `json_prompt` naming the fields"
    try:
        body = await client._request("GET", path, ctx=ctx)
    except ApiFaulted as exc:
        if exc.tag.startswith("PARSE_") and _parser_of(exc.params) == "dedicated":
            raise ApiFaulted(
                f"{exc} The dedicated parser failed; {retry}.", tag=exc.tag, params=exc.params
            ) from exc
        raise
    except ApiError as exc:
        if "returned 404" in str(exc):
            raise ApiError(
                f"No request {request_id!r}: the API does not know this request id. Either it "
                "expired or it was never a valid one. Start it again."
            ) from exc
        raise

    state = client._state(body)
    if state != "done":
        return {
            "request_id": request_id,
            "state": state or "pending",
            "note": f"Not finished. Poll again in {config.POLL_SECONDS}s.",
        }
    parser = _parser_of(body.get("params"))
    failure = _dedicated_failure(body) if parser == "dedicated" else ""
    body = _process_content(body, _output_format(body.get("params")), _token_budget())
    result = _trim(_flag_thin_content(body))
    reply: dict[str, Any] = {"request_id": request_id, "state": "done", "result": result}
    if failure:
        _label(reply, parser, f"The dedicated parser failed ({failure}); {retry}.")
    elif parser == "dedicated":
        _label(reply, parser, _dedicated_note(result, retry))
    elif parser == "ai":
        _label(reply, parser, "Extracted by the AI parser from your prompt or schema.")
    return _pop_screenshots(reply, result)


@mcp.tool(annotations=_local_read("Read an offloaded page"))
async def read_scraped(
    path: Annotated[str, Field(description="Path from a result's `content_offloaded.path`.")],
    offset: Annotated[int, Field(description="Character offset to start at.", ge=0)] = 0,
    length: Annotated[
        int, Field(description="How many characters to return.", ge=1)
    ] = config.READ_CHUNK_CHARS,
) -> dict[str, Any]:
    """Read a chunk of a scraped page that was offloaded to disk.

    Use the `path` from a scrape result's `content_offloaded`. Start at offset 0 and keep
    calling with the returned `next_offset` until `eof` is true — and stop as soon as you
    have what you need rather than reading the whole file by reflex. Returns `text` plus
    `offset`, `returned_chars`, `total_chars`, `next_offset` and `eof`.
    """
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


@mcp.tool(annotations=_network_read("List scrape endpoints"))
async def list_scrapers(
    endpoint: Annotated[
        str | None,
        Field(
            description=(
                "Name a scrape endpoint to get the JSON Schema of its request body instead "
                "of the list. Omit it to list what exists."
            ),
            examples=["scrape/amazon/search"],
        ),
    ] = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """List the scrape endpoints this API implements, or describe one of them.

    Not needed to read a URL — `scrape` does that for any site. Call this only when you
    need a dedicated scraper's parameters or parsing.

    The list is grouped by parser: `json_supported` endpoints have a dedicated parser (and
    take the AI parser too), `json_supported_only_with_prompt_or_schema` ones take only the
    AI parser, `json_not_supported` ones return no JSON at all.

    Call this before assuming a dedicated scraper does or does not exist for a site,
    then call it again with the endpoint name to get the JSON Schema of what that endpoint
    accepts: types, `required`, `enum` values and defaults. That is the authoritative
    parameter list — more current than any documentation. Run the endpoint itself with
    `run_scraper`.
    """
    if endpoint is None:
        return await client._request("GET", "/v1/scrapers?group_by=json_output_support", ctx=ctx)

    name = _endpoint_name(endpoint)
    await client._note(ctx, f"Describing /v1/{name}")
    return await client._request("OPTIONS", f"/v1/{name}?format=json_schema", ctx=ctx)


@mcp.tool(annotations=_network_read("Run a dedicated scraper"), output_schema=None)
async def run_scraper(
    endpoint: Annotated[
        str,
        Field(
            description=(
                "A scrape endpoint path from `list_scrapers`, without the /v1/ prefix, "
                "e.g. 'scrape/amazon/search'."
            )
        ),
    ],
    params: Annotated[
        dict[str, Any],
        Field(
            description=(
                "The endpoint's own request body. Get its JSON Schema from "
                "`list_scrapers(endpoint)` rather than guessing the keys."
            )
        ),
    ],
    structured: Annotated[
        bool,
        Field(
            description=(
                "Return parsed JSON instead of page content. Implied by `json_prompt` or "
                "`json_schema`. The endpoint's dedicated parser runs first when it has one."
            )
        ),
    ] = False,
    parser: Annotated[
        Literal["auto", "ai"],
        Field(
            description=(
                "auto uses the endpoint's dedicated parser when it has one and falls back "
                "to the AI parser when that fails or returns nothing. ai goes straight to the AI "
                "parser — use it when a dedicated result lacked the fields you need."
            )
        ),
    ] = "auto",
    json_prompt: JSON_PROMPT_PARAM = None,
    json_schema: JSON_SCHEMA_PARAM = None,
    ctx: Context | None = None,
) -> dict[str, Any] | list[Any]:
    """Run a dedicated scrape endpoint with its own parameters.

    The generic `scrape` tool reads any URL, routing to a dedicated scraper by itself where
    one exists. Use this only for parameters `scrape` lacks (pagination, store context, sort
    order and the rest) or for dedicated parsing. Look the endpoint up
    with `list_scrapers()`, read its parameters with `list_scrapers(endpoint)`, then call
    it here.

    For structured data, set `structured=True` and describe the fields in `json_prompt`.
    Endpoints with a dedicated parser return typical page content in a predefined shape:
    search results (organic links, ads, images, news, videos, SERP extras), product
    listings and details (title, price, stock, seller, reviews), seller profiles,
    bestsellers, hotel offers, AI chat answers (prompt, response, citations), YouTube
    video and channel metadata. That runs first; with no dedicated parser, or one that
    failed or returned nothing, the AI parser extracts what `json_prompt` describes. The
    result's `parser` says which one answered, and `parser_note` carries the dedicated
    parser's own verdict (e.g. PARSE_PARTIAL_SUCCESS_SOME_FIELDS_DEFAULT). If a dedicated result
    lacks what you need, call again with `parser="ai"`.

    Calls run synchronously. One that times out is sent to the corresponding asynchronous
    endpoint (`/v1/async/...`) and returns a `request_id` to poll with `check_scrape`; an
    `async/...` endpoint, or one `list_scrapers` lists only in async form, is always queued.
    """
    name = await _route(_endpoint_name(endpoint), ctx)
    output = params.get(config.OUTPUT_PARAM)
    wants_json = isinstance(output, list) and "json" in output

    if structured or wants_json or json_prompt is not None or json_schema is not None:
        spec = (
            params.get(config.JSON_PARAM) if isinstance(params.get(config.JSON_PARAM), dict) else {}
        )
        prompt = json_prompt if json_prompt is not None else spec.get("prompt")
        schema = json_schema if json_schema is not None else spec.get("schema")

        if prompt is not None and not str(prompt).strip():
            raise ApiError("`json_prompt` must describe the fields to pull out.")

        return await _extract(name, params, parser, prompt, schema, ctx)

    return await _scrape(name, params, ctx)


def _endpoint_name(endpoint: str) -> str:
    """Accept a source path from `list_scrapers`, not a URL — every segment is checked."""
    name = endpoint.strip().strip("/")
    if name.startswith("v1/"):
        name = name[3:]
    if not name or not all(config.ENDPOINT_SEGMENT_RE.match(seg) for seg in name.split("/")):
        raise ApiError(
            f"{endpoint!r} is not an endpoint name. Pass one of the paths from "
            "`list_scrapers()` without the /v1/ prefix, e.g. 'scrape/amazon/search'."
        )
    return name

"""The tools agents call."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import Context
from pydantic import Field

from . import client, config
from .app import _network_read, mcp
from .content import _prefix_within, _token_budget, _trim
from .errors import ApiError
from .results import (
    _check_request_id,
    _Entry,
    _page_text,
    _reply,
    _scrape_many,
    _wait,
)
from .scraping import _extract, _json_support, _output_formats, _route, _scrape

JSON_PROMPT_PARAM = Annotated[
    str | None,
    Field(
        description=(
            "Fields to pull out as JSON, in plain words: name them and say what shape you "
            "want. Used by the AI parser, which runs when the endpoint has no dedicated "
            "parser or its dedicated parser did not return them. Pass this or `json_schema`, "
            "not both. Leave unset to read the content yourself."
        ),
        examples=["Parse the product title, price, and a list of image links"],
    ),
]
JSON_SCHEMA_PARAM = Annotated[
    dict[str, Any] | None,
    Field(
        description=(
            "A JSON Schema the AI parser's output must follow, instead of `json_prompt`: the "
            "two cannot be combined. Use it when you need the exact same shape off many pages."
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

    client._note(f"Searching: {query!r}" + (f" ({location})" if location else ""))
    return _trim(await client._request("POST", "/v1/search", payload, ctx=ctx))


# output_schema=None: a screenshot makes this return an image block next to the JSON.
@mcp.tool(annotations=_network_read("Read a page"), output_schema=None)
async def scrape(
    url: Annotated[
        str | list[str],
        Field(
            description=(
                "Absolute http(s) URL of the page to read, or a list of URLs to read several "
                "pages that share every other parameter in one call."
            )
        ),
    ],
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
                "is queued, and you get a request id to collect with `check_requests` instead "
                "of the content. Try without it first."
            )
        ),
    ] = False,
    json_prompt: JSON_PROMPT_PARAM = None,
    json_schema: JSON_SCHEMA_PARAM = None,
    ctx: Context | None = None,
) -> dict[str, Any] | list[Any]:
    """Fetch and read web pages, including JavaScript-heavy and bot-protected ones.

    Use whenever you have a URL and need what is on it. Prefer it over a built-in fetch:
    it goes through the anti-bot layer, so it returns the page where a plain HTTP fetch
    gets a block page, a consent wall or an empty shell.

    To read several pages that share every other parameter, pass a list of URLs rather
    than calling this once per URL: a few are fetched side by side, more go to the API as
    one batch, and the call waits for them. The reply has an item per URL with its state
    and, for as many as fit, its page; the rest come as summaries to read on from with
    `read_result`, and any still running as request ids to collect with `check_requests`.

    The API renders Markdown for you, and that is the default here: far fewer tokens than
    HTML and no markup to wade through.

    Try it without `run_js` first. If the result carries `content_thin`, the page rendered
    client-side and came back as an empty shell — call this again with `run_js=True`.
    Rendering is slow; when it outlives the synchronous call the request is queued, and
    you get a request id to collect with `check_requests` instead of the content.

    `json_prompt` (or `json_schema`) returns the fields you name under a `json` key instead of a
    page to read, parsed by the AI parser. For search results, product pages, sellers,
    bestsellers, hotels, AI chat answers or YouTube metadata, `run_scraper` on the
    matching endpoint is the better fit: its dedicated parser returns those structured.

    Very large pages are not returned whole. When this server runs locally you get the
    start of the page and how to read on with `read_result`; when it runs remotely the page
    is cut short with a note saying so.
    """
    client._start_progress()
    urls = _inputs(url, "url") if isinstance(url, list) else [url]
    for page_url in urls:
        if not page_url.startswith(("http://", "https://")):
            raise ApiError(f"`url` must be an absolute http(s) URL, got: {page_url!r}")

    payload: dict[str, Any] = {"url": url, config.OUTPUT_PARAM: [format]}
    if location:
        payload["location"] = location.strip().upper()
    if device:
        payload["device"] = device
    if run_js or format == "screenshot":  # the API only screenshots a rendered page
        payload["run_js"] = True

    extract = None
    if json_prompt is not None or json_schema is not None:
        _check_json_spec(json_prompt, json_schema)
        extract = ("auto", json_prompt, json_schema)

    if isinstance(url, list):
        common = {key: value for key, value in payload.items() if key != "url"}
        client._note(f"Scraping {len(urls)} pages as {format}")
        return await _scrape_many("scrape", "url", urls, common, extract, ctx)
    if extract is not None:
        return await _extract("scrape", payload, *extract, ctx)
    client._note(f"Scraping {url} as {format}")
    return await _scrape("scrape", payload, ctx)


# output_schema=None: screenshots come back as image blocks next to the JSON.
@mcp.tool(annotations=_network_read("Collect queued requests"), output_schema=None)
async def check_requests(
    request_ids: Annotated[
        list[str],
        Field(
            description=(
                "The `request_id`s of queued requests, from `scrape` or `run_scraper` "
                "replies or an earlier call to this tool. A single id is fine."
            ),
            min_length=1,
            max_length=config.CHECK_REQUESTS_LIMIT,
        ),
    ],
    ctx: Context | None = None,
) -> dict[str, Any] | list[Any]:
    """Collect requests the API is running in the background.

    Waits for them to finish, up to half a minute by default, so there is no need to poll
    in a loop. Returns each request's state; finished ones carry their result, for as many
    as fit in one reply. The rest come as summaries marked `summarized`, holding as much of
    each page as there was room for: read on with `read_result`. A parser note comes once,
    in `parser_notes`, and each result names its own. Only the first few screenshots come
    back as images. If some are still pending, call again with only their request ids.
    """
    client._start_progress()
    ids = list(dict.fromkeys(request_ids))
    for request_id in ids:
        _check_request_id(request_id)
    entries = [_Entry(request_id=request_id, pending=True) for request_id in ids]
    await _wait(entries, ctx, strict=True)
    return _reply(entries)


@mcp.tool(annotations=_network_read("Read a long result in parts"))
async def read_result(
    request_id: Annotated[str, Field(description="The `request_id` of the result to read.")],
    page: Annotated[
        int,
        Field(
            description=(
                "Which page of the result, counting from 0. Most results have one; a request "
                "for several pages of results, a search say, has more."
            ),
            ge=0,
        ),
    ] = 0,
    offset: Annotated[
        int,
        Field(
            description=(
                "Character to start at: the `next_offset` a summary, a cut page or the "
                "previous part gave."
            ),
            ge=0,
        ),
    ] = 0,
    length: Annotated[
        int,
        Field(
            description="Most characters to return; fewer come back if they would not fit.",
            ge=1,
        ),
    ] = config.READ_CHUNK_CHARS,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read a long page of a result in parts, from where its reply cut it short.

    Use the `request_id`, `page` and `next_offset` that a summary, a cut page or the
    previous part gave, and keep calling with the `next_offset` each part returns until
    `eof` is true — and stop as soon as you have what you need rather than reading the
    whole page by reflex. Reading runs no new scrape: the page comes from disk where this
    server wrote it, or from the API, which keeps the results of queued requests. Returns
    `text` plus `offset`, `returned_chars`, `total_chars`, `next_offset` and `eof`.
    """
    _check_request_id(request_id)
    text, fmt = await _page_text(request_id, page, ctx)
    chunk = text[offset : offset + length]
    budget = _token_budget()
    if budget is not None:
        # The reply's other fields take a few dozen tokens; the text gets the rest.
        chunk = chunk[: max(1, _prefix_within(chunk, budget - 100))]
    next_offset = offset + len(chunk)
    return {
        "request_id": request_id,
        "page": page,
        "format": fmt,
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
    AI parser, `json_not_supported` ones return no JSON at all. The batch endpoints listed
    under /v1/async/scrape/batch/ are used for you: give `run_scraper` a list instead.

    Call this before assuming a dedicated scraper does or does not exist for a site,
    then call it again with the endpoint name to get the JSON Schema of what that endpoint
    accepts: types, `required`, `enum` values and defaults. That is the authoritative
    parameter list — more current than any documentation. Run the endpoint itself with
    `run_scraper`.
    """
    if endpoint is None:
        return await client._request("GET", "/v1/scrapers?group_by=json_output_support", ctx=ctx)

    # Batch endpoints, and some others, exist only in async form: 'scrape/batch' is described
    # at /v1/async/scrape/batch.
    name = await _route(_endpoint_name(endpoint), ctx)
    client._note(f"Describing /v1/{name}")
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
                "`list_scrapers(endpoint)` rather than guessing the keys. Give `url` or "
                "`query` as a list to run several that share every other parameter."
            )
        ),
    ],
    structured: Annotated[
        bool,
        Field(
            description=(
                "Return parsed JSON instead of page content. Implied by `json_prompt` or "
                "`json_schema`, and by an endpoint with a dedicated parser unless `output` "
                "in `params` asks for content. The dedicated parser runs first when there is one."
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

    An endpoint with a dedicated parser returns its parsed JSON by default; set `output` in
    `params` (e.g. ["markdown"]) to read the page instead. Other endpoints return Markdown
    wherever they offer it, unless `output` says otherwise; for structured data from them,
    describe the fields in `json_prompt`.
    Endpoints with a dedicated parser return typical page content in a predefined shape:
    search results (organic links, ads, images, news, videos, SERP extras), product
    listings and details (title, price, stock, seller, reviews), seller profiles,
    bestsellers, hotel offers, AI chat answers (prompt, response, citations), YouTube
    video and channel metadata. That runs first; with no dedicated parser, or one that
    failed or returned nothing, the AI parser extracts what `json_prompt` describes. The
    result's `parser` says which one answered, and `parser_note` carries the dedicated
    parser's own verdict (e.g. PARSE_PARTIAL_SUCCESS_SOME_FIELDS_DEFAULT). If a dedicated result
    lacks what you need, call again with `parser="ai"`.

    To run several URLs or queries with the same parameters, give `url` or `query` in
    `params` as a list: a few run side by side, more go to the endpoint's batch in one
    request, and the call waits for them. The reply has an item per URL or query, as
    `scrape` gives for a list. A batch cannot fall back from the dedicated parser to the AI
    parser item by item: run the ones it failed again with `parser="ai"`.

    Calls run synchronously. One that times out is sent to the corresponding asynchronous
    endpoint (`/v1/async/...`) and returns a `request_id` to collect with `check_requests`;
    an `async/...` endpoint, or one `list_scrapers` lists only in async form, is always
    queued.
    """
    client._start_progress()
    name = _endpoint_name(endpoint)
    if _is_batch(name):
        own = name.removeprefix("async/").replace("scrape/batch", "scrape", 1)
        raise ApiError(
            f"{endpoint!r} is a batch endpoint. Call run_scraper on {own!r} instead, with `url` "
            "or `query` as a list: it batches them for you."
        )
    name = await _route(name, ctx)
    key, inputs = _list_input(params)
    output = params.get(config.OUTPUT_PARAM)
    # A `json` prompt or schema in the params asks for parsed output as much as `output` does.
    wants_json = (isinstance(output, list) and "json" in output) or isinstance(
        params.get(config.JSON_PARAM), dict
    )

    if output is None and not structured:
        # Parsed JSON from a dedicated parser costs nothing extra and reads better than the
        # page it came from, a results page above all.
        structured = await _json_support(name, ctx) == config.JSON_SUPPORTED

    if structured or wants_json or json_prompt is not None or json_schema is not None:
        spec = (
            params.get(config.JSON_PARAM) if isinstance(params.get(config.JSON_PARAM), dict) else {}
        )
        prompt = json_prompt if json_prompt is not None else spec.get("prompt")
        schema = json_schema if json_schema is not None else spec.get("schema")
        _check_json_spec(prompt, schema)

        if key:
            common = {k: v for k, v in params.items() if k != key}
            return await _scrape_many(name, key, inputs, common, (parser, prompt, schema), ctx)
        return await _extract(name, params, parser, prompt, schema, ctx)

    if output is None:
        default = await _default_output(name, params, ctx)
        if default:
            params = {**params, config.OUTPUT_PARAM: default}
    if key:
        common = {k: v for k, v in params.items() if k != key}
        return await _scrape_many(name, key, inputs, common, None, ctx)
    return await _scrape(name, params, ctx)


async def _default_output(
    name: str, params: dict[str, Any], ctx: Context | None
) -> list[str] | None:
    """The `output` for a request that sets none: Markdown wherever the endpoint offers it.

    The API's own default is HTML, far more tokens than an agent needs, which is why
    `scrape` asks for Markdown too.

    A `json` prompt or schema only applies to JSON output, so it gets that instead. An
    endpoint whose schema lists no Markdown (Google Trends, YouTube metadata) keeps its own
    default. If the schema cannot be read, Markdown is sent and the API has the last word.
    """
    if config.JSON_PARAM in params:
        return ["json"]
    formats = await _output_formats(name, ctx)
    return ["markdown"] if formats is None or "markdown" in formats else None


def _check_json_spec(prompt: Any, schema: Any) -> None:
    """Refuse what the API refuses: a blank prompt, or a prompt and a schema together."""
    if prompt is not None and not str(prompt).strip():
        raise ApiError("`json_prompt` must describe the fields to pull out.")
    if prompt and schema:
        raise ApiError(
            "`json_prompt` and `json_schema` cannot be used together: the AI parser takes one of "
            "them. Pass `json_schema` when you need an exact shape, `json_prompt` otherwise."
        )


def _list_input(params: dict[str, Any]) -> tuple[str | None, list[str]]:
    """The field that holds a list of items to run, `url` or `query`, and its items.

    None when neither is a list: then the params are one request.
    """
    keys = [key for key in config.BATCH_INPUT_PARAMS if isinstance(params.get(key), list)]
    if not keys:
        return None, []
    if len(keys) > 1:
        raise ApiError(
            "Only one of `url` and `query` can be a list: the items differ in it and share "
            "every other parameter."
        )
    return keys[0], _inputs(params[keys[0]], keys[0])


def _inputs(items: list[Any], key: str) -> list[str]:
    """A list of URLs or queries to run, as given — a repeated one runs again — within this
    server's limit."""
    if not items:
        raise ApiError(f"`{key}` is an empty list: give at least one item.")
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ApiError(f"Every item of `{key}` must be a non-empty string.")
    if len(items) > config.BATCH_LIMIT:
        raise ApiError(
            f"`{key}` has {len(items)} items, and this server takes at most "
            f"{config.BATCH_LIMIT} per call. Split the list."
        )
    return list(items)


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


def _is_batch(name: str) -> bool:
    """Whether an endpoint name is a batch endpoint: scrape/batch, or one under it."""
    name = name.removeprefix("async/")
    return name == "scrape/batch" or name.startswith("scrape/batch/")

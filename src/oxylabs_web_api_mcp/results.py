"""Many results in one reply: lists of scrapes, queued requests waited for, and a long page
read in parts."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp import Context

from . import client, config
from .content import (
    _decode_screenshots,
    _estimate_tokens,
    _flag_thin_content,
    _output_format,
    _request_id,
    _result_url,
    _spill,
    _spill_dir,
    _spill_path,
    _token_budget,
    _trim,
)
from .errors import ApiError, ApiFaulted, ApiNotFound, ApiTimeout
from .scraping import (
    _dedicated_failure,
    _dedicated_note,
    _extract,
    _is_queued,
    _json_support,
    _label,
    _parser_of,
    _results,
    _scrape,
    _scraper_groups,
)

PARSER_RETRY = "call run_scraper again with parser='ai' and `json_prompt` naming the fields"
_REQUEST_FAILURES = (ApiFaulted, ApiTimeout, ApiNotFound)
_FORMATS_BY_EXT = {"md": "markdown", "json": "json", "html": "html"}


@dataclass
class _Entry:
    """One URL, query or request id of a call that handles several."""

    request_id: str | None = None
    input: Any = None
    body: dict[str, Any] | None = None  # the finished response
    error: ApiError | None = None  # it failed, or was never queued
    pending: bool = False
    kept: bool = True  # the API keeps the result, so `read_result` can fetch it again
    unread: ApiError | None = None  # the last attempt to check on it failed


def _check_request_id(request_id: str) -> None:
    if not _request_id(request_id):
        raise ApiError(
            f"{request_id!r} is not a request id. Use the `request_id` a queued call returned."
        )


async def _collect(request_id: str, ctx: Context | None) -> dict[str, Any]:
    """Read a queued request from the API, with errors an agent can act on."""
    # Every /v1/async/... request, whatever endpoint queued it, is collected from this one path.
    path = f"/v1/async/scrape/{request_id}"
    try:
        return await client._request("GET", path, ctx=ctx)
    except ApiFaulted as exc:
        if exc.tag.startswith("PARSE_") and _parser_of(exc.params) == "dedicated":
            raise ApiFaulted(
                f"{exc} The dedicated parser failed; {PARSER_RETRY}.",
                tag=exc.tag,
                params=exc.params,
            ) from exc
        raise
    except ApiError as exc:
        if "returned 404" in str(exc):
            raise ApiNotFound(
                f"No request {request_id!r}: the API does not know this request id. Either it "
                "expired or it was never a valid one. Start it again."
            ) from exc
        raise


def _finished(body: dict[str, Any]) -> dict[str, Any]:
    """A finished request's result, labelled with the parser that answered and its verdict."""
    body = {key: value for key, value in body.items() if key not in ("parser", "parser_note")}
    parser = _parser_of(body.get("params"))
    failure = _dedicated_failure(body) if parser == "dedicated" else ""
    result = _trim(_flag_thin_content(body))
    result.pop("request_id", None)  # the item carries it
    item: dict[str, Any] = {"state": "done", "result": result}
    if failure:
        _label(item, parser, f"The dedicated parser failed ({failure}); {PARSER_RETRY}.")
    elif parser == "dedicated":
        _label(item, parser, _dedicated_note(result, PARSER_RETRY))
    elif parser == "ai":
        _label(item, parser, "Extracted by the AI parser from your prompt or schema.")
    return item


def _input_of(body: dict[str, Any]) -> Any:
    """The URL or query a request was made for, from the params the API echoes."""
    params = body.get("params")
    if not isinstance(params, dict):
        return None
    return next((params[key] for key in config.BATCH_INPUT_PARAMS if params.get(key)), None)


async def _read(entries: list[_Entry], ctx: Context | None) -> None:
    """Check each queued entry once, a few at a time."""
    semaphore = asyncio.Semaphore(config.CHECK_REQUESTS_PULL_LIMIT)

    async def read(entry: _Entry) -> None:
        async with semaphore:
            try:
                body = await _collect(str(entry.request_id), ctx)
            except _REQUEST_FAILURES as exc:
                entry.error, entry.pending = exc, False
                return
            except ApiError as exc:
                # It may be running or done; only reading it failed, so it is checked again.
                entry.unread = exc
                return
        entry.unread = None
        if entry.input is None:
            entry.input = _input_of(body)
        if client._state(body) == "done":
            entry.body, entry.pending = body, False

    await asyncio.gather(*(read(entry) for entry in entries))


async def _wait(entries: list[_Entry], ctx: Context | None, strict: bool = False) -> None:
    """Check the queued entries until each has finished, for `MAX_WAIT_FOR_QUEUED_SECONDS`.

    An agent has no way to wait between calls, so it would poll again at once, a model turn
    each time; waiting here turns that loop into one call. With `strict`, a first round that
    reads nothing at all raises its error — a bad key, or the API out of reach — instead of
    waiting it out.
    """
    deadline = time.monotonic() + config.MAX_WAIT_FOR_QUEUED_SECONDS
    first = True
    while True:
        pending = [entry for entry in entries if entry.pending]
        if not pending:
            return
        await _read(pending, ctx)
        if strict and first and all(entry.unread for entry in pending):
            raise pending[0].unread  # type: ignore[misc]
        first = False
        left = sum(1 for entry in entries if entry.pending)
        done = len(entries) - left
        await client._progress(ctx, done, len(entries), f"{done} of {len(entries)} finished")
        if not left or time.monotonic() + config.POLL_SECONDS > deadline:
            return
        await asyncio.sleep(config.POLL_SECONDS)


def _reply(entries: list[_Entry]) -> dict[str, Any] | list[Any]:
    """Every entry's state and, for as many as fit, its result; screenshots as images."""
    items: list[dict[str, Any]] = []
    formats: dict[int, str] = {}
    readable: dict[int, bool] = {}
    shots: list[tuple[_Entry, dict[str, Any], Any]] = []
    for entry in entries:
        item: dict[str, Any] = {}
        if entry.request_id:
            item["request_id"] = entry.request_id
        if entry.input is not None:
            item["input"] = entry.input
        if entry.error is not None:
            item.update(state="failed", error=str(entry.error))
        elif entry.pending or entry.body is None:
            item["state"] = "pending"
            if entry.unread is not None:
                item["error"] = f"Could not check: {entry.unread}"
        else:
            item.update(_finished(entry.body))
            formats[len(items)] = _output_format(entry.body.get("params"))
            # Locally every summarized page goes to disk, so any of them can be read on.
            readable[len(items)] = bool(entry.request_id) and (entry.kept or config.SPILL_ENABLED)
            for info, image in _decode_screenshots(item["result"], entry.request_id):
                shots.append((entry, info, image))
        items.append(item)

    # Each image is a full-page PNG, so only the first few go out with the reply.
    limit = config.MAX_SCREENSHOTS_PER_REPLY
    for entry, info, _ in shots[limit:]:
        see = (
            f"See it with check_requests(['{entry.request_id}'])"
            if entry.kept and entry.request_id
            else "Scrape it again on its own to see it"
        )
        info["note"] = f"Not returned as an image: a reply carries at most {limit}. {see}" + (
            ", or open the file at `path`." if "path" in info else "."
        )

    done = sum(1 for item in items if item["state"] == "done")
    failed = sum(1 for item in items if item["state"] == "failed")
    reply: dict[str, Any] = {"done": done, "failed": failed, "pending": len(items) - done - failed}
    notes = _share_notes(items)
    if notes:
        reply["parser_notes"] = notes
    reply["items"] = items
    if reply["pending"]:
        waits = (
            f": it waits up to {config.MAX_WAIT_FOR_QUEUED_SECONDS}s for them"
            if config.MAX_WAIT_FOR_QUEUED_SECONDS
            else ""
        )
        reply["note"] = (
            f"{reply['pending']} not finished yet. Collect them by calling check_requests with "
            f"their request_ids{waits}."
        )
    _fit(reply, formats, readable)
    images = [image for _, _, image in shots[:limit]]
    return [reply, *images] if images else reply


def _share_notes(items: list[dict[str, Any]]) -> list[str]:
    """Take each item's `parser_note` out, and return the distinct ones for the whole reply.

    A list's results come from one endpoint and one parser, so fifty of them would carry
    the same few hundred characters fifty times. Each item names its note by its place.
    """
    notes: list[str] = []
    for item in items:
        note = item.get("parser_note")
        if not isinstance(note, str):
            continue
        if note not in notes:
            notes.append(note)
        item["parser_note"] = f"parser_notes[{notes.index(note)}]"
    return notes


def _fit(reply: dict[str, Any], formats: dict[int, str], readable: dict[int, bool]) -> None:
    """Fit the finished results in `reply` to the token budget.

    Room is kept first for every finished result as a bare summary (each page's URL and
    size) and for the note that says so, so the reply stays within the budget however many
    results there are. Then each result goes out whole, in order, while it fits; the rest
    share what is left, each page showing as much of its start as its share holds. No
    result is dropped, and a later small one still goes out whole after a large one did
    not. A summarized page that can be read on says where to continue; locally it is
    written to disk for that.
    """
    budget = _token_budget()
    items = reply["items"]
    finished = list(formats)
    if budget is None or not finished:
        return
    wholes: dict[int, dict[str, Any]] = {}
    texts: dict[int, list[str]] = {}
    for n in finished:
        wholes[n] = items[n]
        items[n], texts[n] = _summary(items[n], formats[n])
        if not readable[n]:
            items[n]["kept"] = False
    unreadable = not all(readable[n] for n in finished)
    longest_note = {"results_note": _results_note(len(finished), len(finished), unreadable)}
    left = budget - _tokens(reply) - _tokens(longest_note)
    for n in finished:
        floor = _tokens(items[n])  # already counted in `left`
        # Each estimate rounds up on its own, so a swap can add a token more than they differ.
        tokens = _tokens_within(_as_text(wholes[n]), left + floor - 1)
        if tokens is not None:
            items[n] = wholes[n]
            left -= tokens - floor + 1

    cut = [n for n in finished if items[n] is not wholes[n]]
    pages = [(n, page) for n in cut for page in items[n]["result"]["results"]]
    for k, (n, page) in enumerate(pages):
        share = left // (len(pages) - k)
        left -= _preview(page, texts[n][page["page"]], share, readable[n])
    for n in cut:
        if readable[n] and config.SPILL_ENABLED:
            item = items[n]
            for page in item["result"]["results"]:
                if text := texts[n][page["page"]]:
                    _spill(text, page["url"], formats[n], item["request_id"], page["page"])
    if cut:
        unreadable = not all(readable[n] for n in cut)
        reply["results_note"] = _results_note(len(cut), len(finished), unreadable)


def _summary(item: dict[str, Any], fmt: str) -> tuple[dict[str, Any], list[str]]:
    """A finished request as a bare summary, each page's URL and size, and its pages' text.

    Locally a page also names the `path` it is written to, should it be summarized.
    """
    request_id = _request_id(item.get("request_id"))
    pages: list[dict[str, Any]] = []
    texts: list[str] = []
    for index, result in enumerate(_results(item["result"])):
        content = None if fmt == "screenshot" else result.get(fmt)
        text = "" if content is None else _as_text(content)
        url = _result_url(result)
        page: dict[str, Any] = {
            "page": index,
            "url": url,
            "format": fmt,
            "total_chars": len(text),
            "estimated_tokens": _estimate_tokens(text),
        }
        for key in ("content_thin", "screenshot"):
            if key in result:
                page[key] = result[key]
        if config.SPILL_ENABLED and text and request_id:
            page["path"] = str(_spill_path(url, fmt, request_id, index))
        pages.append(page)
        texts.append(text)
    summary = {**item, "result": {**item["result"], "results": pages}, "summarized": True}
    return summary, texts


def _preview(page: dict[str, Any], text: str, share: int, read_on: bool) -> int:
    """Show as much of the start of `text` on `page` as `share` tokens hold; return the cost.

    With `read_on`, a page shown in part says where to continue.
    """
    if not text or share <= 0:
        return 0
    base = _tokens(page)

    def shown(length: int) -> dict[str, Any]:
        more = read_on and length < len(text)
        return {**page, "preview": text[:length], **({"next_offset": length} if more else {})}

    def cost(length: int) -> int:
        return _tokens(shown(length)) - base + 1

    length = _longest(min(len(text), 4 * share + 4), lambda length: cost(length) <= share)
    if not length:
        return 0
    page.update(shown(length))
    return cost(length)


def _results_note(summarized: int, finished: int, unreadable: bool) -> str:
    """Say how many finished results came as summaries, and how to read on in them."""
    note = (
        f"{summarized} of the {finished} finished results did not fit in this reply whole, so "
        "they come as summaries (marked `summarized`), each page with as much of its start as "
        "there was room for under `preview`. Read on from a page's `next_offset` with "
        "read_result(request_id, page, offset), and stop once you have what you need."
    )
    if unreadable:
        note += (
            " A summary marked `kept: false` came from a synchronous scrape, whose result the "
            "API does not keep: run that one again on its own for more of it."
        )
    return note


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _tokens(value: Any) -> int:
    """What `value` costs in a reply, as JSON."""
    return _estimate_tokens(json.dumps(value, ensure_ascii=False, default=str))


def _tokens_within(text: str, budget: int) -> int | None:
    """`text`'s estimated tokens, or None when that is over `budget`.

    A token covers at most about four characters, so a text over four times the budget is
    turned down by its length alone, without scanning every character.
    """
    if (len(text) + 3) // 4 > budget:
        return None
    tokens = _estimate_tokens(text)
    return tokens if tokens <= budget else None


def _longest(limit: int, fits: Callable[[int], bool]) -> int:
    """The largest length up to `limit` that `fits`, given that every shorter one fits too."""
    low, high = 0, limit
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


def _prefix_within(text: str, budget: int) -> int:
    """The length of the longest start of `text` that costs at most `budget` in a reply."""
    if _tokens([text]) <= budget:
        return len(text)
    return _longest(
        min(len(text), 4 * budget + 4), lambda length: _tokens([text[:length]]) <= budget
    )


async def _page_text(request_id: str, page: int, ctx: Context | None) -> tuple[str, str]:
    """A result page's whole text and its format: from disk when written there, else the API.

    A page read from the API is written to disk when local, so reading on stays local.
    """
    if config.SPILL_ENABLED:
        for path in sorted(_spill_dir().glob(f"{request_id}-{page}.*.txt")):
            text = path.read_text(encoding="utf-8", errors="replace")
            return text, _FORMATS_BY_EXT.get(path.name.split(".")[-2], "html")
    try:
        body = await _collect(request_id, ctx)
    except ApiNotFound as exc:
        raise ApiNotFound(
            f"{exc} The API keeps the results of queued requests only: a page from a "
            "synchronous scrape can be read on only where this server wrote it to disk, so "
            "run that scrape again on its own for more of it."
        ) from exc
    if client._state(body) != "done":
        raise ApiError(
            f"Request {request_id!r} has not finished. Collect it with check_requests first."
        )
    results = _results(body)
    if page >= len(results):
        raise ApiError(f"Request {request_id!r} has {len(results)} page(s); `page` counts from 0.")
    fmt = _output_format(body.get("params"))
    content = None if fmt == "screenshot" else results[page].get(fmt)
    if content is None:
        raise ApiError(
            f"Page {page} of request {request_id!r} has no text to read"
            + (
                ": a screenshot is an image, which check_requests returns."
                if fmt == "screenshot"
                else "."
            )
        )
    text = _as_text(content)
    if config.SPILL_ENABLED:
        _spill(text, _result_url(results[page]), fmt, request_id, page)
    return text, fmt


async def _scrape_many(
    name: str,
    key: str,
    inputs: list[Any],
    common: dict[str, Any],
    extract: tuple[str, str | None, dict[str, Any] | None] | None,
    ctx: Context | None,
) -> dict[str, Any] | list[Any]:
    """Scrape several URLs or queries that share every other parameter, in one reply.

    Up to `MAX_PARALLEL_SYNC_SCRAPES` run side by side through the synchronous endpoint,
    as a call each would; more go to the endpoint's batch in one request. What is queued
    is then waited for, so most lists come back finished. `extract` is the parser, prompt
    and schema when JSON is wanted.
    """
    if len(inputs) <= config.MAX_PARALLEL_SYNC_SCRAPES:
        entries = await _run_each(name, key, inputs, common, extract, ctx)
    else:
        entries = await _run_batch(name, key, inputs, common, extract, ctx)
    errors = [entry.error for entry in entries]
    if all(errors) and len({str(error) for error in errors}) == 1:
        raise errors[0]  # type: ignore[misc]  # the call is wrong, not each of its items
    await _wait(entries, ctx)
    return _reply(entries)


async def _run_each(
    name: str,
    key: str,
    inputs: list[Any],
    common: dict[str, Any],
    extract: tuple[str, str | None, dict[str, Any] | None] | None,
    ctx: Context | None,
) -> list[_Entry]:
    """Run each item through the synchronous endpoint, side by side."""

    async def run(item: Any) -> _Entry:
        payload = {key: item, **common}
        try:
            if extract is None:
                response = await _scrape(name, payload, ctx, raw=True)
            else:
                response = await _extract(name, payload, *extract, ctx, raw=True)
        except ApiError as exc:
            return _Entry(input=item, error=exc)
        if _is_queued(response):
            request_id = _request_id(response.get("request_id"))
            return _Entry(request_id=request_id, input=item, pending=True)
        metadata = response.get("metadata")
        request_id = _request_id(metadata.get("request_id") if isinstance(metadata, dict) else None)
        return _Entry(request_id=request_id, input=item, body=response, kept=False)

    return list(await asyncio.gather(*(run(item) for item in inputs)))


async def _run_batch(
    name: str,
    key: str,
    inputs: list[Any],
    common: dict[str, Any],
    extract: tuple[str, str | None, dict[str, Any] | None] | None,
    ctx: Context | None,
) -> list[_Entry]:
    """Queue every item in one request to the endpoint's batch."""
    batch = _batch_name(name)
    path = f"/v1/async/{batch}"
    groups = await _scraper_groups(ctx)
    if groups is not None and path not in groups:
        raise ApiError(
            f"/v1/{name} takes no batches, so one call runs at most "
            f"{config.MAX_PARALLEL_SYNC_SCRAPES} items on it. Split the list."
        )
    payload = {key: inputs, **common}
    if extract is not None:
        payload = await _batch_json(batch, payload, *extract, ctx)
    await client._note(ctx, f"Queueing a batch of {len(inputs)} on {path}")
    body = await client._request("POST", path, payload, ctx=ctx, idempotent=False, cost=len(inputs))

    # The API echoes each request's full params; the item is the one it was queued for.
    entries = []
    for request in body.get("data") or []:
        metadata = request.get("metadata") if isinstance(request, dict) else None
        request_id = _request_id(metadata.get("request_id") if isinstance(metadata, dict) else None)
        if request_id:
            echoed = request.get("params") if isinstance(request.get("params"), dict) else {}
            entries.append(_Entry(request_id=request_id, input=echoed.get(key), pending=True))
    refused = [
        _Entry(input=item, error=ApiError(f"Not queued: {detail}"))
        for item, detail in _rejected(body.get("errors"), key, inputs)
    ]
    if not entries:
        reasons = "; ".join(f"{entry.input!r}: {entry.error}" for entry in refused)
        raise ApiError(f"{path} queued nothing. {reasons or body}")
    return entries + refused


def _batch_name(name: str) -> str:
    """The batch endpoint of a scrape endpoint: scrape/batch, or scrape/batch/<source>."""
    first, _, source = name.removeprefix("async/").partition("/")
    return "/".join(part for part in (first, "batch", source) if part)


async def _batch_json(
    batch: str,
    payload: dict[str, Any],
    parser: str,
    prompt: str | None,
    schema: dict[str, Any] | None,
    ctx: Context | None,
) -> dict[str, Any]:
    """A batch's payload for JSON: the dedicated parser where there is one, else the AI one.

    A batch has one set of parameters, so it cannot fall back from the dedicated parser to
    the AI parser item by item the way a single call does: the items it failed are run
    again with parser='ai'. The API also takes a batch that asks for JSON its endpoint
    cannot give, failing each item at poll time, so that is refused here first.
    """
    group = await _json_support(f"async/{batch}", ctx)
    if group == config.JSON_NOT_SUPPORTED:
        raise ApiError(
            f"/v1/async/{batch} cannot return structured JSON. Call it without `json_prompt` "
            "and read the content instead."
        )
    base = {k: v for k, v in payload.items() if k != config.JSON_PARAM}
    base[config.OUTPUT_PARAM] = ["json"]
    spec = {k: v for k, v in (("prompt", prompt), ("schema", schema)) if v}
    if parser == "auto" and (group == config.JSON_SUPPORTED or (group is None and not spec)):
        return base
    if not spec:
        raise ApiError(
            "parser='ai' needs `json_prompt` (or `json_schema`) describing the fields to pull out."
            if parser == "ai"
            else f"/v1/async/{batch} has no dedicated parser, so structured output needs "
            "`json_prompt` (or `json_schema`) describing the fields to pull out."
        )
    return {**base, config.JSON_PARAM: spec}


def _rejected(errors: Any, key: str, inputs: list[Any]) -> list[tuple[Any, str]]:
    """The items the API refused, each as its input and why.

    The API names an item by a pointer into the request, `#/url/3`; the agent needs the URL
    or query itself to fix it or send it again.
    """
    rejected = []
    for error in errors or []:
        if not isinstance(error, dict):
            continue
        field, _, index = str(error.get("pointer") or "").removeprefix("#/").partition("/")
        known = field == key and index.isdigit() and int(index) < len(inputs)
        rejected.append((inputs[int(index)] if known else None, str(error.get("detail") or error)))
    return rejected

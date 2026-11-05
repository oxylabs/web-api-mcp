"""Running scrapes: sync first, queued when slow, and the parser choice for JSON."""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from typing import Any

from fastmcp import Context

from . import client, config
from .content import (
    _flag_thin_content,
    _output_format,
    _pop_screenshots,
    _process_content,
    _token_budget,
    _trim,
)
from .errors import ApiAuthError, ApiError, ApiFaulted, ApiTimeout

# Per API key (hashed): the parser groups, and each endpoint's `output` values. Bounded, so
# a server shared by many keys does not grow with every one of them.
_JSON_GROUPS: dict[str, tuple[float, dict[str, str]]] = {}
_OUTPUT_FORMATS: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}


def _cached(cache: MutableMapping[Any, Any], key: Any) -> Any:
    """A live cache entry's value, or None; a hit moves to the back of the eviction line."""
    entry = cache.pop(key, None)
    if entry is None or entry[0] < time.time():
        return None
    cache[key] = entry
    return entry[1]


def _cache(cache: MutableMapping[Any, Any], key: Any, value: Any, limit: int) -> None:
    """Keep `value` for `JSON_GROUPS_TTL_SECONDS`, dropping the least recently used beyond
    `limit` entries."""
    cache.pop(key, None)
    cache[key] = (time.time() + config.JSON_GROUPS_TTL_SECONDS, value)
    while len(cache) > limit:
        del cache[next(iter(cache))]


def _async_path(name: str) -> str:
    """The asynchronous endpoint for a scrape endpoint: /v1/scrape/x -> /v1/async/scrape/x."""
    return name if name.startswith("async/") else f"async/{name}"


async def _queue(
    name: str, payload: dict[str, Any], ctx: Context | None, reason: str = ""
) -> dict[str, Any]:
    """Hand slow work to the API's own queue and give the agent the id to poll.

    The request is never awaited here. The API keeps the result, which is why nothing about
    the request lives in this process: a restart or a second replica changes nothing.
    """
    path = f"/v1/{_async_path(name)}"
    client._note(f"Queueing {path}")
    response = _trim(await client._request("POST", path, payload, ctx=ctx, idempotent=False))
    request_id = response.get("request_id")
    if not request_id:
        raise ApiError(f"{path} accepted the request but returned no request_id: {response}")
    response["note"] = (
        f"{reason or 'Queued. '}Collect it with check_requests(['{request_id}']), which "
        "waits for it to finish."
    )
    return response


async def _scrape(
    name: str, payload: dict[str, Any], ctx: Context | None, raw: bool = False
) -> dict[str, Any] | list[Any]:
    """Run a scrape synchronously, and queue it only if the synchronous call times out.

    Most pages, rendered ones included, finish inside a tool call, so the sync endpoint is
    always tried first. A timeout — the API's own, a gateway's 504, or no answer at all —
    is not resent to it: the same payload goes to the corresponding asynchronous endpoint
    and the agent gets a request id to poll instead. While the call runs, the client hears
    every few seconds that it is still alive. `raw` returns the response as the API sent
    it, for a caller fitting several into one reply, which keeps the client informed itself.
    """
    if name.startswith("async/"):
        return await _queue(name, payload, ctx)

    path = f"/v1/{name}"
    client._note(f"Running {path}")
    request = client._request(
        "POST", path, payload, ctx=ctx, retry_faulted=False, slow_is_timeout=True
    )
    try:
        if raw:
            response = await request
        else:
            response = await client._heartbeat(ctx, request, f"Waiting for {path}")
    except ApiTimeout as exc:
        client._note(f"{path} timed out; queueing it instead")
        return await _queue(
            name,
            payload,
            ctx,
            reason=(
                f"The synchronous call timed out ({exc}), so the same request was queued instead. "
            ),
        )

    if raw:
        return response
    echoed = response.get("params")
    fmt = _output_format(
        echoed if isinstance(echoed, dict) and echoed.get(config.OUTPUT_PARAM) else payload
    )
    response = _process_content(response, fmt, _token_budget())
    return _pop_screenshots(_trim(_flag_thin_content(response)))


async def _scraper_groups(ctx: Context | None) -> dict[str, str] | None:
    """Every scrape path, mapped to its `JSON_*` group; None if unknown.

    Read from `GET /v1/scrapers?group_by=json_output_support` and cached per key, since
    the list can differ between keys. A failed lookup is not fatal: the scrape itself
    still answers with the authoritative error. A refused key is, since the scrape would
    only fail the same way.
    """
    key = client._key_id()
    groups = _cached(_JSON_GROUPS, key)
    if groups is None:
        try:
            body = await client._request(
                "GET", "/v1/scrapers?group_by=json_output_support", ctx=ctx
            )
        except ApiAuthError:
            raise
        except ApiError:
            return None
        listed = body.get("scrapers") if isinstance(body, dict) else None
        groups = {
            path: group
            for group, paths in (listed.items() if isinstance(listed, dict) else ())
            if isinstance(paths, list)
            for path in paths
        }
        _cache(_JSON_GROUPS, key, groups, config.MAX_CACHED_KEYS)
    return groups


async def _output_formats(name: str, ctx: Context | None) -> frozenset[str] | None:
    """The `output` values an endpoint accepts, from its JSON Schema; None if unknown.

    Cached per key and endpoint for as long as the parser groups. An endpoint whose
    schema has no `output` field accepts none.
    """
    key = (client._key_id(), name)
    formats = _cached(_OUTPUT_FORMATS, key)
    if formats is None:
        try:
            schema = await client._request("OPTIONS", f"/v1/{name}?format=json_schema", ctx=ctx)
        except ApiError:
            return None
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict):
            return None
        output = properties.get(config.OUTPUT_PARAM)
        items = output.get("items") if isinstance(output, dict) else None
        enum = items.get("enum") if isinstance(items, dict) else None
        if output is not None and not isinstance(enum, list):
            return None
        formats = frozenset(map(str, enum or ()))
        _cache(_OUTPUT_FORMATS, key, formats, config.MAX_CACHED_SCHEMAS)
    return formats


async def _json_support(name: str, ctx: Context | None) -> str | None:
    """Which parsers an endpoint offers: one of the `JSON_*` groups, or None if unknown."""
    groups = await _scraper_groups(ctx)
    return groups.get(f"/v1/{name}") if groups else None


async def _route(name: str, ctx: Context | None) -> str:
    """The asynchronous endpoint for a scraper `/v1/scrapers` lists only in async form."""
    if name.startswith("async/") or name == "scrape":
        return name
    groups = await _scraper_groups(ctx)
    if groups and f"/v1/{name}" not in groups and f"/v1/{_async_path(name)}" in groups:
        return _async_path(name)
    return name


def _json_output(output: Any) -> list[str]:
    """The `output` of a request for JSON: the caller's own list when it already asks for
    JSON — Markdown beside the parse, say — and JSON alone otherwise."""
    if isinstance(output, list) and config.JSON_PARAM in output:
        return list(dict.fromkeys(output))
    return [config.JSON_PARAM]


def _results(response: Any) -> list[dict[str, Any]]:
    results = response.get("results") if isinstance(response, dict) else None
    return [result for result in results or [] if isinstance(result, dict)]


def _has_json(response: Any) -> bool:
    """Whether any result carries a non-empty parsed `json`, whole or cut to fit the reply."""
    return any(
        result.get(config.JSON_PARAM) not in (None, {}, [], "")
        or any(
            isinstance(result.get(note), dict)
            and result[note].get("format") == config.JSON_PARAM
            and result[note].get("total_chars")
            for note in ("content_offloaded", "content_truncated", "json_truncated")
        )
        for result in _results(response)
    )


def _parse_tags(response: Any) -> list[str]:
    """The parser's own verdict per result, from `metadata.statuses.json_parse.tag`."""
    tags = []
    for result in _results(response):
        status = ((result.get("metadata") or {}).get("statuses") or {}).get("json_parse")
        if isinstance(status, dict) and status.get("tag"):
            tags.append(str(status["tag"]))
    return tags


def _parse_failed(tag: str) -> bool:
    """A verdict that means the dedicated parser produced nothing usable for this page."""
    return tag.startswith("PARSE_FAILED") or tag == "PARSE_NOT_SUPPORTED"


def _label(response: Any, parser: str, note: str) -> Any:
    if isinstance(response, dict):
        response["parser"] = parser
        response["parser_note"] = note
    return response


def _dedicated_failure(response: Any) -> str:
    """Why a finished dedicated parse is unusable, or "" when it produced something."""
    failure = next((tag for tag in _parse_tags(response) if _parse_failed(tag)), "")
    if not failure and not _has_json(response):
        failure = "no parsed content"
    return failure


def _dedicated_note(response: Any, retry: str) -> str:
    tags = _parse_tags(response)
    verdict = f" The parser reported {', '.join(sorted(set(tags)))}." if tags else ""
    return (
        "Parsed by the endpoint's dedicated parser, which returns its own predefined "
        f"structure.{verdict} Look for the fields you need in it; if they are missing, "
        f"{retry} to have the AI parser extract them."
    )


async def _extract(
    name: str,
    payload: dict[str, Any],
    parser: str,
    prompt: str | None,
    schema: dict[str, Any] | None,
    ctx: Context | None,
    raw: bool = False,
) -> dict[str, Any] | list[Any]:
    """Return structured JSON, from the endpoint's dedicated parser when it has one.

    A dedicated parser returns the endpoint's own fixed structure and cannot be steered;
    the AI parser returns what the prompt or schema describes. So the dedicated one goes
    first, and the AI parser runs when there is none, when it failed or came back empty,
    or when the caller asks for it after a dedicated result lacked what they needed.
    An `output` that already asks for JSON beside other formats keeps them. `raw` is
    passed on to `_scrape`.
    """
    group = await _json_support(name, ctx)
    if group == config.JSON_NOT_SUPPORTED:
        raise ApiError(
            f"/v1/{name} cannot return structured JSON. Call it without `json_prompt` and read "
            "the content instead."
        )

    base = {k: v for k, v in payload.items() if k != config.JSON_PARAM}
    base[config.OUTPUT_PARAM] = _json_output(payload.get(config.OUTPUT_PARAM))
    ai_spec: dict[str, Any] = {}
    if prompt:
        ai_spec["prompt"] = prompt
    if schema:
        ai_spec["schema"] = schema

    # With the groups unknown and no prompt, the dedicated parser is the only option left,
    # so it is sent anyway and the API answers whether the endpoint has one.
    dedicated = group == config.JSON_SUPPORTED or (group is None and not ai_spec)
    if dedicated and parser == "auto":
        client._note(f"Parsing /v1/{name} with its dedicated parser")
        try:
            response = await _scrape(name, base, ctx, raw)
        except ApiFaulted as exc:
            # A failed parse can fault the whole request; a failed scrape would fail the AI
            # parser too, so only the former is worth a second attempt.
            if not ai_spec or not exc.tag.startswith("PARSE_"):
                raise
            failure = exc.tag
        else:
            retry = (
                "call again with parser='ai' and `json_prompt` naming the fields"
                if not ai_spec
                else "call again with parser='ai'"
            )
            if _is_queued(response):
                return _label(
                    response,
                    "dedicated",
                    "Queued with the endpoint's dedicated parser. check_requests reports its "
                    f"verdict; if it failed or lacks the fields you need, {retry}.",
                )
            failure = _dedicated_failure(response)
            if not failure or not ai_spec:
                return _label(response, "dedicated", _dedicated_note(response, retry))
        client._note(f"The dedicated parser did not succeed ({failure}); using the AI parser")

    if not ai_spec:
        if parser == "ai":
            raise ApiError(
                "parser='ai' needs `json_prompt` (or `json_schema`) describing the fields "
                "to pull out."
            )
        raise ApiError(
            f"/v1/{name} has no dedicated parser, so structured output needs `json_prompt` "
            "(or `json_schema`) describing the fields to pull out."
        )
    client._note(f"Parsing /v1/{name} with the AI parser")
    response = await _scrape(name, {**base, config.JSON_PARAM: ai_spec}, ctx, raw)
    if _is_queued(response):
        return _label(
            response,
            "ai",
            "Queued with the AI parser. check_requests returns the extracted fields.",
        )
    return _label(response, "ai", "Extracted by the AI parser from your prompt or schema.")


def _is_queued(response: Any) -> bool:
    """A reply carrying a request id to poll rather than results."""
    return isinstance(response, dict) and "results" not in response


def _parser_of(params: Any) -> str:
    """Which parser a request asked for: "dedicated", "ai", or "" when it wanted no JSON."""
    output = params.get(config.OUTPUT_PARAM) if isinstance(params, dict) else None
    if not isinstance(output, list) or "json" not in output:
        return ""
    spec = params.get(config.JSON_PARAM)
    if isinstance(spec, dict) and (spec.get("prompt") or spec.get("schema")):
        return "ai"
    return "dedicated"

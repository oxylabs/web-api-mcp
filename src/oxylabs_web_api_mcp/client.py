"""Calls to the Web API: the key and identity they carry, retries, rate limiting, and
errors an agent can act on."""

from __future__ import annotations

import asyncio
import os
import random
import time
from platform import python_version
from typing import Any

import httpx
from fastmcp import Context
from fastmcp.server.dependencies import get_http_headers

from . import config
from .errors import ApiError, ApiFaulted, ApiTimeout

_CALL_TIMES: list[float] = []


def _http_headers() -> dict[str, str]:
    """Headers of the HTTP request being served, lowercased. Empty on stdio.

    `authorization` is opted back in: FastMCP strips it by default so it is not
    forwarded to an upstream by accident, but it is exactly what we need here.
    """
    return get_http_headers(include={"authorization"})


def _api_key() -> str:
    """The Bearer key, from the request headers over HTTP or the environment on stdio.

    Header auth is what lets a single HTTP deployment serve several callers on their own
    keys; stdio carries no headers, so it reads the environment.
    """
    header = _http_headers().get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()

    key = os.environ.get("OXYLABS_WEB_API_KEY", "").strip()
    if not key:
        raise ApiError(
            "No Oxylabs Web API key. Send it as an 'Authorization: Bearer <key>' header, "
            "or set OXYLABS_WEB_API_KEY in the server environment (see .env.example) and "
            "restart the MCP server."
        )
    return key


def _normalize_client_name(value: object, max_length: int = 64) -> str | None:
    """Turn the client's self-reported name into one token for the `x-oxylabs-sdk` header.

    The client names itself during `initialize` and we forward that for attribution, so it
    is untrusted input on its way into a header value: control characters (CR and LF above
    all) would let a malformed name split or inject headers, non-ASCII would make httpx
    refuse to encode the header, and an unbounded name would push the request past an
    upstream header limit.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(char if char.isascii() and char.isprintable() else " " for char in value)
    return "-".join(cleaned.lower().split())[:max_length].rstrip("-") or None


def _sdk_header(ctx: Context | None) -> str:
    """Identify this server, and the MCP client driving it, to the API."""
    try:
        params = ctx.request_context.session.client_params  # type: ignore[union-attr]
        # mcp 2 renamed the field to snake_case; mcp 1 only has the camelCase one.
        info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
        client = _normalize_client_name(info.name)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — telemetry must never be the reason a call fails
        client = None
    name = os.environ.get("OXYLABS_SDK_NAME", "oxylabs-web-api-mcp")
    product = f"{name}/{config.VERSION} {client}" if client else f"{name}/{config.VERSION}"
    return f"{product} (python {python_version()})"


def _detail(resp: httpx.Response) -> str:
    """Pull the useful part out of an error body; fall back to raw text."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict):
        # Validation problems name each bad field in `errors`; gateway errors carry `message`.
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(
                ": ".join(str(part) for part in (e.get("pointer"), e.get("detail")) if part)
                for e in errors
                if isinstance(e, dict)
            )
        return str(body.get("detail") or body.get("message") or body)[:500]
    return str(body)[:500]


def _quota_spent(resp: httpx.Response) -> bool:
    """A 429 whose problem title says the plan is spent; backoff cannot clear it."""
    if resp.status_code != 429:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("title") == "QUOTA_EXCEEDED"


def _check(resp: httpx.Response, path: str) -> dict[str, Any]:
    body = _json_body(resp)
    if resp.status_code == 401:
        reason = str((body or {}).get("detail") or (body or {}).get("message") or "").strip()
        raise ApiError(
            f"Authentication failed (401){f': {reason}' if reason else ''}. Check the API key."
        )
    if _quota_spent(resp):
        raise ApiError(
            "Quota exceeded (429): the plan's quota is spent. Stop and tell the user to "
            "check their plan in the Oxylabs dashboard; retrying will not help."
        )
    if resp.status_code == 429:
        # Reached only once the retries above are spent: backoff did not clear it, so this
        # is a quota problem for the human, not something to keep hammering.
        raise ApiError(
            "Rate limited (429) and still limited after backoff. Lower concurrency, or "
            "check the account's remaining quota in the Oxylabs dashboard."
        )
    if _timed_out(body):
        raise ApiTimeout(f"{path} returned {resp.status_code}: {_detail(resp)}")
    # A faulted request is answered with an error status and `state: "faulted"`, next to the
    # params and metadata of the request; `title` names why.
    if body is not None and _state(body) == "faulted":
        raise _faulted(body, path, resp.status_code)
    if resp.status_code >= 400:
        raise ApiError(f"{path} returned {resp.status_code}: {_detail(resp)}")
    if body is None:
        # OPTIONS on an endpoint is not guaranteed to answer with JSON.
        return {"raw": resp.text[:20000]}
    return body


def _json_body(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _timed_out(body: dict[str, Any] | None) -> bool:
    """The sync endpoint's own "scraping took too long" problem."""
    return body is not None and body.get("title") in config.TIMEOUT_TITLES


def _faulted(body: dict[str, Any], path: str, status_code: int) -> ApiFaulted:
    """Name the failure from the problem's `title` and `detail`."""
    tag = str(body.get("title") or "")
    details = str(body.get("detail") or "")
    request_id = (body.get("metadata") or {}).get("request_id")
    reason = ": ".join(part for part in (tag, details) if part) or "no reason given"
    if tag in config.PERMANENT_FAULTS or tag.startswith(config.PERMANENT_FAULT_PREFIXES):
        advice = (
            "Retrying the same request will not change this: fix the parameters, try another "
            "target, or report it to the user."
        )
    else:
        advice = (
            "Faulted requests are not charged, so retrying costs nothing; if it keeps failing, "
            f"quote request_id {request_id or 'unknown'} to support."
        )
    return ApiFaulted(
        f'{path} returned {status_code} with state "faulted" ({reason}). {advice}',
        tag=tag,
        params=body.get("params"),
    )


def _state(body: dict[str, Any]) -> Any:
    """The request's state: pending, done or faulted. `status` is an HTTP code, not this."""
    return body.get("state")


def _check_rate_limit() -> None:
    """Stop a runaway agent from spending the whole key. Off unless the operator sets it.

    The count lives in this process, so each replica keeps its own.
    """
    if config.RATE_LIMIT is None:
        return
    count, window = config.RATE_LIMIT
    cutoff = time.time() - window
    _CALL_TIMES[:] = [t for t in _CALL_TIMES if t > cutoff]
    if len(_CALL_TIMES) >= count:
        wait = int(_CALL_TIMES[0] + window - time.time()) + 1
        raise ApiError(
            f"This server's own rate limit ({os.environ.get('OXYLABS_RATE_LIMIT')}) is "
            f"exhausted. It frees up in about {wait}s. Do not retry in a loop — do "
            "something else, or ask the operator to raise OXYLABS_RATE_LIMIT."
        )
    _CALL_TIMES.append(time.time())


async def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    ctx: Context | None = None,
    api_key: str | None = None,
    retry_faulted: bool = True,
) -> dict[str, Any]:
    """Call the Web API, turning transport failures into agent-readable errors.

    Transient upstream failures (500, 502, 503, 504) and connection errors are retried
    here with exponential backoff. The agent has no better move than trying again, so
    doing it in the server saves a round trip and a confused recovery.
    """
    if method == "POST":
        _check_rate_limit()
    headers = {
        "Authorization": f"Bearer {api_key or _api_key()}",
        "x-oxylabs-sdk": _sdk_header(ctx),
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"

    last_error: ApiError | None = None
    for attempt in range(config.RETRIES + 1):
        if attempt:
            # Full jitter: concurrent callers that hit the same 429 must not retry in lockstep.
            await asyncio.sleep(random.uniform(0, config.RETRY_BASE_DELAY * 2 ** (attempt - 1)))
            await _note(ctx, f"Retrying {path} (attempt {attempt + 1} of {config.RETRIES + 1})")
        try:
            async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
                resp = await client.request(
                    method, f"{config.BASE_URL}{path}", json=payload, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise ApiError(f"Request to {path} timed out.") from exc
        except httpx.HTTPError as exc:
            last_error = ApiError(
                f"Could not reach the Oxylabs Web API at {config.BASE_URL}{path}: {exc}"
            )
            continue

        # A timed-out or faulted request is a verdict, not a transient 500: reading a finished
        # request again changes nothing, and a slow request resent waits as long again.
        body = _json_body(resp)
        faulted = body is not None and _state(body) == "faulted"
        settled = _timed_out(body) or (faulted and (method == "GET" or not retry_faulted))
        if (
            resp.status_code in config.RETRY_STATUS
            and attempt < config.RETRIES
            and not _quota_spent(resp)
            and not settled
        ):
            last_error = ApiError(f"{path} returned {resp.status_code}: {_detail(resp)}")
            continue
        return _check(resp, path)

    raise last_error or ApiError(f"{path} failed after {config.RETRIES + 1} attempts.")


async def _note(ctx: Context | None, message: str) -> None:
    """Tell the client what is happening. Never worth failing a call over."""
    if ctx is None:
        return
    try:
        await ctx.info(message)
    except Exception:  # noqa: BLE001
        pass

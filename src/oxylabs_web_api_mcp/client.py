"""Calls to the Web API: the key and identity they carry, retries, rate limiting, and
errors an agent can act on."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import random
import time
from collections.abc import Awaitable
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
from platform import python_version
from typing import Any, TypeVar

import httpx
from fastmcp import Context
from fastmcp.server.dependencies import get_http_headers

from . import config
from .errors import ApiAuthError, ApiError, ApiFaulted, ApiNotFound, ApiTimeout

logger = logging.getLogger("oxylabs_web_api_mcp")

_T = TypeVar("_T")

# When each scrape inside the rate limit's window started, per API key (hashed).
_CALL_TIMES: dict[str, list[float]] = {}
# The HTTP client, made once per event loop and reused for its connection pool.
_HTTP: tuple[Any, Any, Any] | None = None
# How many progress notifications the current tool call has sent; the tasks it starts share it.
_PROGRESS: ContextVar[list[int] | None] = ContextVar("oxylabs_progress", default=None)


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
        raise ApiError("Oxylabs Web API key is missing.")
    return key


def _key_id(key: str | None = None) -> str:
    """What caches and counters keep instead of the API key itself: a hash of it."""
    return hashlib.sha256((key or _api_key()).encode()).hexdigest()


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


def _retry_after(resp: httpx.Response) -> float | None:
    """The seconds `Retry-After` asks to wait, as a number or an HTTP date; None if unsaid."""
    value = resp.headers.get("retry-after", "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def _check(resp: httpx.Response, path: str) -> dict[str, Any]:
    body = _json_body(resp)
    if resp.status_code == 401:
        reason = str((body or {}).get("detail") or (body or {}).get("message") or "").strip()
        raise ApiAuthError(
            f"Authentication failed (401){f': {reason}' if reason else ''}. Check the API key."
        )
    if resp.status_code == 429:
        # Reached once backoff did not clear it, or when the API asks for a longer wait than
        # is worth holding a tool call for. A 429 is a rate limit or a spent quota.
        wait = _retry_after(resp)
        asks = f": the API asks to wait {math.ceil(wait)}s before the next call" if wait else ""
        raise ApiError(
            f"Rate limited or out of quota (429){asks}. Do not retry at once: lower "
            "concurrency, and if it persists, tell the user to check the plan's remaining "
            f"quota in the Oxylabs dashboard. {_detail(resp)}"
        )
    # A faulted request is answered with an error status and `state: "faulted"`, next to the
    # params and metadata of the request; `title` names why.
    if _timed_out(body):
        raise ApiTimeout(f"{path} returned {resp.status_code}: {_detail(resp)}")
    if body is not None and _state(body) == "faulted":
        raise _faulted(body, path, resp.status_code)
    if resp.status_code == 404:
        raise ApiNotFound(f"{path} returned 404: {_detail(resp)}")
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


def _check_rate_limit(cost: int = 1, key: str | None = None) -> None:
    """Stop a runaway agent from spending a whole key. Off unless the operator sets it.

    `cost` is how many scrapes the call starts: a batch is one call but many scrapes, and
    each of them counts. Every API key has a window of its own, so callers who bring their
    own key over HTTP do not use up each other's. The windows live in this process, so each
    replica keeps its own.
    """
    if config.RATE_LIMIT is None:
        return
    count, window = config.RATE_LIMIT
    limit = os.environ.get("OXYLABS_RATE_LIMIT")
    advice = (
        "Do not retry in a loop — do something else, or ask the operator to raise "
        "OXYLABS_RATE_LIMIT."
    )
    if cost > count:
        if cost == 1:  # a limit of 0
            raise ApiError(f"This server's own rate limit ({limit}) allows no scrapes. {advice}")
        raise ApiError(
            f"A batch of {cost} is more than this server's own rate limit ({limit}) allows "
            f"in a whole window. Send at most {count} at a time. {advice}"
        )
    bucket = key or _key_id()
    now = time.monotonic()
    times = [t for t in _CALL_TIMES.get(bucket, ()) if t > now - window]
    room = count - len(times)
    if cost > room:
        # The oldest calls leave the window first; this many of them must go to make room.
        wait = int(times[cost - room - 1] + window - now) + 1
        if cost == 1:
            problem = f"is exhausted. It frees up in about {wait}s."
        else:
            problem = (
                f"has room for {room} more scrapes, and this batch is {cost}. Send at most "
                f"{room}, or wait about {wait}s for room for all of them."
            )
        raise ApiError(f"This server's own rate limit ({limit}) {problem} {advice}")
    _CALL_TIMES[bucket] = [*times, *([now] * cost)]
    if len(_CALL_TIMES) > config.MAX_CACHED_KEYS:
        # Forget keys that have been idle for a whole window; they start afresh anyway.
        for idle in [k for k, ts in _CALL_TIMES.items() if not ts or ts[-1] <= now - window]:
            del _CALL_TIMES[idle]


def _http_client() -> Any:
    """The HTTP client for the running event loop, made once and reused for its pool.

    It keeps no cookies: callers on different keys share it, and nothing set on one
    caller's response may ride along on another caller's request.
    """
    global _HTTP
    loop = asyncio.get_running_loop()
    if _HTTP is None or _HTTP[0] is not loop or _HTTP[1] is not httpx.AsyncClient:
        cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
        timeout = httpx.Timeout(config.HTTP_TIMEOUT, connect=config.CONNECT_TIMEOUT)
        _HTTP = (loop, httpx.AsyncClient, httpx.AsyncClient(timeout=timeout, cookies=cookies))
    return _HTTP[2]


def _backoff(attempt: int, rate_limited: bool = False) -> float:
    """How long to sleep before retry number `attempt`.

    Jittered, so callers that failed together do not retry together. A rate limit needs
    longer to clear than a hiccup, so its waits start higher and are never near zero.
    """
    ceiling = config.RETRY_BASE_DELAY * 2 ** (attempt - 1)
    if rate_limited:
        ceiling *= config.RATE_LIMIT_BACKOFF_FACTOR
        return random.uniform(ceiling / 2, ceiling)
    return random.uniform(0, ceiling)


async def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    ctx: Context | None = None,
    retry_faulted: bool = True,
    idempotent: bool = True,
    slow_is_timeout: bool = False,
    cost: int = 1,
) -> dict[str, Any]:
    """Call the Web API, turning transport failures into agent-readable errors.

    Transient upstream failures (500, 502, 503, 504) and connection errors are retried
    here with exponential backoff. The agent has no better move than trying again, so
    doing it in the server saves a round trip and a confused recovery. A 429 or 503 that
    says how long to wait (`Retry-After`) is waited out, up to `MAX_RETRY_AFTER_SECONDS`.

    A POST that must not run twice (`idempotent=False`: anything it queues, a batch above
    all) is resent only after a refused connection, a 429 or a 503; any other failure says
    it may have been queued anyway, since sending it again could run it twice. `cost` is
    how many scrapes a POST starts.

    `slow_is_timeout` marks a synchronous scrape: a gateway timeout (504) or no answer
    within `HTTP_TIMEOUT` is then an `ApiTimeout` for the caller to queue, not a failure to
    resend, since a resend would wait as long again. The API's own timeout problem is one
    for any request.
    """
    key = _api_key()
    if method == "POST":
        _check_rate_limit(cost, _key_id(key))
    retry_status = config.RETRY_STATUS if idempotent else config.QUEUE_RETRY_STATUS
    maybe_taken = (
        ""
        if idempotent
        else " It may have been queued anyway, so sending it again could run it twice."
    )
    headers = {
        "Authorization": f"Bearer {key}",
        "x-oxylabs-sdk": _sdk_header(ctx),
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"

    last_error: ApiError | None = None
    delay = 0.0
    for attempt in range(config.RETRIES + 1):
        if attempt:
            await asyncio.sleep(delay)
            _note(f"Retrying {path} (attempt {attempt + 1} of {config.RETRIES + 1})")
        try:
            resp = await _http_client().request(
                method, f"{config.BASE_URL}{path}", json=payload, headers=headers
            )
        except httpx.TimeoutException as exc:
            unsent = isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
            if slow_is_timeout and not unsent:
                raise ApiTimeout(f"{path} sent no answer in {config.HTTP_TIMEOUT}s.") from exc
            raise ApiError(f"Request to {path} timed out.{'' if unsent else maybe_taken}") from exc
        except httpx.HTTPError as exc:
            if not idempotent and not isinstance(exc, httpx.ConnectError):
                raise ApiError(f"Request to {path} failed: {exc}.{maybe_taken}") from exc
            last_error = ApiError(
                f"Could not reach the Oxylabs Web API at {config.BASE_URL}{path}: {exc}"
            )
            delay = _backoff(attempt + 1)
            continue

        if slow_is_timeout and resp.status_code == 504:
            raise ApiTimeout(f"{path} returned 504: {_detail(resp)}")
        # A timed-out or faulted request is a verdict, not a transient 500: reading a finished
        # request again changes nothing, and a slow request resent waits as long again.
        body = _json_body(resp)
        faulted = body is not None and _state(body) == "faulted"
        settled = _timed_out(body) or (faulted and (method == "GET" or not retry_faulted))
        wait = _retry_after(resp) if resp.status_code in (429, 503) else None
        too_long = wait is not None and wait > config.MAX_RETRY_AFTER_SECONDS
        if (
            resp.status_code in retry_status
            and attempt < config.RETRIES
            and not settled
            and not too_long
        ):
            last_error = ApiError(f"{path} returned {resp.status_code}: {_detail(resp)}")
            if wait is not None:
                delay = wait + random.uniform(0, config.RETRY_BASE_DELAY)
            else:
                delay = _backoff(attempt + 1, rate_limited=resp.status_code == 429)
            continue
        if maybe_taken and resp.status_code in config.RETRY_STATUS - retry_status and not settled:
            raise ApiError(f"{path} returned {resp.status_code}: {_detail(resp)}.{maybe_taken}")
        return _check(resp, path)

    raise last_error or ApiError(f"{path} failed after {config.RETRIES + 1} attempts.")


def _note(message: str) -> None:
    """Record what is happening in the server's log.

    Not sent to the client: MCP has deprecated server log notifications (SEP-2577), and a
    long call keeps the client informed with progress notifications instead.
    """
    logger.info(message)


def _start_progress() -> None:
    """Begin a tool call's progress count; the tasks the call starts share it."""
    _PROGRESS.set([0])


async def _progress(ctx: Context | None, message: str) -> None:
    """Tell the client the call is alive and what it is doing. Never fatal.

    MCP requires each progress value to be greater than the last, so the value counts the
    call's notifications and `message` says what happened.
    """
    if ctx is None:
        return
    count = _PROGRESS.get()
    if count is None:
        count = [0]
        _PROGRESS.set(count)
    count[0] += 1
    try:
        await ctx.report_progress(count[0], None, message)
    except Exception:  # noqa: BLE001
        pass


async def _heartbeat(ctx: Context | None, work: Awaitable[_T], message: str) -> _T:
    """Await `work`, telling the client every `HEARTBEAT_SECONDS` that it is still running.

    A synchronous scrape can take minutes. Without a sign of life, a client whose request
    timeout is shorter gives up on the call — and on a result that may already be paid for.
    """
    if ctx is None:
        return await work
    task = asyncio.ensure_future(work)
    started = time.monotonic()
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=config.HEARTBEAT_SECONDS)
            if done:
                return task.result()
            await _progress(ctx, f"{message} ({int(time.monotonic() - started)}s)")
    finally:
        if not task.done():
            task.cancel()

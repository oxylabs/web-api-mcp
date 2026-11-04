"""Web API calls: the key and client name they carry, error mapping, retries and the rate limit."""

from __future__ import annotations

import asyncio
import os
import random
from platform import python_version

import httpx
from helpers import _headers, _stub_client
from mcp.types import InitializeRequestParams

from oxylabs_web_api_mcp import client, config
from oxylabs_web_api_mcp.errors import ApiError


def _resp(status: int, json_body=None, text: str = "") -> httpx.Response:
    req = httpx.Request("POST", "https://webapi.oxylabs.io/v1/search")
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=req)
    return httpx.Response(status, text=text, request=req)


def test_detail_flattens_validation_errors():
    body = {
        "status": 400,
        "title": "INVALID_BODY_PROPERTY",
        "detail": "A body property has an invalid value.",
        "errors": [{"detail": "Invalid type, integer is supported.", "pointer": "/pages"}],
    }
    assert client._detail(_resp(400, body)) == "/pages: Invalid type, integer is supported."


def test_detail_falls_back_to_message_then_text():
    assert client._detail(_resp(404, {"message": "Resource not found."})) == "Resource not found."
    assert client._detail(_resp(502, text="<html>bad gateway</html>")) == "<html>bad gateway</html>"


def test_transient_failures_are_retried_then_succeed():
    calls = []

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, json=None, headers=None):
            calls.append(url)
            status = 503 if len(calls) < 3 else 200
            return httpx.Response(
                status,
                json={"results": []} if status == 200 else {"message": "upstream"},
                request=httpx.Request(method, url),
            )

    real_client, real_delay = httpx.AsyncClient, config.RETRY_BASE_DELAY
    httpx.AsyncClient, config.RETRY_BASE_DELAY = _Client, 0
    try:
        out = asyncio.run(client._request("POST", "/v1/search", {"query": "x"}))
        assert out == {"results": []}
        assert len(calls) == 3, calls
    finally:
        httpx.AsyncClient, config.RETRY_BASE_DELAY = real_client, real_delay


def test_retries_give_up_and_report_the_last_failure():
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, json=None, headers=None):
            return httpx.Response(
                502, json={"message": "still down"}, request=httpx.Request(method, url)
            )

    real_client, real_delay = httpx.AsyncClient, config.RETRY_BASE_DELAY
    httpx.AsyncClient, config.RETRY_BASE_DELAY = _Client, 0
    try:
        asyncio.run(client._request("POST", "/v1/search", {"query": "x"}))
    except ApiError as exc:
        assert "502" in str(exc), exc
    else:
        raise AssertionError("expected ApiError after exhausting retries")
    finally:
        httpx.AsyncClient, config.RETRY_BASE_DELAY = real_client, real_delay


def test_a_400_is_not_retried():
    calls = []

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, json=None, headers=None):
            calls.append(url)
            return httpx.Response(400, json={"detail": "bad"}, request=httpx.Request(method, url))

    real_client = httpx.AsyncClient
    httpx.AsyncClient = _Client
    try:
        asyncio.run(client._request("POST", "/v1/search", {"query": "x"}))
    except ApiError:
        assert len(calls) == 1, "a validation error is the caller's bug; retrying wastes quota"
    finally:
        httpx.AsyncClient = real_client


def test_rate_limit_parsing_and_enforcement():
    assert config._parse_rate_limit("") is None
    assert config._parse_rate_limit("100/1h") == (100, 3600.0)
    assert config._parse_rate_limit("50/30m") == (50, 1800.0)
    try:
        config._parse_rate_limit("lots")
    except ValueError:
        pass
    else:
        raise AssertionError("a malformed rate limit must fail loudly at startup")

    real_limit, real_times = config.RATE_LIMIT, list(client._CALL_TIMES)
    config.RATE_LIMIT, client._CALL_TIMES[:] = (2, 3600.0), []
    try:
        client._check_rate_limit()
        client._check_rate_limit()
        try:
            client._check_rate_limit()
        except ApiError as exc:
            assert "rate limit" in str(exc).lower(), exc
        else:
            raise AssertionError("the third call must be refused")
    finally:
        config.RATE_LIMIT, client._CALL_TIMES[:] = real_limit, real_times


def test_only_posts_count_against_the_rate_limit():
    def ok(method, url):
        return httpx.Response(200, json={"state": "pending"}, request=httpx.Request(method, url))

    real_client, real_limit, real_times = (
        httpx.AsyncClient,
        config.RATE_LIMIT,
        list(client._CALL_TIMES),
    )
    httpx.AsyncClient = _stub_client(ok)
    config.RATE_LIMIT, client._CALL_TIMES[:] = (1, 3600.0), []
    try:
        for _ in range(3):
            asyncio.run(client._request("GET", "/v1/async/scrape/1"))
        asyncio.run(client._request("POST", "/v1/scrape", {}))
        try:
            asyncio.run(client._request("POST", "/v1/scrape", {}))
        except ApiError as exc:
            assert "rate limit" in str(exc).lower(), exc
        else:
            raise AssertionError("the second POST must be refused")
    finally:
        httpx.AsyncClient = real_client
        config.RATE_LIMIT, client._CALL_TIMES[:] = real_limit, real_times


def test_a_429_is_retried_and_then_reported_as_a_quota_problem():
    calls = []

    def cleared(method, url):
        calls.append(url)
        status = 429 if len(calls) < 3 else 200
        return httpx.Response(
            status,
            json={"results": []} if status == 200 else {"message": "slow down"},
            request=httpx.Request(method, url),
        )

    real_client, real_delay = httpx.AsyncClient, config.RETRY_BASE_DELAY
    httpx.AsyncClient, config.RETRY_BASE_DELAY = _stub_client(cleared), 0
    try:
        assert asyncio.run(client._request("POST", "/v1/search", {"query": "x"})) == {"results": []}
        assert len(calls) == 3, "a rate limit clears on its own; it is worth retrying"

        stuck = _stub_client(
            lambda m, u: httpx.Response(429, json={"message": "no"}, request=httpx.Request(m, u))
        )
        httpx.AsyncClient = stuck
        try:
            asyncio.run(client._request("POST", "/v1/search", {"query": "x"}))
        except ApiError as exc:
            # Backoff cannot clear a spent quota, so the message has to send the human
            # to the dashboard rather than inviting another round of retries.
            assert "quota" in str(exc).lower(), exc
        else:
            raise AssertionError("expected ApiError once the retries are spent")
    finally:
        httpx.AsyncClient, config.RETRY_BASE_DELAY = real_client, real_delay


def test_backoff_is_jittered_so_callers_do_not_retry_in_lockstep():
    windows = []

    def _record(low, high):
        windows.append((low, high))
        return 0

    stub = _stub_client(
        lambda m, u: httpx.Response(503, json={"message": "down"}, request=httpx.Request(m, u))
    )
    real_client, real_uniform = httpx.AsyncClient, random.uniform
    httpx.AsyncClient, random.uniform = stub, _record
    try:
        try:
            asyncio.run(client._request("POST", "/v1/search", {"query": "x"}))
        except ApiError:
            pass
        assert windows, "a retry must sleep a random amount, not a fixed one"
        assert all(low == 0 for low, _ in windows), windows
        highs = [high for _, high in windows]
        assert highs == sorted(highs) and highs[-1] > highs[0], highs
    finally:
        httpx.AsyncClient, random.uniform = real_client, real_uniform


def test_a_spent_quota_is_not_retried():
    calls = []

    def spent(method, url):
        calls.append(url)
        return httpx.Response(
            429,
            json={"status": 429, "title": "QUOTA_EXCEEDED", "detail": "Upgrade your plan."},
            request=httpx.Request(method, url),
        )

    real_client = httpx.AsyncClient
    httpx.AsyncClient = _stub_client(spent)
    try:
        asyncio.run(client._request("POST", "/v1/search", {"query": "x"}))
    except ApiError as exc:
        assert len(calls) == 1, "backoff cannot refill a quota"
        assert "quota" in str(exc).lower() and "retrying will not help" in str(exc), exc
    else:
        raise AssertionError("expected ApiError for a spent quota")
    finally:
        httpx.AsyncClient = real_client


def test_search_validation_errors_name_the_field():
    problem = {
        "status": 400,
        "title": "VALIDATION_ERROR",
        "detail": "1 request field is invalid; fix every entry in `errors` and resend.",
        "errors": [
            {
                "pointer": "#/location",
                "detail": "location must be an ISO 3166-1 alpha-2 country code",
            }
        ],
    }
    resp = httpx.Response(400, json=problem, request=httpx.Request("POST", "http://x/v1/search"))
    assert client._detail(resp) == "#/location: location must be an ISO 3166-1 alpha-2 country code"


def test_a_plain_500_is_still_retried():
    calls = []

    def flaky(method, url):
        calls.append(url)
        if len(calls) == 1:
            return httpx.Response(
                500, json={"title": "INTERNAL_SERVER_ERROR"}, request=httpx.Request(method, url)
            )
        return httpx.Response(200, json={"state": "done"}, request=httpx.Request(method, url))

    real_client, real_delay = httpx.AsyncClient, config.RETRY_BASE_DELAY
    httpx.AsyncClient, config.RETRY_BASE_DELAY = _stub_client(flaky), 0
    try:
        asyncio.run(client._request("POST", "/v1/scrape", {}, retry_faulted=False))
    finally:
        httpx.AsyncClient, config.RETRY_BASE_DELAY = real_client, real_delay
    assert len(calls) == 2, calls


def test_a_query_error_without_a_pointer_reads_cleanly():
    resp = httpx.Response(
        400,
        json={"title": "INVALID_QUERY", "errors": [{"detail": "unknown group_by"}]},
        request=httpx.Request("GET", "https://x/v1/scrapers"),
    )
    assert client._detail(resp) == "unknown group_by"


def test_a_permanent_fault_does_not_invite_a_retry():
    for tag in ("REQUEST_FAILED_BROWSER_INSTRUCTIONS", "DOWNLOAD_FAILED_VIDEO_PRIVATE"):
        exc = client._faulted({"title": tag, "detail": "d", "state": "faulted"}, "/v1/scrape", 500)
        assert "will not change" in str(exc) and "costs nothing" not in str(exc), exc
    exc = client._faulted(
        {"title": "REQUEST_FAILED_AFTER_TOO_MANY_RETRIES", "state": "faulted"}, "/v1/scrape", 500
    )
    assert "costs nothing" in str(exc), exc


def test_a_401_carries_the_apis_reason():
    resp = httpx.Response(
        401,
        json={"status": 401, "title": "UNAUTHORIZED", "detail": "Invalid authorization header."},
        request=httpx.Request("POST", "https://x/v1/scrape"),
    )
    try:
        client._check(resp, "/v1/scrape")
    except ApiError as exc:
        assert "Invalid authorization header." in str(exc), exc
    else:
        raise AssertionError("expected ApiError for a 401")
    bare = httpx.Response(401, text="", request=httpx.Request("POST", "https://x/v1/scrape"))
    try:
        client._check(bare, "/v1/scrape")
    except ApiError as exc:
        assert str(exc) == "Authentication failed (401). Check the API key.", exc
    else:
        raise AssertionError("expected ApiError for a 401")


def test_bearer_header_beats_the_environment():
    with _headers({"authorization": "Bearer from-header"}):
        assert client._api_key() == "from-header"
    # No usable header falls back to the environment.
    with _headers({"authorization": "Basic nope"}):
        assert client._api_key() == "test-key"
    assert client._api_key() == "test-key"


def test_missing_key_everywhere_is_an_actionable_error():
    saved = os.environ.pop("OXYLABS_WEB_API_KEY")
    try:
        client._api_key()
    except ApiError as exc:
        assert "Authorization: Bearer" in str(exc), exc
    else:
        raise AssertionError("expected ApiError when no key is available")
    finally:
        os.environ["OXYLABS_WEB_API_KEY"] = saved


def test_client_name_cannot_inject_a_header():
    assert client._normalize_client_name("evil\r\nX-Injected: 1") == "evil-x-injected:-1"
    assert client._normalize_client_name(" Claude \t Code ") == "claude-code"
    assert client._normalize_client_name("x" * 63 + " y") == "x" * 63
    assert len(client._normalize_client_name("x" * 5000)) == 64
    assert client._normalize_client_name("  \t  ") is None
    assert client._normalize_client_name(None) is None
    assert client._normalize_client_name("Cursor™") == "cursor"
    assert client._normalize_client_name("クライ") is None

    params = InitializeRequestParams.model_validate(
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "Claude\r\nCode", "version": "1"},
        }
    )

    class _Ctx:
        class request_context:
            class session:
                client_params = params

    python = python_version()
    assert client._sdk_header(_Ctx()) == (
        f"oxylabs-web-api-mcp/{config.VERSION} claude-code (python {python})"
    )
    assert client._sdk_header(None) == f"oxylabs-web-api-mcp/{config.VERSION} (python {python})"

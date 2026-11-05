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
from oxylabs_web_api_mcp.errors import ApiAuthError, ApiError, ApiNotFound


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


def test_rate_limit_parsing_and_enforcement(monkeypatch):
    assert config._parse_rate_limit("") is None
    assert config._parse_rate_limit("100/1h") == (100, 3600.0)
    assert config._parse_rate_limit("50/30m") == (50, 1800.0)
    try:
        config._parse_rate_limit("lots")
    except ValueError:
        pass
    else:
        raise AssertionError("a malformed rate limit must fail loudly at startup")

    monkeypatch.setattr(config, "RATE_LIMIT", (2, 3600.0))
    monkeypatch.setattr(client, "_CALL_TIMES", {})
    client._check_rate_limit()
    client._check_rate_limit()
    try:
        client._check_rate_limit()
    except ApiError as exc:
        assert "rate limit" in str(exc).lower(), exc
    else:
        raise AssertionError("the third call must be refused")


def test_a_batch_is_charged_one_scrape_per_item(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT", (5, 3600.0))
    monkeypatch.setattr(client, "_CALL_TIMES", {})
    client._check_rate_limit(3)
    for cost, reason in ((3, "room for 2 more"), (6, "more than")):
        try:
            client._check_rate_limit(cost)
        except ApiError as exc:
            assert reason in str(exc), exc
        else:
            raise AssertionError(f"a batch of {cost} must be refused")
    client._check_rate_limit(2)  # fills the window exactly
    try:
        client._check_rate_limit()
    except ApiError as exc:
        assert "exhausted" in str(exc), exc
    else:
        raise AssertionError("a full window must refuse even one more scrape")


def test_a_batch_post_is_not_resent_when_the_api_may_have_taken_it(monkeypatch):
    def api(*answers):
        calls = []

        def respond(method, url):
            answer = answers[min(len(calls), len(answers) - 1)]
            calls.append(answer)
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(answer, json={"data": []}, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx, "AsyncClient", _stub_client(respond))
        return calls

    def post():
        return asyncio.run(client._request("POST", "/v1/async/scrape/batch", {}, idempotent=False))

    monkeypatch.setattr(config, "RETRY_BASE_DELAY", 0)
    # After a 502, or a connection that broke mid-request, it is not sent again.
    for answer in (502, httpx.ReadError("connection reset")):
        calls = api(answer, 200)
        try:
            post()
        except ApiError as exc:
            assert "may have been queued" in str(exc), exc
        else:
            raise AssertionError(f"{answer!r} must not be resent")
        assert len(calls) == 1, calls
    # After a 503, or a connection never made, it is sent again.
    for answer in (503, httpx.ConnectError("refused")):
        calls = api(answer, 200)
        post()
        assert len(calls) == 2, calls


def test_only_posts_count_against_the_rate_limit(monkeypatch):
    def ok(method, url):
        return httpx.Response(200, json={"state": "pending"}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "AsyncClient", _stub_client(ok))
    monkeypatch.setattr(config, "RATE_LIMIT", (1, 3600.0))
    monkeypatch.setattr(client, "_CALL_TIMES", {})
    for _ in range(3):
        asyncio.run(client._request("GET", "/v1/async/scrape/1"))
    asyncio.run(client._request("POST", "/v1/scrape", {}))
    try:
        asyncio.run(client._request("POST", "/v1/scrape", {}))
    except ApiError as exc:
        assert "rate limit" in str(exc).lower(), exc
    else:
        raise AssertionError("the second POST must be refused")


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
        assert str(exc) == "Oxylabs Web API key is missing.", exc
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


def test_errors_say_what_kind_they_are():
    def raised(status, body):
        try:
            client._check(_resp(status, body), "/v1/x")
        except ApiError as exc:
            return exc
        raise AssertionError(f"a {status} must raise")

    assert isinstance(raised(401, {"detail": "no"}), ApiAuthError)
    assert isinstance(raised(404, {"detail": "Query not found."}), ApiNotFound)


def _sleeps(monkeypatch) -> list[float]:
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(client.asyncio, "sleep", sleep)
    return slept


def _limited(retry_after: str | None, then: int = 429):
    calls = []

    def respond(method, url):
        calls.append(url)
        if len(calls) > 1 and then != 429:
            return httpx.Response(then, json={"results": []}, request=httpx.Request(method, url))
        headers = {"Retry-After": retry_after} if retry_after else {}
        body = {"status": 429, "title": "RATE_LIMIT_EXCEEDED", "detail": "Slow down."}
        return httpx.Response(429, json=body, headers=headers, request=httpx.Request(method, url))

    return calls, _stub_client(respond)


def test_a_rate_limit_waits_as_long_as_the_api_asks(monkeypatch):
    calls, stub = _limited("3", then=200)
    monkeypatch.setattr(httpx, "AsyncClient", stub)
    slept = _sleeps(monkeypatch)
    asyncio.run(client._request("POST", "/v1/search", {"query": "x"}))
    assert len(calls) == 2 and 3 <= slept[0] <= 3 + config.RETRY_BASE_DELAY, slept


def test_a_long_retry_after_is_reported_not_waited_out(monkeypatch):
    calls, stub = _limited("120")
    monkeypatch.setattr(httpx, "AsyncClient", stub)
    slept = _sleeps(monkeypatch)
    try:
        asyncio.run(client._request("POST", "/v1/search", {"query": "x"}))
    except ApiError as exc:
        assert "wait 120s" in str(exc), exc
    else:
        raise AssertionError("a rate limit that will not clear in time must be reported")
    assert len(calls) == 1 and slept == [], (calls, slept)


def test_a_rate_limit_backs_off_longer_than_a_hiccup(monkeypatch):
    windows = []

    def record(low, high):
        windows.append((low, high))
        return 0

    _, stub = _limited(None)
    monkeypatch.setattr(httpx, "AsyncClient", stub)
    monkeypatch.setattr(random, "uniform", record)
    try:
        asyncio.run(client._request("POST", "/v1/search", {"query": "x"}))
    except ApiError:
        pass
    # Never near zero, unlike a 5xx's backoff, which may retry at once.
    assert windows and all(low >= config.RETRY_BASE_DELAY for low, _ in windows), windows


def test_each_key_has_a_rate_limit_window_of_its_own(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT", (1, 3600.0))
    monkeypatch.setattr(client, "_CALL_TIMES", {})
    with _headers({"authorization": "Bearer caller-a"}):
        client._check_rate_limit()
        try:
            client._check_rate_limit()
        except ApiError:
            pass
        else:
            raise AssertionError("caller-a's window is full")
    with _headers({"authorization": "Bearer caller-b"}):
        client._check_rate_limit()  # caller-a did not use up caller-b's window
    assert all(len(key) == 64 for key in client._CALL_TIMES), "keys are kept only as hashes"


def test_the_http_client_is_shared_and_keeps_no_cookies(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(
            200, json={"results": []}, headers={"set-cookie": "session=a; Path=/"}
        )

    class Mocked(httpx.AsyncClient):
        made = 0

        def __init__(self, **kwargs):
            Mocked.made += 1
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Mocked)

    async def two_callers():
        with _headers({"authorization": "Bearer caller-a"}):
            await client._request("POST", "/v1/search", {"query": "x"})
        with _headers({"authorization": "Bearer caller-b"}):
            await client._request("POST", "/v1/search", {"query": "y"})

    asyncio.run(two_callers())
    assert Mocked.made == 1, "one client, so its connections are reused"
    assert seen == [None, None], "a cookie set on one caller's reply must not reach another"


def test_a_slow_call_tells_the_client_it_is_alive(monkeypatch):
    """On a clock that moves only when the heartbeat waits, so no real time passes."""
    sent = []
    now = [0.0]
    real_wait = asyncio.wait

    class _Ctx:
        async def report_progress(self, progress, total=None, message=None):
            sent.append((progress, message))

    async def call():
        release = asyncio.Event()

        async def slow():
            await release.wait()
            return "done"

        async def fake_wait(tasks, timeout=None):
            now[0] += timeout
            if now[0] >= 3 * config.HEARTBEAT_SECONDS:
                release.set()  # the work finishes during the third wait
                return await real_wait(tasks)
            await asyncio.sleep(0)
            return set(), set(tasks)

        monkeypatch.setattr(client.asyncio, "wait", fake_wait)
        client._start_progress()
        return await client._heartbeat(_Ctx(), slow(), "Waiting for /v1/scrape")

    monkeypatch.setattr(client.time, "monotonic", lambda: now[0])
    assert asyncio.run(call()) == "done"
    # A notification per wait the work outlived, each greater than the one before, as MCP
    # requires, and each saying how long it has been.
    step = config.HEARTBEAT_SECONDS
    assert sent == [
        (1, f"Waiting for /v1/scrape ({step}s)"),
        (2, f"Waiting for /v1/scrape ({2 * step}s)"),
    ], sent

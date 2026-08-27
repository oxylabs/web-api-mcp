"""Offline checks for the bits that aren't just pass-through: error parsing and validation.

Run: python tests/test_server.py   (or: pytest)
"""

import asyncio
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["OXYLABS_WEB_API_KEY"] = "test-key"

import oxylabs_web_api_mcp.server as srv  # noqa: E402
from oxylabs_web_api_mcp.server import (  # noqa: E402
    ApiError,
    _detail,
    check_scrape,
    extract,
    read_scraped,
    scrape,
    scrape_target,
)


def _resp(status: int, json_body=None, text: str = "") -> httpx.Response:
    req = httpx.Request("POST", "https://webapi.oxylabs.io/v1/search")
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=req)
    return httpx.Response(status, text=text, request=req)


def test_detail_flattens_validation_errors():
    body = {
        "status_code": 400,
        "detail": "Validation failed for POST /v1/search",
        "extra": [{"message": "Input should be less than or equal to 20", "key": "max_results"}],
    }
    assert _detail(_resp(400, body)) == "max_results: Input should be less than or equal to 20"


def test_detail_falls_back_to_message_then_text():
    assert _detail(_resp(404, {"message": "Resource not found."})) == "Resource not found."
    assert _detail(_resp(502, text="<html>bad gateway</html>")) == "<html>bad gateway</html>"


def test_search_rejects_bad_input_before_calling_the_api():
    for kwargs in (
        {"query": "   "},
        {"query": "ok", "max_results": 0},
        {"query": "ok", "max_results": 21},
    ):
        try:
            asyncio.run(srv.mcp.call_tool("search", kwargs))
        except Exception:  # noqa: BLE001 — ApiError or FastMCP's own validation error
            continue
        raise AssertionError(f"expected an error for {kwargs}")


def test_scrape_rejects_relative_urls():
    try:
        asyncio.run(scrape("www.example.com"))
    except ApiError:
        return
    raise AssertionError("expected ApiError for a non-absolute URL")


def _big_payload(chars: int) -> dict:
    return {
        "status": "done",
        "results": [{"content": "x" * chars, "url": "https://ex.com/a"}],
        "params": {"url": "https://ex.com/a"},
    }


def test_oversized_content_spills_to_disk_when_local(tmp_dir=None):
    import tempfile

    spill = tmp_dir or tempfile.mkdtemp()
    os.environ["OXYLABS_SPILL_DIR"] = spill
    srv._SPILL_ENABLED = True
    try:
        size = srv.MAX_INLINE_TOKENS * 4 + 5000
        out = srv._process_content(_big_payload(size), "markdown", srv.MAX_INLINE_TOKENS)
        result = out["results"][0]
        info = result["content_offloaded"]

        assert info["total_chars"] == size, info
        assert len(result["content"]) == srv.PREVIEW_CHARS, len(result["content"])
        assert "content_truncated" not in result
        assert os.path.isfile(info["path"]), info

        # The agent walks the file in chunks and reaches the end.
        first = asyncio.run(read_scraped(info["path"], offset=0, length=1000))
        assert first["returned_chars"] == 1000 and not first["eof"], first
        last = asyncio.run(read_scraped(info["path"], offset=size - 10, length=1000))
        assert last["eof"] and last["returned_chars"] == 10, last
    finally:
        srv._SPILL_ENABLED = False
        del os.environ["OXYLABS_SPILL_DIR"]


def test_oversized_content_truncates_when_remote():
    srv._SPILL_ENABLED = False
    size = srv.MAX_INLINE_TOKENS * 4 + 5000
    result = srv._process_content(_big_payload(size), "markdown", srv.MAX_INLINE_TOKENS)["results"][
        0
    ]
    assert result["content_truncated"]["total_chars"] == size
    assert result["content_truncated"]["returned_chars"] < size
    assert "content_offloaded" not in result


def test_small_content_is_left_alone():
    out = srv._process_content(_big_payload(100), "markdown", srv.MAX_INLINE_TOKENS)["results"][0]
    assert out["content"] == "x" * 100
    assert "content_offloaded" not in out and "content_truncated" not in out


def test_read_scraped_refuses_paths_outside_the_spill_dir():
    for bad in ("/etc/passwd", "/etc/hosts.txt", os.path.join(os.getcwd(), "pyproject.toml")):
        try:
            asyncio.run(read_scraped(bad))
        except ApiError:
            continue
        raise AssertionError(f"read_scraped must refuse {bad}")


def test_scrape_rejects_unknown_format():
    try:
        asyncio.run(srv.mcp.call_tool("scrape", {"url": "https://ex.com", "format": "pdf"}))
    except Exception:  # noqa: BLE001 — FastMCP rejects it before the tool body runs
        return
    raise AssertionError("expected ApiError for an unsupported format")


# --------------------------------------------------------------------- new behaviour


class _FakeCtx:
    """Just enough of a FastMCP Context for the bits that elicit and log."""

    def __init__(self, elicitation=None, answer=None):
        caps = type("Caps", (), {"elicitation": elicitation})()
        params = type("Params", (), {"capabilities": caps})()
        self.session = type("Session", (), {"client_params": params})()
        self._answer = answer
        self.messages = []

    async def info(self, message):
        self.messages.append(message)

    async def elicit(self, message, response_type):
        action, proceed = self._answer
        data = type("Data", (), {"proceed": proceed})()
        return type("Result", (), {"action": action, "data": data})()


class _headers:
    """Stand in for the headers of an in-flight HTTP request."""

    def __init__(self, headers):
        self.headers = headers

    def __enter__(self):
        self._real = srv._http_headers
        srv._http_headers = lambda: self.headers
        return self

    def __exit__(self, *exc):
        srv._http_headers = self._real
        return False


def test_bearer_header_beats_the_environment():
    with _headers({"authorization": "Bearer from-header"}):
        assert srv._api_key() == "from-header"
    # No usable header falls back to the environment.
    with _headers({"authorization": "Basic nope"}):
        assert srv._api_key() == "test-key"
    assert srv._api_key() == "test-key"


def test_missing_key_everywhere_is_an_actionable_error():
    saved = os.environ.pop("OXYLABS_WEB_API_KEY")
    try:
        srv._api_key()
    except ApiError as exc:
        assert "Authorization: Bearer" in str(exc), exc
    else:
        raise AssertionError("expected ApiError when no key is available")
    finally:
        os.environ["OXYLABS_WEB_API_KEY"] = saved


def test_trim_drops_the_echo_but_keeps_the_request_id():
    out = srv._trim(
        {
            "status": "done",
            "results": [1],
            "params": {"query": "x"},
            "metadata": {"timestamp": 1, "request_id": "abc"},
        }
    )
    assert out == {"status": "done", "results": [1], "request_id": "abc"}, out
    # Nothing to keep is fine too.
    assert srv._trim({"results": []}) == {"results": []}


def test_endpoint_names_cannot_walk_the_path():
    assert srv._endpoint_name("scrape") == "scrape"
    assert srv._endpoint_name(" amazon_search ") == "amazon_search"
    for bad in ("../admin", "scrape?x=1", "v1/../../etc", "/v1/scrape", "HTTP://evil", ""):
        try:
            srv._endpoint_name(bad)
        except ApiError:
            continue
        raise AssertionError(f"_endpoint_name must refuse {bad!r}")


def _run_js_job(call):
    """Run a run_js tool call against a stubbed API and poll it to completion."""

    async def fake_request(method, path, payload=None, **kwargs):
        return {"results": [{"content": "rendered", "url": payload["url"]}], "params": payload}

    real = srv._request
    srv._request = fake_request
    try:
        started = asyncio.run(call())
        assert started["status"] == "running" and started["job_id"], started

        async def drain():
            await srv._JOBS[started["job_id"]]["task"]
            return await check_scrape(started["job_id"])

        return started, asyncio.run(drain())
    finally:
        srv._request = real


def test_run_js_returns_a_job_to_poll_and_then_the_content():
    started, done = _run_js_job(lambda: scrape("https://ex.com", run_js=True))
    assert "check_scrape" in started["note"]
    assert done["status"] == "done"
    assert done["result"]["results"][0]["content"] == "rendered", done
    # The envelope echo is gone by the time the agent sees it.
    assert "params" not in done["result"]


def test_scrape_target_routes_run_js_through_the_same_job_path():
    started, done = _run_js_job(
        lambda: scrape_target("amazon_search", {"url": "https://ex.com", "run_js": True})
    )
    assert done["status"] == "done" and started["status"] == "running"


def test_check_scrape_reports_a_failed_job_instead_of_hanging():
    async def failing(method, path, payload=None, **kwargs):
        raise ApiError("upstream exploded")

    real = srv._request
    srv._request = failing
    try:
        started = asyncio.run(scrape("https://ex.com", run_js=True))

        async def drain():
            await srv._JOBS[started["job_id"]]["task"]
            return await check_scrape(started["job_id"])

        try:
            asyncio.run(drain())
        except ApiError as exc:
            assert "upstream exploded" in str(exc), exc
        else:
            raise AssertionError("a failed job must surface as an error, not a result")
    finally:
        srv._request = real


def test_a_slow_render_is_not_reported_as_a_stuck_one():
    """A job inside the 150s window must read as normal, past it as still-worth-polling.

    The failure this guards is an agent abandoning a healthy render and starting over,
    which doubles the bill and the wait.
    """

    def _running(age):
        return {
            "status": "running",
            "created": time.time() - age,
            "url": "https://ex.com",
            "result": None,
            "error": None,
        }

    srv._JOBS["slow"] = _running(90)
    srv._JOBS["late"] = _running(srv.JS_RENDER_MAX_SECONDS + 60)
    try:
        slow = asyncio.run(check_scrape("slow"))
        assert slow["elapsed_seconds"] >= 90, slow
        assert "normal" in slow["note"] and str(srv.JS_RENDER_MAX_SECONDS) in slow["note"], slow

        late = asyncio.run(check_scrape("late"))
        assert f"past the {srv.JS_RENDER_MAX_SECONDS}s" in late["note"], late
        # Past the ceiling it still says to poll, not to restart: the server waits longer.
        assert "poll again" in late["note"], late
    finally:
        srv._JOBS.pop("slow", None)
        srv._JOBS.pop("late", None)


def test_check_scrape_rejects_an_unknown_job():
    try:
        asyncio.run(check_scrape("nope"))
    except ApiError:
        return
    raise AssertionError("expected ApiError for an unknown job id")


def test_extract_needs_the_user_to_approve_the_extra_cost():
    calls = []

    async def fake_request(method, path, payload=None, **kwargs):
        calls.append(payload)
        return {"results": [{"content": {"title": "t"}}], "params": payload}

    real = srv._request
    srv._request = fake_request
    try:
        # Declined: nothing is billed.
        try:
            asyncio.run(
                extract(
                    "https://ex.com",
                    "the title",
                    ctx=_FakeCtx(elicitation={}, answer=("decline", False)),
                )
            )
        except ApiError as exc:
            assert "declined" in str(exc), exc
        else:
            raise AssertionError("a declined elicitation must stop the call")
        assert not calls, "declining must not reach the API"

        # A client that cannot ask is refused rather than billed silently.
        try:
            asyncio.run(extract("https://ex.com", "the title", ctx=_FakeCtx()))
        except ApiError as exc:
            assert "elicitation" in str(exc), exc
        else:
            raise AssertionError("no elicitation support must stop the call")
        assert not calls

        # Accepted: the prompt goes out as a json output request.
        out = asyncio.run(
            extract(
                "https://ex.com", "the title", ctx=_FakeCtx(elicitation={}, answer=("accept", True))
            )
        )
        assert calls[0]["json"] == {"prompt": "the title"}, calls
        assert calls[0][srv.OUTPUT_PARAM] == ["json"]
        assert out["results"][0]["content"] == {"title": "t"}
    finally:
        srv._request = real


def test_extract_approval_can_be_waived_by_the_operator():
    async def fake_request(method, path, payload=None, **kwargs):
        return {"results": [{"content": {}}], "params": payload}

    real = srv._request
    srv._request = fake_request
    os.environ["OXYLABS_EXTRACT_APPROVAL"] = "0"
    try:
        asyncio.run(extract("https://ex.com", "the title"))
    finally:
        srv._request = real
        del os.environ["OXYLABS_EXTRACT_APPROVAL"]


def _page(content, output="markdown"):
    return {
        "results": [{"content": content, "url": "https://ex.com/a"}],
        "params": {"output": [output]},
    }


def test_an_empty_shell_is_flagged_for_a_js_retry():
    out = srv._flag_thin_content(_page("# Loading\n\n"))["results"][0]
    assert out["content_thin"]["visible_chars"] < srv.THIN_CONTENT_CHARS, out
    assert "run_js=True" in out["content_thin"]["note"]


def test_a_real_page_is_left_alone():
    out = srv._flag_thin_content(_page("word " * 400))["results"][0]
    assert "content_thin" not in out, out


def test_html_is_measured_on_its_visible_text_not_its_markup():
    shell = (
        "<html><head>"
        + '<meta name="x" content="y">' * 60
        + '</head><body><div id="root"></div><script>var a=1;</script></body></html>'
    )
    assert len(shell) > srv.THIN_CONTENT_CHARS
    out = srv._flag_thin_content(_page(shell, output="html"))["results"][0]
    assert "content_thin" in out, out


def test_a_javascript_notice_is_flagged_even_above_the_length_floor():
    page = "Home About Contact " * 40 + " You need to enable JavaScript to run this app."
    assert len(page) > srv.THIN_CONTENT_CHARS
    out = srv._flag_thin_content(_page(page))["results"][0]
    assert out["content_thin"]["reason"] == "the page says it needs JavaScript", out


def test_a_long_article_about_javascript_is_not_a_shell():
    article = "Turn on JavaScript to see the demo. " + ("prose " * 2000)
    out = srv._flag_thin_content(_page(article))["results"][0]
    assert "content_thin" not in out, out


def test_offloaded_pages_are_never_called_thin():
    payload = _page("x" * (srv.MAX_INLINE_TOKENS * 4 + 100))
    srv._SPILL_ENABLED = False
    out = srv._flag_thin_content(srv._process_content(payload, "markdown", srv.MAX_INLINE_TOKENS))[
        "results"
    ][0]
    assert "content_truncated" in out and "content_thin" not in out, out


# ------------------------------------------------------- hardening from the competitor survey


def test_client_name_cannot_inject_a_header():
    assert srv._sanitize_client_field("evil\r\nX-Injected: 1") == "evil  X-Injected: 1"
    assert len(srv._sanitize_client_field("x" * 5000)) == 64
    assert srv._sanitize_client_field("  \t  ") is None
    assert srv._sanitize_client_field(None) is None

    # And the header it builds is single-line whatever the client called itself.
    class _Bad:
        class request_context:
            class session:
                class client_params:
                    class clientInfo:
                        name = "a\r\nb"

    header = srv._sdk_header(_Bad())
    assert "\r" not in header and "\n" not in header, header


def test_token_estimate_is_script_aware():
    latin = "word " * 200  # 1000 chars of English
    cjk = "\u4e2d" * 1000  # 1000 Chinese characters
    assert srv._estimate_tokens(latin) < 300, srv._estimate_tokens(latin)
    assert srv._estimate_tokens(cjk) == 1000, srv._estimate_tokens(cjk)
    # The old char-based rule treated these as equal; they are 4x apart.
    assert srv._estimate_tokens(cjk) > srv._estimate_tokens(latin) * 3


def test_a_cjk_page_that_fits_in_chars_is_still_offloaded():
    srv._SPILL_ENABLED = False
    page = {"results": [{"content": "\u4e2d" * 20000, "url": "https://ex.com"}], "params": {}}
    out = srv._process_content(page, "markdown", srv.MAX_INLINE_TOKENS)["results"][0]
    assert "content_truncated" in out, "20k CJK chars is ~20k tokens and must not go inline"
    assert out["content_truncated"]["estimated_tokens"] == 20000


def test_client_can_declare_its_own_token_budget():
    assert srv._token_budget() == srv.MAX_INLINE_TOKENS
    with _headers({"x-mcp-max-tokens": "50000"}):
        assert srv._token_budget() == 50000
    with _headers({"x-mcp-max-tokens": "0"}):
        assert srv._token_budget() is None
    with _headers({"x-mcp-max-tokens": "nonsense"}):
        assert srv._token_budget() == srv.MAX_INLINE_TOKENS


def test_truncation_notice_says_how_to_get_the_rest():
    srv._SPILL_ENABLED = False
    page = {"results": [{"content": "word " * 40000, "url": "https://ex.com"}], "params": {}}
    note = srv._process_content(page, "markdown", 1000)["results"][0]["content_truncated"]["note"]
    for hint in ("X-MCP-Max-Tokens", "stdio", "extract"):
        assert hint in note, note


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

    real_client, real_delay = httpx.AsyncClient, srv.RETRY_BASE_DELAY
    httpx.AsyncClient, srv.RETRY_BASE_DELAY = _Client, 0
    try:
        out = asyncio.run(srv._request("POST", "/v1/search", {"query": "x"}))
        assert out == {"results": []}
        assert len(calls) == 3, calls
    finally:
        httpx.AsyncClient, srv.RETRY_BASE_DELAY = real_client, real_delay


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

    real_client, real_delay = httpx.AsyncClient, srv.RETRY_BASE_DELAY
    httpx.AsyncClient, srv.RETRY_BASE_DELAY = _Client, 0
    try:
        asyncio.run(srv._request("POST", "/v1/search", {"query": "x"}))
    except ApiError as exc:
        assert "502" in str(exc), exc
    else:
        raise AssertionError("expected ApiError after exhausting retries")
    finally:
        httpx.AsyncClient, srv.RETRY_BASE_DELAY = real_client, real_delay


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
        asyncio.run(srv._request("POST", "/v1/search", {"query": "x"}))
    except ApiError:
        assert len(calls) == 1, "a validation error is the caller's bug; retrying wastes quota"
    finally:
        httpx.AsyncClient = real_client


def test_rate_limit_parsing_and_enforcement():
    assert srv._parse_rate_limit("") is None
    assert srv._parse_rate_limit("100/1h") == (100, 3600.0)
    assert srv._parse_rate_limit("50/30m") == (50, 1800.0)
    try:
        srv._parse_rate_limit("lots")
    except ValueError:
        pass
    else:
        raise AssertionError("a malformed rate limit must fail loudly at startup")

    real_limit, real_times = srv.RATE_LIMIT, list(srv._CALL_TIMES)
    srv.RATE_LIMIT, srv._CALL_TIMES[:] = (2, 3600.0), []
    try:
        srv._check_rate_limit()
        srv._check_rate_limit()
        try:
            srv._check_rate_limit()
        except ApiError as exc:
            assert "rate limit" in str(exc).lower(), exc
        else:
            raise AssertionError("the third call must be refused")
    finally:
        srv.RATE_LIMIT, srv._CALL_TIMES[:] = real_limit, real_times


def test_the_skill_ships_inside_the_package():
    text = srv._skill_text()
    assert not text.startswith("---"), "frontmatter is for a skill loader, not a resource"
    assert "search" in text and "run_js" in text, text[:200]


def _stub_client(responder):
    """Swap httpx.AsyncClient for one that answers from `responder(method, url)`."""

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, json=None, headers=None):
            return responder(method, url)

    return _Client


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

    real_client, real_delay = httpx.AsyncClient, srv.RETRY_BASE_DELAY
    httpx.AsyncClient, srv.RETRY_BASE_DELAY = _stub_client(cleared), 0
    try:
        assert asyncio.run(srv._request("POST", "/v1/search", {"query": "x"})) == {"results": []}
        assert len(calls) == 3, "a rate limit clears on its own; it is worth retrying"

        stuck = _stub_client(
            lambda m, u: httpx.Response(429, json={"message": "no"}, request=httpx.Request(m, u))
        )
        httpx.AsyncClient = stuck
        try:
            asyncio.run(srv._request("POST", "/v1/search", {"query": "x"}))
        except ApiError as exc:
            # Backoff cannot clear a spent quota, so the message has to send the human
            # to the dashboard rather than inviting another round of retries.
            assert "quota" in str(exc).lower(), exc
        else:
            raise AssertionError("expected ApiError once the retries are spent")
    finally:
        httpx.AsyncClient, srv.RETRY_BASE_DELAY = real_client, real_delay


def test_backoff_is_jittered_so_callers_do_not_retry_in_lockstep():
    windows = []

    def _record(low, high):
        windows.append((low, high))
        return 0

    stub = _stub_client(
        lambda m, u: httpx.Response(503, json={"message": "down"}, request=httpx.Request(m, u))
    )
    real_client, real_uniform = httpx.AsyncClient, srv.random.uniform
    httpx.AsyncClient, srv.random.uniform = stub, _record
    try:
        try:
            asyncio.run(srv._request("POST", "/v1/search", {"query": "x"}))
        except ApiError:
            pass
        assert windows, "a retry must sleep a random amount, not a fixed one"
        assert all(low == 0 for low, _ in windows), windows
        highs = [high for _, high in windows]
        assert highs == sorted(highs) and highs[-1] > highs[0], highs
    finally:
        httpx.AsyncClient, srv.random.uniform = real_client, real_uniform


def test_a_2xx_carrying_faulted_is_not_treated_as_success():
    stub = _stub_client(
        lambda m, u: httpx.Response(
            200,
            json={"status": "faulted", "results": [], "metadata": {"request_id": "req-42"}},
            request=httpx.Request(m, u),
        )
    )
    real_client = httpx.AsyncClient
    httpx.AsyncClient = stub
    try:
        asyncio.run(srv._request("POST", "/v1/search", {"query": "x"}))
    except ApiError as exc:
        assert "faulted" in str(exc) and "req-42" in str(exc), exc
    else:
        raise AssertionError("a 2xx with status=faulted is a failure, not a result")
    finally:
        httpx.AsyncClient = real_client


def test_env_file_fills_unset_oxylabs_vars_and_nothing_else():
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, ".env")
        with open(path, "w") as fh:
            fh.write(
                "# a comment\n"
                "\n"
                'export OXYLABS_WEB_API_KEY="from-file"\n'
                "OXYLABS_TIMEOUT=7\n"
                "PATH=/definitely/not\n"
            )

        saved_path, saved_timeout = os.environ["PATH"], os.environ.pop("OXYLABS_TIMEOUT", None)
        os.environ["OXYLABS_ENV_FILE"] = path
        try:
            srv._load_env_file()
            # The real environment wins: the key is already set, so the file must not win.
            assert os.environ["OXYLABS_WEB_API_KEY"] == "test-key"
            assert os.environ["OXYLABS_TIMEOUT"] == "7"
            assert os.environ["PATH"] == saved_path, "only OXYLABS_* may come from a .env"

            # An MCP config interpolating ${OXYLABS_WEB_API_KEY} passes "" when it is
            # unset; that must not shadow the file.
            saved_key = os.environ["OXYLABS_WEB_API_KEY"]
            os.environ["OXYLABS_WEB_API_KEY"] = ""
            try:
                srv._load_env_file()
                assert os.environ["OXYLABS_WEB_API_KEY"] == "from-file"
            finally:
                os.environ["OXYLABS_WEB_API_KEY"] = saved_key
        finally:
            os.environ.pop("OXYLABS_ENV_FILE", None)
            os.environ.pop("OXYLABS_TIMEOUT", None)
            if saved_timeout is not None:
                os.environ["OXYLABS_TIMEOUT"] = saved_timeout


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all checks passed")

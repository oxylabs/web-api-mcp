"""Offline checks for the bits that aren't just pass-through: error parsing and validation.

Run: python tests/test_server.py   (or: pytest)
"""

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("OXYLABS_API_KEY", "test-key")

import oxylabs_web_api_mcp.server as srv  # noqa: E402
from oxylabs_web_api_mcp.server import (  # noqa: E402
    ApiError,
    _detail,
    check_scrape,
    extract,
    read_scraped,
    scrape,
    scrape_target,
    search,
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
            asyncio.run(search(**kwargs))
        except ApiError:
            continue
        raise AssertionError(f"expected ApiError for {kwargs}")


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
        size = srv.MAX_INLINE_CHARS + 5000
        out = srv._process_content(_big_payload(size), "markdown")
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
    size = srv.MAX_INLINE_CHARS + 5000
    result = srv._process_content(_big_payload(size), "markdown")["results"][0]
    assert result["content_truncated"]["total_chars"] == size
    assert len(result["content"]) == srv.MAX_INLINE_CHARS
    assert "content_offloaded" not in result


def test_small_content_is_left_alone():
    out = srv._process_content(_big_payload(100), "markdown")["results"][0]
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
        asyncio.run(scrape("https://ex.com", format="pdf"))
    except ApiError:
        return
    raise AssertionError("expected ApiError for an unsupported format")


# --------------------------------------------------------------------- new behaviour


class _FakeCtx:
    """Just enough Context for the bits that read headers and capabilities."""

    def __init__(self, headers=None, elicitation=None, answer=None):
        self.headers = headers
        self.client_capabilities = type("Caps", (), {"elicitation": elicitation})()
        self._answer = answer
        self.messages = []

    async def info(self, message):
        self.messages.append(message)

    async def elicit(self, message, schema):
        action, proceed = self._answer
        data = type("Data", (), {"proceed": proceed})()
        return type("Result", (), {"action": action, "data": data})()


def test_bearer_header_beats_the_environment():
    ctx = _FakeCtx(headers={"authorization": "Bearer from-header"})
    assert srv._api_key(ctx) == "from-header"
    # No usable header falls back to the environment.
    assert srv._api_key(_FakeCtx(headers={"authorization": "Basic nope"})) == "test-key"
    assert srv._api_key(None) == "test-key"


def test_missing_key_everywhere_is_an_actionable_error():
    saved = os.environ.pop("OXYLABS_API_KEY")
    try:
        srv._api_key(_FakeCtx())
    except ApiError as exc:
        assert "Authorization: Bearer" in str(exc), exc
    else:
        raise AssertionError("expected ApiError when no key is available")
    finally:
        os.environ["OXYLABS_API_KEY"] = saved


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
    assert srv._endpoint_name("/v1/amazon_search/") == "amazon_search"
    for bad in ("../admin", "scrape?x=1", "v1/../../etc", "HTTP://evil", ""):
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
    payload = _page("x" * (srv.MAX_INLINE_CHARS + 100))
    srv._SPILL_ENABLED = False
    out = srv._flag_thin_content(srv._process_content(payload, "markdown"))["results"][0]
    assert "content_truncated" in out and "content_thin" not in out, out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all checks passed")

"""Offline checks for the bits that aren't just pass-through: error parsing and validation.

Run: python tests/test_server.py   (or: pytest)
"""

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["OXYLABS_WEB_API_KEY"] = "test-key"

import oxylabs_web_api_mcp.server as srv  # noqa: E402
from oxylabs_web_api_mcp.server import (  # noqa: E402
    ApiError,
    _detail,
    check_scrape,
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
        "status": 400,
        "title": "INVALID_BODY_PROPERTY",
        "detail": "A body property has an invalid value.",
        "errors": [{"detail": "Invalid type, integer is supported.", "pointer": "/pages"}],
    }
    assert _detail(_resp(400, body)) == "/pages: Invalid type, integer is supported."


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
    # The live API's result shape: page text under the format's own key, the scraped
    # URL under the result's `metadata` — there is no fused `content` field.
    return {
        "state": "done",
        "results": [{"markdown": "x" * chars, "metadata": {"url": "https://ex.com/a"}}],
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
        assert len(result["markdown"]) == srv.PREVIEW_CHARS, len(result["markdown"])
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
    assert out["markdown"] == "x" * 100
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
    """Just enough of a FastMCP Context for the bits that log."""

    def __init__(self):
        self.messages = []

    async def info(self, message):
        self.messages.append(message)


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
            "state": "done",
            "results": [1],
            "params": {"query": "x"},
            "metadata": {"timestamp": 1, "request_id": "abc"},
        }
    )
    assert out == {"state": "done", "results": [1], "request_id": "abc"}, out
    # Nothing to keep is fine too.
    assert srv._trim({"results": []}) == {"results": []}


def test_endpoint_names_cannot_walk_the_path():
    assert srv._endpoint_name("scrape") == "scrape"
    # /v1/scrapers returns slash-separated source paths; those must pass whole.
    assert srv._endpoint_name("scrape/amazon/search") == "scrape/amazon/search"
    assert srv._endpoint_name("/v1/scrape/youtube/search/max/") == "scrape/youtube/search/max"
    for bad in ("../admin", "scrape?x=1", "v1/../../etc", "scrape/../etc", "HTTP://evil", ""):
        try:
            srv._endpoint_name(bad)
        except ApiError:
            continue
        raise AssertionError(f"_endpoint_name must refuse {bad!r}")


GROUPS = {
    "scrapers": {
        "json_not_supported": ["/v1/async/scrape/media"],
        "json_supported": [
            "/v1/scrape/amazon/product",
            "/v1/async/scrape/chatgpt",
        ],
        "json_supported_only_with_prompt_or_schema": ["/v1/scrape", "/v1/async/scrape"],
    }
}


class _api:
    """Stub `_request`: answer each POST from `answers` in turn, and the parser groups."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, method, path, payload=None, **kwargs):
        if path.startswith("/v1/scrapers"):
            self.calls.append((method, path, None))
            return GROUPS
        self.calls.append((method, path, payload))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(payload)
        return answer

    def __enter__(self):
        self._real = srv._request
        srv._request = self
        srv._JSON_GROUPS.clear()
        return self

    def __exit__(self, *exc):
        srv._request = self._real
        srv._JSON_GROUPS.clear()
        return False

    @property
    def posts(self):
        return [(path, payload) for method, path, payload in self.calls if method == "POST"]


def _pending(payload):
    return {
        "state": "pending",
        "params": payload,
        "metadata": {"request_id": "7500000000000000001"},
    }


def _done(payload, **result):
    return {"state": "done", "params": payload, "results": [result]}


def test_run_js_goes_sync_first_and_returns_the_content():
    with _api(lambda p: _done(p, markdown="word " * 400)) as api:
        out = asyncio.run(scrape("https://ex.com", run_js=True))
    assert api.posts == [("/v1/scrape", api.posts[0][1])], api.posts
    assert api.posts[0][1]["run_js"] is True
    assert out["results"][0]["markdown"].startswith("word"), out


def test_a_sync_timeout_is_sent_to_the_async_endpoint():
    timeout = srv.ApiTimeout("/v1/scrape returned 500: Scraping took too long, try again.")
    with _api(timeout, _pending) as api:
        out = asyncio.run(scrape("https://ex.com", run_js=True))
    assert [path for path, _ in api.posts] == ["/v1/scrape", "/v1/async/scrape"], api.posts
    assert api.posts[0][1] == api.posts[1][1], "the queued request must be the same payload"
    assert out["state"] == "pending" and out["request_id"] == "7500000000000000001"
    assert "check_scrape('7500000000000000001')" in out["note"]
    assert "timed out" in out["note"], out["note"]
    assert "params" not in out and "metadata" not in out


def test_a_gateway_timeout_on_sync_is_retried_not_queued():
    calls = []

    def gateway_timeout(method, url):
        calls.append(url)
        if len(calls) == 1:
            return httpx.Response(504, text="Gateway Timeout", request=httpx.Request(method, url))
        done = {"state": "done", "results": [{"markdown": "x" * 600}], "params": {}}
        return httpx.Response(200, json=done, request=httpx.Request(method, url))

    real_client, real_delay = httpx.AsyncClient, srv.RETRY_BASE_DELAY
    httpx.AsyncClient, srv.RETRY_BASE_DELAY = _stub_client(gateway_timeout), 0
    try:
        asyncio.run(scrape("https://ex.com"))
    finally:
        httpx.AsyncClient, srv.RETRY_BASE_DELAY = real_client, real_delay
    assert [u.rsplit("/v1/", 1)[1] for u in calls] == ["scrape", "scrape"], calls


def test_scrape_sends_the_location_in_uppercase():
    with _api(lambda p: _done(p, markdown="x" * 600)) as api:
        asyncio.run(scrape("https://ex.com", location=" de "))
    assert api.posts[0][1]["location"] == "DE", api.posts


def test_scrape_target_runs_sync_even_with_run_js_and_queues_async_endpoints():
    with _api(lambda p: _done(p, html="<p>x</p>")) as api:
        asyncio.run(scrape_target("scrape/amazon/search", {"query": "x", "run_js": True}))
    assert api.posts[0][0] == "/v1/scrape/amazon/search", api.posts

    with _api(_pending) as api:
        out = asyncio.run(scrape_target("async/scrape/chatgpt", {"prompt": "hi"}))
    assert api.posts[0][0] == "/v1/async/scrape/chatgpt"
    assert "check_scrape('7500000000000000001')" in out["note"]


def test_check_scrape_polls_the_api_for_the_request_id():
    calls = []

    async def fake_request(method, path, payload=None, **kwargs):
        calls.append((method, path))
        if path.endswith("/111"):
            return {"state": "pending"}
        if path.endswith("/404404"):
            raise ApiError(f"{path} returned 404: Query not found.")
        return {
            "state": "done",
            "results": [{"markdown": "answer"}],
            "params": {"output": ["markdown"]},
            "metadata": {"request_id": "7504857924934611969"},
        }

    real = srv._request
    srv._request = fake_request
    try:
        pending = asyncio.run(check_scrape("111"))
        assert pending["state"] == "pending" and "5s" in pending["note"], pending

        done = asyncio.run(check_scrape("7504857924934611969"))
        assert done["state"] == "done"
        assert done["result"]["results"][0]["markdown"] == "answer"
        assert done["result"]["request_id"] == "7504857924934611969"
        assert "params" not in done["result"]
        assert calls[-1] == ("GET", "/v1/async/scrape/7504857924934611969")

        for bad in ("nothex", "404404"):
            try:
                asyncio.run(check_scrape(bad))
            except ApiError as exc:
                assert "No request" in str(exc) or "not a request id" in str(exc), exc
            else:
                raise AssertionError(f"{bad!r} must surface as an unknown request")
        # The non-numeric id never reached the API; the 404 one did, once.
        assert len(calls) == 3
    finally:
        srv._request = real


def test_extract_on_a_url_uses_the_ai_parser_without_asking():
    with _api(lambda p: _done(p, json={"title": "t"})) as api:
        out = asyncio.run(scrape("https://ex.com", json_prompt="the title", ctx=_FakeCtx()))
    assert api.calls[0][1] == "/v1/scrapers?group_by=json_output_support", api.calls
    ((path, payload),) = api.posts
    assert path == "/v1/scrape"
    assert payload[srv.OUTPUT_PARAM] == ["json"]
    assert payload["json"] == {"prompt": "the title"}
    assert out["parser"] == "ai" and out["results"][0]["json"] == {"title": "t"}


def test_extract_passes_a_schema_to_the_ai_parser():
    schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    with _api(lambda p: _done(p, json={"title": "t"})) as api:
        asyncio.run(scrape("https://ex.com", json_schema=schema))
    assert api.posts[0][1]["json"] == {"schema": schema}


def test_the_dedicated_parser_runs_first_where_there_is_one():
    with _api(lambda p: _done(p, json={"title": "t", "price": 9})) as api:
        out = asyncio.run(
            scrape_target("scrape/amazon/product", {"query": "B0"}, json_prompt="title and price")
        )
    ((path, payload),) = api.posts
    assert path == "/v1/scrape/amazon/product"
    assert payload[srv.OUTPUT_PARAM] == ["json"] and "json" not in payload, payload
    assert out["parser"] == "dedicated" and "parser='ai'" in out["parser_note"]


def test_an_empty_dedicated_result_falls_back_to_the_ai_parser():
    with _api(lambda p: _done(p, json=None), lambda p: _done(p, json={"title": "t"})) as api:
        out = asyncio.run(
            scrape_target("scrape/amazon/product", {"query": "B0"}, json_prompt="the title")
        )
    assert [payload.get("json") for _, payload in api.posts] == [None, {"prompt": "the title"}]
    assert out["parser"] == "ai"


def test_parser_ai_skips_the_dedicated_parser():
    with _api(lambda p: _done(p, json={"seller": "s"})) as api:
        asyncio.run(
            scrape_target(
                "scrape/amazon/product", {"query": "B0"}, json_prompt="the seller", parser="ai"
            )
        )
    assert len(api.posts) == 1 and api.posts[0][1]["json"] == {"prompt": "the seller"}


def test_structured_without_a_prompt_uses_the_dedicated_parser_only():
    with _api(lambda p: _done(p, json=None)) as api:
        out = asyncio.run(scrape_target("scrape/amazon/product", {"query": "B0"}, structured=True))
    assert len(api.posts) == 1 and out["parser"] == "dedicated"


def test_structured_needs_a_prompt_where_there_is_no_dedicated_parser():
    with _api() as api:
        try:
            asyncio.run(scrape_target("scrape", {"url": "https://ex.com"}, structured=True))
        except ApiError as exc:
            assert "no dedicated parser" in str(exc), exc
        else:
            raise AssertionError("expected ApiError without json_prompt on a prompt-only endpoint")
    assert not api.posts


def test_endpoints_without_json_refuse_structured_output():
    with _api() as api:
        try:
            asyncio.run(scrape_target("async/scrape/media", {"url": "x"}, json_prompt="anything"))
        except ApiError as exc:
            assert "cannot return structured JSON" in str(exc), exc
        else:
            raise AssertionError("json_not_supported must be refused")
    assert not api.posts


def test_a_queued_dedicated_parse_says_how_to_fall_back():
    with _api(_pending) as api:
        out = asyncio.run(
            scrape_target("async/scrape/chatgpt", {"prompt": "hi"}, json_prompt="the citations")
        )
    assert api.posts[0][0] == "/v1/async/scrape/chatgpt" and "json" not in api.posts[0][1]
    assert out["parser"] == "dedicated" and "request_id" in out
    assert "check_scrape" in out["parser_note"], out["parser_note"]


def test_a_queued_ai_parse_is_not_called_extracted():
    timeout = srv.ApiTimeout("/v1/scrape returned 500: REQUEST_FAILED_SCRAPE_TIMEOUT")
    with _api(timeout, _pending):
        out = asyncio.run(scrape("https://ex.com", json_prompt="the title"))
    assert out["parser"] == "ai" and out["state"] == "pending", out
    assert "Queued" in out["parser_note"] and "Extracted" not in out["parser_note"]


def test_parser_ai_without_a_prompt_says_what_it_needs():
    with _api() as api:
        try:
            asyncio.run(
                scrape_target(
                    "scrape/amazon/product", {"query": "B0"}, structured=True, parser="ai"
                )
            )
        except ApiError as exc:
            assert "parser='ai' needs" in str(exc) and "no dedicated" not in str(exc), exc
        else:
            raise AssertionError("parser='ai' without a prompt must be refused")
    assert not api.posts


def test_an_unknown_group_still_tries_the_dedicated_parser_without_a_prompt():
    async def broken(method, path, payload=None, **kwargs):
        if path.startswith("/v1/scrapers"):
            raise ApiError("/v1/scrapers returned 500: down")
        posts.append(payload)
        return _done(payload, json={"title": "t"})

    posts = []
    real = srv._request
    srv._request = broken
    srv._JSON_GROUPS.clear()
    try:
        out = asyncio.run(scrape_target("scrape/amazon/product", {"query": "B0"}, structured=True))
    finally:
        srv._request = real
    assert posts == [{"query": "B0", "output": ["json"]}], posts
    assert out["parser"] == "dedicated"


def test_a_faulted_queued_dedicated_parse_suggests_the_ai_parser():
    params = {"output": ["json"], "json": {"prompt": None, "schema": None}}
    fault = srv.ApiFaulted("faulted (PARSE_FAILED)", tag="PARSE_FAILED", params=params)

    async def fake_request(method, path, payload=None, **kwargs):
        raise fault

    real = srv._request
    srv._request = fake_request
    try:
        asyncio.run(check_scrape("7500000000000000001"))
    except srv.ApiFaulted as exc:
        assert "parser='ai'" in str(exc), exc
    else:
        raise AssertionError("a faulted request must raise")
    finally:
        srv._request = real


def test_scrape_rejects_a_place_name_as_location():
    try:
        asyncio.run(srv.mcp.call_tool("scrape", {"url": "https://ex.com", "location": "Germany"}))
    except Exception as exc:  # noqa: BLE001 — FastMCP rejects it before the tool body runs
        assert "pattern" in str(exc), exc
        return
    raise AssertionError("a place name must be rejected before calling the API")


def test_an_endpoint_without_a_sync_form_is_queued():
    with _api(_pending, _pending) as api:
        asyncio.run(scrape_target("scrape/chatgpt", {"prompt": "hi"}))
        asyncio.run(scrape_target("scrape/media", {"url": "https://ex.com/a.mp4"}))
    assert [path for path, _ in api.posts] == [
        "/v1/async/scrape/chatgpt",
        "/v1/async/scrape/media",
    ], api.posts


def _collect(body):
    async def fake_request(method, path, payload=None, **kwargs):
        return body

    real = srv._request
    srv._request = fake_request
    try:
        return asyncio.run(check_scrape("7500000000000000001"))
    finally:
        srv._request = real


def test_check_scrape_reports_a_failed_dedicated_parse():
    body = {
        "state": "done",
        "params": {"prompt": "hi", "output": ["json"], "json": {"prompt": None, "schema": None}},
        "results": [
            {"json": None, "metadata": {"statuses": {"json_parse": {"tag": "PARSE_FAILED"}}}}
        ],
    }
    out = _collect(body)
    assert out["parser"] == "dedicated", out
    assert "PARSE_FAILED" in out["parser_note"] and "parser='ai'" in out["parser_note"]


def test_check_scrape_labels_the_parser_that_answered():
    dedicated = {
        "state": "done",
        "params": {"prompt": "hi", "output": ["json"], "json": {"prompt": None, "schema": None}},
        "results": [{"json": {"response": "x"}}],
    }
    out = _collect(dedicated)
    assert out["parser"] == "dedicated" and "failed" not in out["parser_note"], out

    ai = {**dedicated, "params": {**dedicated["params"], "json": {"prompt": "the answer"}}}
    assert _collect(ai)["parser"] == "ai"

    page = {"state": "done", "params": {"output": ["markdown"]}, "results": [{"markdown": "x"}]}
    assert "parser" not in _collect(page)


def test_parser_groups_are_cached_per_key():
    with _api(lambda p: _done(p, json={"a": 1}), lambda p: _done(p, json={"a": 1})) as api:
        asyncio.run(scrape("https://ex.com", json_prompt="a"))
        asyncio.run(scrape("https://ex.com", json_prompt="a"))
    lookups = [c for c in api.calls if c[1].startswith("/v1/scrapers")]
    assert len(lookups) == 1, api.calls


def test_a_failed_group_lookup_does_not_block_extraction():
    async def broken(method, path, payload=None, **kwargs):
        if path.startswith("/v1/scrapers"):
            raise ApiError("/v1/scrapers returned 500: down")
        return _done(payload, json={"a": 1})

    real = srv._request
    srv._request = broken
    srv._JSON_GROUPS.clear()
    try:
        out = asyncio.run(scrape("https://ex.com", json_prompt="a"))
    finally:
        srv._request = real
    assert out["parser"] == "ai"


def test_list_scrapers_groups_by_json_support():
    with _api() as api:
        out = asyncio.run(srv.list_scrapers())
    assert api.calls == [("GET", "/v1/scrapers?group_by=json_output_support", None)]
    assert "json_supported" in out["scrapers"]


def _page(content, output="markdown"):
    return {
        "results": [{output: content, "metadata": {"url": "https://ex.com/a"}}],
        "params": {"output": [output]},
    }


def test_an_empty_shell_is_flagged_for_a_js_retry():
    out = srv._flag_thin_content(_page("# Loading\n\n"))["results"][0]
    assert out["content_thin"]["visible_chars"] < srv.THIN_CONTENT_CHARS, out
    assert "run_js=True" in out["content_thin"]["note"]


def test_an_empty_rendered_page_is_not_sent_back_to_run_js():
    page = _page("# Loading\n\n")
    page["params"]["run_js"] = True
    note = srv._flag_thin_content(page)["results"][0]["content_thin"]["note"]
    assert "Do not retry with run_js" in note and "login" in note, note


def test_a_null_params_echo_does_not_crash_the_content_checks():
    payload = {"results": [{"markdown": "# x"}], "params": None}
    assert "content_thin" in srv._flag_thin_content(payload)["results"][0]
    srv._process_content(payload, "markdown", 1)


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
    page = {"results": [{"markdown": "\u4e2d" * 20000}], "params": {}}
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
    page = {"results": [{"markdown": "word " * 40000}], "params": {}}
    note = srv._process_content(page, "markdown", 1000)["results"][0]["content_truncated"]["note"]
    for hint in ("X-MCP-Max-Tokens", "stdio", "json_prompt"):
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


def test_only_posts_count_against_the_rate_limit():
    def ok(method, url):
        return httpx.Response(200, json={"state": "pending"}, request=httpx.Request(method, url))

    real_client, real_limit, real_times = httpx.AsyncClient, srv.RATE_LIMIT, list(srv._CALL_TIMES)
    httpx.AsyncClient = _stub_client(ok)
    srv.RATE_LIMIT, srv._CALL_TIMES[:] = (1, 3600.0), []
    try:
        for _ in range(3):
            asyncio.run(srv._request("GET", "/v1/async/scrape/1"))
        asyncio.run(srv._request("POST", "/v1/scrape", {}))
        try:
            asyncio.run(srv._request("POST", "/v1/scrape", {}))
        except ApiError as exc:
            assert "rate limit" in str(exc).lower(), exc
        else:
            raise AssertionError("the second POST must be refused")
    finally:
        httpx.AsyncClient = real_client
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
        asyncio.run(srv._request("POST", "/v1/search", {"query": "x"}))
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
    assert srv._detail(resp) == "#/location: location must be an ISO 3166-1 alpha-2 country code"


def test_env_file_fills_unset_oxylabs_vars_and_nothing_else():
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, ".env")
        with open(path, "w") as fh:
            fh.write(
                "# a comment\n"
                "\n"
                'export OXYLABS_WEB_API_KEY="from-file"\n'
                "OXYLABS_SPILL_DIR=/from/file\n"
                "PATH=/definitely/not\n"
            )

        saved_path, saved_dir = os.environ["PATH"], os.environ.pop("OXYLABS_SPILL_DIR", None)
        os.environ["OXYLABS_ENV_FILE"] = path
        try:
            srv._load_env_file()
            # The real environment wins: the key is already set, so the file must not win.
            assert os.environ["OXYLABS_WEB_API_KEY"] == "test-key"
            assert os.environ["OXYLABS_SPILL_DIR"] == "/from/file"
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
            os.environ.pop("OXYLABS_SPILL_DIR", None)
            if saved_dir is not None:
                os.environ["OXYLABS_SPILL_DIR"] = saved_dir


def test_the_sync_endpoints_timeout_problem_is_queued_without_a_resend():
    calls = []
    problem = {
        "status": 500,
        "title": "REQUEST_FAILED_SCRAPE_TIMEOUT",
        "detail": "Scraping took too long, try again.",
        "instance": "trace-1",
    }

    def answer(method, url):
        calls.append(url)
        if url.endswith("/v1/scrape"):
            return httpx.Response(500, json=problem, request=httpx.Request(method, url))
        queued = {"state": "pending", "metadata": {"request_id": "7500000000000000009"}}
        return httpx.Response(200, json=queued, request=httpx.Request(method, url))

    real_client, real_delay = httpx.AsyncClient, srv.RETRY_BASE_DELAY
    httpx.AsyncClient, srv.RETRY_BASE_DELAY = _stub_client(answer), 0
    try:
        out = asyncio.run(scrape("https://ex.com", run_js=True))
    finally:
        httpx.AsyncClient, srv.RETRY_BASE_DELAY = real_client, real_delay
    assert [u.rsplit("/v1/", 1)[1] for u in calls] == ["scrape", "async/scrape"], calls
    assert out["request_id"] == "7500000000000000009", out
    assert "Scraping took too long" in out["note"], out["note"]


def test_a_faulted_sync_scrape_is_reported_not_resent():
    calls = []
    problem = {
        "status": 500,
        "title": "REQUEST_FAILED_AFTER_TOO_MANY_RETRIES",
        "detail": "The request failed after the maximum number of retries.",
        "instance": "trace-2",
        "state": "faulted",
        "params": {"url": "https://ex.com"},
        "metadata": {"timestamp": 1, "request_id": "r-1"},
    }

    def faulted(method, url):
        calls.append(url)
        return httpx.Response(500, json=problem, request=httpx.Request(method, url))

    real_client, real_delay = httpx.AsyncClient, srv.RETRY_BASE_DELAY
    httpx.AsyncClient, srv.RETRY_BASE_DELAY = _stub_client(faulted), 0
    try:
        asyncio.run(scrape("https://ex.com"))
    except srv.ApiFaulted as exc:
        assert exc.tag == "REQUEST_FAILED_AFTER_TOO_MANY_RETRIES", exc.tag
        assert "maximum number of retries" in str(exc) and "r-1" in str(exc), exc
        assert len(calls) == 1, "a faulted scrape is the agent's retry to make, not a resend"
    else:
        raise AssertionError("a faulted sync request is a failure")
    finally:
        httpx.AsyncClient, srv.RETRY_BASE_DELAY = real_client, real_delay


def test_a_plain_500_is_still_retried():
    calls = []

    def flaky(method, url):
        calls.append(url)
        if len(calls) == 1:
            return httpx.Response(
                500, json={"title": "INTERNAL_SERVER_ERROR"}, request=httpx.Request(method, url)
            )
        return httpx.Response(200, json={"state": "done"}, request=httpx.Request(method, url))

    real_client, real_delay = httpx.AsyncClient, srv.RETRY_BASE_DELAY
    httpx.AsyncClient, srv.RETRY_BASE_DELAY = _stub_client(flaky), 0
    try:
        asyncio.run(srv._request("POST", "/v1/scrape", {}, retry_faulted=False))
    finally:
        httpx.AsyncClient, srv.RETRY_BASE_DELAY = real_client, real_delay
    assert len(calls) == 2, calls


def test_a_query_error_without_a_pointer_reads_cleanly():
    resp = httpx.Response(
        400,
        json={"title": "INVALID_QUERY", "errors": [{"detail": "unknown group_by"}]},
        request=httpx.Request("GET", "https://x/v1/scrapers"),
    )
    assert srv._detail(resp) == "unknown group_by"


def test_a_faulted_async_request_is_read_once_and_names_its_title():
    calls = []
    problem = {
        "status": 500,
        "title": "REQUEST_FAILED_AFTER_TOO_MANY_RETRIES",
        "detail": "The request failed after the maximum number of retries.",
        "state": "faulted",
        "metadata": {"request_id": "77"},
    }

    def faulted(method, url):
        calls.append(url)
        return httpx.Response(500, json=problem, request=httpx.Request(method, url))

    real_client, real_delay = httpx.AsyncClient, srv.RETRY_BASE_DELAY
    httpx.AsyncClient, srv.RETRY_BASE_DELAY = _stub_client(faulted), 0
    try:
        asyncio.run(check_scrape("77"))
    except srv.ApiFaulted as exc:
        assert exc.tag == "REQUEST_FAILED_AFTER_TOO_MANY_RETRIES", exc.tag
        assert len(calls) == 1, "a faulted request stays faulted; polling it again is waste"
    else:
        raise AssertionError("a faulted async request is a failure")
    finally:
        httpx.AsyncClient, srv.RETRY_BASE_DELAY = real_client, real_delay


def _parsed(tag, json_body):
    def answer(payload):
        out = _done(payload, json=json_body)
        out["results"][0]["metadata"] = {"statuses": {"json_parse": {"code": 0, "tag": tag}}}
        return out

    return answer


def test_a_failed_dedicated_parse_falls_back_to_the_ai_parser():
    with _api(_parsed("PARSE_FAILED", {"x": None}), lambda p: _done(p, json={"t": 1})) as api:
        out = asyncio.run(
            scrape_target("scrape/amazon/product", {"query": "B0"}, json_prompt="the title")
        )
    assert len(api.posts) == 2 and api.posts[1][1]["json"] == {"prompt": "the title"}
    assert out["parser"] == "ai"


def test_a_faulted_dedicated_parse_falls_back_but_a_failed_scrape_does_not():
    parse_fault = srv.ApiFaulted("faulted (PARSE_FAILED)", tag="PARSE_FAILED")
    with _api(parse_fault, lambda p: _done(p, json={"t": 1})) as api:
        out = asyncio.run(
            scrape_target("scrape/amazon/product", {"query": "B0"}, json_prompt="the title")
        )
    assert out["parser"] == "ai" and len(api.posts) == 2

    scrape_fault = srv.ApiFaulted("faulted", tag="REQUEST_FAILED_AFTER_TOO_MANY_RETRIES")
    with _api(scrape_fault) as api:
        try:
            asyncio.run(
                scrape_target("scrape/amazon/product", {"query": "B0"}, json_prompt="the title")
            )
        except srv.ApiFaulted:
            pass
        else:
            raise AssertionError("a failed scrape would fail the AI parser too")
    assert len(api.posts) == 1


def test_a_partial_dedicated_parse_is_returned_with_its_verdict():
    tag = "PARSE_PARTIAL_SUCCESS_SOME_FIELDS_DEFAULT"
    with _api(_parsed(tag, {"title": "t"})) as api:
        out = asyncio.run(
            scrape_target("scrape/amazon/product", {"query": "B0"}, json_prompt="the title")
        )
    assert len(api.posts) == 1 and out["parser"] == "dedicated"
    assert tag in out["parser_note"], out["parser_note"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all checks passed")


def test_screenshot_comes_back_as_an_image_not_base64_text():
    """`format="screenshot"` forces run_js (the API demands it) and the PNG is an image block."""
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    shot = base64.b64encode(png).decode()

    with _api(lambda p: _done(p, screenshot=shot)) as api:
        out = asyncio.run(scrape("https://ex.com", format="screenshot"))
    assert api.posts[0][0] == "/v1/scrape" and api.posts[0][1]["run_js"] is True
    assert isinstance(out, list) and len(out) == 2, out
    envelope, image = out
    assert isinstance(image, srv.Image) and image.data == png
    assert envelope["results"][0]["screenshot"]["bytes"] == len(png)

    # A screenshot that outlives the sync call is collected through check_scrape instead.
    timeout = srv.ApiTimeout("timed out")
    with _api(timeout, _pending):
        started = asyncio.run(scrape("https://ex.com", format="screenshot"))
    assert started["state"] == "pending"

    async def collected(method, path, payload=None, **kwargs):
        return {
            "state": "done",
            "results": [{"screenshot": shot}],
            "params": {"output": ["screenshot"]},
        }

    real = srv._request
    srv._request = collected
    try:
        out = asyncio.run(check_scrape(started["request_id"]))
    finally:
        srv._request = real
    envelope, image = out
    assert image.data == png and envelope["result"]["results"][0]["screenshot"]["bytes"] == len(png)


def test_a_target_scrape_without_output_budgets_the_format_the_api_picked():
    srv._SPILL_ENABLED = False
    size = srv.MAX_INLINE_TOKENS * 4 + 5000

    def html_default(payload):
        return _done({**payload, "output": ["html"]}, html="<p>" + "x" * size + "</p>")

    with _api(html_default):
        out = asyncio.run(scrape_target("scrape/amazon/product", {"query": "B0"}))
    result = out["results"][0]
    assert "content_truncated" in result, result.keys()
    assert len(result["html"]) < size


def test_a_permanent_fault_does_not_invite_a_retry():
    for tag in ("REQUEST_FAILED_BROWSER_INSTRUCTIONS", "DOWNLOAD_FAILED_VIDEO_PRIVATE"):
        exc = srv._faulted({"title": tag, "detail": "d", "state": "faulted"}, "/v1/scrape", 500)
        assert "will not change" in str(exc) and "costs nothing" not in str(exc), exc
    exc = srv._faulted(
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
        srv._check(resp, "/v1/scrape")
    except ApiError as exc:
        assert "Invalid authorization header." in str(exc), exc
    else:
        raise AssertionError("expected ApiError for a 401")
    bare = httpx.Response(401, text="", request=httpx.Request("POST", "https://x/v1/scrape"))
    try:
        srv._check(bare, "/v1/scrape")
    except ApiError as exc:
        assert str(exc) == "Authentication failed (401). Check the API key.", exc
    else:
        raise AssertionError("expected ApiError for a 401")

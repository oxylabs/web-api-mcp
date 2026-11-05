"""Sync-then-queue scraping and the dedicated-then-AI parser choice."""

from __future__ import annotations

import asyncio

import httpx
from helpers import _api, _done, _FakeCtx, _pending, _stub_client

from oxylabs_web_api_mcp import client, config, scraping
from oxylabs_web_api_mcp.errors import ApiError, ApiFaulted, ApiTimeout
from oxylabs_web_api_mcp.tools import run_scraper, scrape


def test_run_js_goes_sync_first_and_returns_the_content():
    with _api(lambda p: _done(p, markdown="word " * 400)) as api:
        out = asyncio.run(scrape("https://ex.com", run_js=True))
    assert api.posts == [("/v1/scrape", api.posts[0][1])], api.posts
    assert api.posts[0][1]["run_js"] is True
    assert out["results"][0]["markdown"].startswith("word"), out


def test_a_sync_timeout_is_sent_to_the_async_endpoint():
    timeout = ApiTimeout("/v1/scrape returned 500: Scraping took too long, try again.")
    with _api(timeout, _pending) as api:
        out = asyncio.run(scrape("https://ex.com", run_js=True))
    assert [path for path, _ in api.posts] == ["/v1/scrape", "/v1/async/scrape"], api.posts
    assert api.posts[0][1] == api.posts[1][1], "the queued request must be the same payload"
    assert out["state"] == "pending" and out["request_id"] == "7500000000000000001"
    assert "check_requests(['7500000000000000001'])" in out["note"]
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

    real_client, real_delay = httpx.AsyncClient, config.RETRY_BASE_DELAY
    httpx.AsyncClient, config.RETRY_BASE_DELAY = _stub_client(gateway_timeout), 0
    try:
        asyncio.run(scrape("https://ex.com"))
    finally:
        httpx.AsyncClient, config.RETRY_BASE_DELAY = real_client, real_delay
    assert [u.rsplit("/v1/", 1)[1] for u in calls] == ["scrape", "scrape"], calls


def test_run_scraper_runs_sync_even_with_run_js_and_queues_async_endpoints():
    with _api(lambda p: _done(p, html="<p>x</p>")) as api:
        asyncio.run(run_scraper("scrape/amazon/search", {"query": "x", "run_js": True}))
    assert api.posts[0][0] == "/v1/scrape/amazon/search", api.posts

    with _api(_pending) as api:
        out = asyncio.run(run_scraper("async/scrape/chatgpt", {"prompt": "hi"}))
    assert api.posts[0][0] == "/v1/async/scrape/chatgpt"
    assert "check_requests(['7500000000000000001'])" in out["note"]


def test_run_scraper_defaults_to_markdown_where_the_endpoint_offers_it():
    only_json = {"properties": {"output": {"type": "array", "items": {"enum": ["json"]}}}}
    trends = "/v1/scrape/google/trends/explore"
    timeout = ApiTimeout("/v1/scrape/amazon/search returned 500: took too long")
    answers = (
        lambda p: _done(p, markdown="word " * 200),
        lambda p: _done(p, markdown="word " * 200),
        lambda p: _done(p, json={"x": 1}),
        lambda p: _done(p, html="<p>x</p>"),
        timeout,
        _pending,
    )
    with _api(*answers, schemas={trends: only_json}) as api:
        for _ in range(2):
            asyncio.run(run_scraper("scrape/amazon/search", {"query": "x"}))
        asyncio.run(run_scraper("scrape/google/trends/explore", {"query": "x"}))
        asyncio.run(run_scraper("scrape/amazon/search", {"query": "x", "output": ["html"]}))
        asyncio.run(run_scraper("scrape/amazon/search", {"query": "y"}))
    outputs = [payload.get("output") for _, payload in api.posts]
    # HTML is the API's default; Markdown is what an agent can read, as with `scrape`.
    assert outputs[:2] == [["markdown"], ["markdown"]]
    # Trends only answers in JSON, so it keeps its own default instead of being refused.
    assert outputs[2] is None
    # An `output` the caller set is sent as is.
    assert outputs[3] == ["html"]
    # A timed-out call is queued with the same payload, Markdown included.
    assert api.posts[4:] == [
        ("/v1/scrape/amazon/search", {"query": "y", "output": ["markdown"]}),
        ("/v1/async/scrape/amazon/search", {"query": "y", "output": ["markdown"]}),
    ]
    # Each endpoint's schema is read once, not once per call.
    assert [path for method, path, _ in api.calls if method == "OPTIONS"] == [
        "/v1/scrape/amazon/search?format=json_schema",
        f"{trends}?format=json_schema",
    ]


def test_extract_on_a_url_uses_the_ai_parser_without_asking():
    with _api(lambda p: _done(p, json={"title": "t"})) as api:
        out = asyncio.run(scrape("https://ex.com", json_prompt="the title", ctx=_FakeCtx()))
    assert api.calls[0][1] == "/v1/scrapers?group_by=json_output_support", api.calls
    ((path, payload),) = api.posts
    assert path == "/v1/scrape"
    assert payload[config.OUTPUT_PARAM] == ["json"]
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
            run_scraper("scrape/amazon/product", {"query": "B0"}, json_prompt="title and price")
        )
    ((path, payload),) = api.posts
    assert path == "/v1/scrape/amazon/product"
    assert payload[config.OUTPUT_PARAM] == ["json"] and "json" not in payload, payload
    assert out["parser"] == "dedicated" and "parser='ai'" in out["parser_note"]


def test_an_empty_dedicated_result_falls_back_to_the_ai_parser():
    with _api(lambda p: _done(p, json=None), lambda p: _done(p, json={"title": "t"})) as api:
        out = asyncio.run(
            run_scraper("scrape/amazon/product", {"query": "B0"}, json_prompt="the title")
        )
    assert [payload.get("json") for _, payload in api.posts] == [None, {"prompt": "the title"}]
    assert out["parser"] == "ai"


def test_parser_ai_skips_the_dedicated_parser():
    with _api(lambda p: _done(p, json={"seller": "s"})) as api:
        asyncio.run(
            run_scraper(
                "scrape/amazon/product", {"query": "B0"}, json_prompt="the seller", parser="ai"
            )
        )
    assert len(api.posts) == 1 and api.posts[0][1]["json"] == {"prompt": "the seller"}


def test_structured_without_a_prompt_uses_the_dedicated_parser_only():
    with _api(lambda p: _done(p, json=None)) as api:
        out = asyncio.run(run_scraper("scrape/amazon/product", {"query": "B0"}, structured=True))
    assert len(api.posts) == 1 and out["parser"] == "dedicated"


def test_structured_needs_a_prompt_where_there_is_no_dedicated_parser():
    with _api() as api:
        try:
            asyncio.run(run_scraper("scrape", {"url": "https://ex.com"}, structured=True))
        except ApiError as exc:
            assert "no dedicated parser" in str(exc), exc
        else:
            raise AssertionError("expected ApiError without json_prompt on a prompt-only endpoint")
    assert not api.posts


def test_endpoints_without_json_refuse_structured_output():
    with _api() as api:
        try:
            asyncio.run(run_scraper("async/scrape/media", {"url": "x"}, json_prompt="anything"))
        except ApiError as exc:
            assert "cannot return structured JSON" in str(exc), exc
        else:
            raise AssertionError("json_not_supported must be refused")
    assert not api.posts


def test_a_queued_dedicated_parse_says_how_to_fall_back():
    with _api(_pending) as api:
        out = asyncio.run(
            run_scraper("async/scrape/chatgpt", {"prompt": "hi"}, json_prompt="the citations")
        )
    assert api.posts[0][0] == "/v1/async/scrape/chatgpt" and "json" not in api.posts[0][1]
    assert out["parser"] == "dedicated" and "request_id" in out
    assert "check_requests" in out["parser_note"], out["parser_note"]


def test_a_queued_ai_parse_is_not_called_extracted():
    timeout = ApiTimeout("/v1/scrape returned 500: REQUEST_FAILED_SCRAPE_TIMEOUT")
    with _api(timeout, _pending):
        out = asyncio.run(scrape("https://ex.com", json_prompt="the title"))
    assert out["parser"] == "ai" and out["state"] == "pending", out
    assert "Queued" in out["parser_note"] and "Extracted" not in out["parser_note"]


def test_parser_ai_without_a_prompt_says_what_it_needs():
    with _api() as api:
        try:
            asyncio.run(
                run_scraper("scrape/amazon/product", {"query": "B0"}, structured=True, parser="ai")
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
    real = client._request
    client._request = broken
    scraping._JSON_GROUPS.clear()
    try:
        out = asyncio.run(run_scraper("scrape/amazon/product", {"query": "B0"}, structured=True))
    finally:
        client._request = real
    assert posts == [{"query": "B0", "output": ["json"]}], posts
    assert out["parser"] == "dedicated"


def test_an_endpoint_without_a_sync_form_is_queued():
    with _api(_pending, _pending) as api:
        asyncio.run(run_scraper("scrape/chatgpt", {"prompt": "hi"}))
        asyncio.run(run_scraper("scrape/media", {"url": "https://ex.com/a.mp4"}))
    assert [path for path, _ in api.posts] == [
        "/v1/async/scrape/chatgpt",
        "/v1/async/scrape/media",
    ], api.posts


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

    real = client._request
    client._request = broken
    scraping._JSON_GROUPS.clear()
    try:
        out = asyncio.run(scrape("https://ex.com", json_prompt="a"))
    finally:
        client._request = real
    assert out["parser"] == "ai"


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

    real_client, real_delay = httpx.AsyncClient, config.RETRY_BASE_DELAY
    httpx.AsyncClient, config.RETRY_BASE_DELAY = _stub_client(answer), 0
    try:
        out = asyncio.run(scrape("https://ex.com", run_js=True))
    finally:
        httpx.AsyncClient, config.RETRY_BASE_DELAY = real_client, real_delay
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

    real_client, real_delay = httpx.AsyncClient, config.RETRY_BASE_DELAY
    httpx.AsyncClient, config.RETRY_BASE_DELAY = _stub_client(faulted), 0
    try:
        asyncio.run(scrape("https://ex.com"))
    except ApiFaulted as exc:
        assert exc.tag == "REQUEST_FAILED_AFTER_TOO_MANY_RETRIES", exc.tag
        assert "maximum number of retries" in str(exc) and "r-1" in str(exc), exc
        assert len(calls) == 1, "a faulted scrape is the agent's retry to make, not a resend"
    else:
        raise AssertionError("a faulted sync request is a failure")
    finally:
        httpx.AsyncClient, config.RETRY_BASE_DELAY = real_client, real_delay


def _parsed(tag, json_body):
    def answer(payload):
        out = _done(payload, json=json_body)
        out["results"][0]["metadata"] = {"statuses": {"json_parse": {"code": 0, "tag": tag}}}
        return out

    return answer


def test_a_failed_dedicated_parse_falls_back_to_the_ai_parser():
    with _api(_parsed("PARSE_FAILED", {"x": None}), lambda p: _done(p, json={"t": 1})) as api:
        out = asyncio.run(
            run_scraper("scrape/amazon/product", {"query": "B0"}, json_prompt="the title")
        )
    assert len(api.posts) == 2 and api.posts[1][1]["json"] == {"prompt": "the title"}
    assert out["parser"] == "ai"


def test_a_faulted_dedicated_parse_falls_back_but_a_failed_scrape_does_not():
    parse_fault = ApiFaulted("faulted (PARSE_FAILED)", tag="PARSE_FAILED")
    with _api(parse_fault, lambda p: _done(p, json={"t": 1})) as api:
        out = asyncio.run(
            run_scraper("scrape/amazon/product", {"query": "B0"}, json_prompt="the title")
        )
    assert out["parser"] == "ai" and len(api.posts) == 2

    scrape_fault = ApiFaulted("faulted", tag="REQUEST_FAILED_AFTER_TOO_MANY_RETRIES")
    with _api(scrape_fault) as api:
        try:
            asyncio.run(
                run_scraper("scrape/amazon/product", {"query": "B0"}, json_prompt="the title")
            )
        except ApiFaulted:
            pass
        else:
            raise AssertionError("a failed scrape would fail the AI parser too")
    assert len(api.posts) == 1


def test_a_partial_dedicated_parse_is_returned_with_its_verdict():
    tag = "PARSE_PARTIAL_SUCCESS_SOME_FIELDS_DEFAULT"
    with _api(_parsed(tag, {"title": "t"})) as api:
        out = asyncio.run(
            run_scraper("scrape/amazon/product", {"query": "B0"}, json_prompt="the title")
        )
    assert len(api.posts) == 1 and out["parser"] == "dedicated"
    assert tag in out["parser_note"], out["parser_note"]


def test_a_target_scrape_without_output_budgets_the_format_the_api_picked():
    config.SPILL_ENABLED = False
    size = config.MAX_INLINE_TOKENS * 4 + 5000

    def html_default(payload):
        return _done({**payload, "output": ["html"]}, html="<p>" + "x" * size + "</p>")

    with _api(html_default):
        out = asyncio.run(run_scraper("scrape/amazon/product", {"query": "B0"}))
    result = out["results"][0]
    assert "content_truncated" in result, result.keys()
    assert len(result["html"]) < size


def test_run_scraper_sends_batch_endpoints_to_the_endpoint_itself():
    # A list on the endpoint itself is batched within the size cap, each item charged against
    # the rate limit; a batch endpoint called directly would be neither.
    for endpoint, own in (
        ("scrape/batch", "'scrape'"),
        ("async/scrape/batch", "'scrape'"),
        ("/v1/async/scrape/batch/amazon/product", "'scrape/amazon/product'"),
    ):
        with _api() as api:
            try:
                asyncio.run(run_scraper(endpoint, {"url": ["https://a.com", "https://b.com"]}))
            except ApiError as exc:
                assert f"run_scraper on {own}" in str(exc) and "as a list" in str(exc), exc
            else:
                raise AssertionError(f"{endpoint!r} must be refused")
        assert api.calls == [], api.calls


def test_a_queued_post_is_not_resent_when_the_api_may_have_taken_it(monkeypatch):
    """The API queues a job before it writes the reply: a failed reply may follow a job."""

    def api(*statuses):
        calls = []

        def respond(method, url):
            status = statuses[min(len(calls), len(statuses) - 1)]
            calls.append(status)
            body = {"state": "pending", "metadata": {"request_id": "7500000000000000009"}}
            return httpx.Response(status, json=body, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx, "AsyncClient", _stub_client(respond))
        return calls

    def queue():
        return asyncio.run(scraping._queue("async/scrape", {"url": "https://ex.com"}, None))

    monkeypatch.setattr(config, "RETRY_BASE_DELAY", 0)
    for status in (500, 502, 504):
        calls = api(status, 202)
        try:
            queue()
        except ApiError as exc:
            assert "may have been queued" in str(exc), exc
        else:
            raise AssertionError(f"a {status} must not be resent")
        assert calls == [status], calls
    # A 429 or a 503 comes before anything is queued, so it is resent.
    for status in (429, 503):
        calls = api(status, 202)
        assert queue()["request_id"] == "7500000000000000009"
        assert calls == [status, 202], calls


def test_run_scraper_returns_the_dedicated_parse_by_default():
    with _api(
        _parsed("PARSE_SUCCESS", {"title": "x"}), lambda p: _done(p, markdown="x" * 600)
    ) as api:
        out = asyncio.run(run_scraper("scrape/amazon/product", {"query": "B0X"}))
        # An `output` set by the caller still gets the page.
        asyncio.run(run_scraper("scrape/amazon/product", {"query": "B0X", "output": ["markdown"]}))
    first, second = (payload for _, payload in api.posts)
    assert first == {"query": "B0X", "output": ["json"]} and out["parser"] == "dedicated"
    assert second == {"query": "B0X", "output": ["markdown"]}

    # Without a dedicated parser it stays Markdown.
    with _api(lambda p: _done(p, markdown="x" * 600)) as api:
        asyncio.run(run_scraper("scrape/amazon/search", {"query": "shoes"}))
    assert api.posts[0][1]["output"] == ["markdown"]


def test_a_json_prompt_and_a_json_schema_are_not_sent_together():
    # The API's AI parser takes one or the other, and refuses a request carrying both.
    prompt, schema = "the title", {"type": "object", "properties": {"title": {"type": "string"}}}
    for call in (
        lambda: scrape("https://ex.com", json_prompt=prompt, json_schema=schema),
        lambda: scrape(["https://a.com", "https://b.com"], json_prompt=prompt, json_schema=schema),
        lambda: run_scraper(
            "scrape", {"url": "https://ex.com", "json": {"prompt": prompt, "schema": schema}}
        ),
        lambda: run_scraper(
            "scrape", {"url": "https://ex.com", "json": {"schema": schema}}, json_prompt=prompt
        ),
    ):
        with _api() as api:
            try:
                asyncio.run(call())
            except ApiError as exc:
                assert "cannot be used together" in str(exc), exc
            else:
                raise AssertionError("a prompt and a schema together must be refused")
        assert api.posts == [], api.posts

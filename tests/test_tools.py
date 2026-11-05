"""Tool input validation, lists, collecting queued requests, reading on, listing, the skill."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
from fastmcp.utilities.types import Image
from helpers import GROUPS, _api, _done, _pending, _stub_client

from oxylabs_web_api_mcp import app, client, config, results, scraping, skill, tools
from oxylabs_web_api_mcp.content import _estimate_tokens
from oxylabs_web_api_mcp.errors import ApiError, ApiFaulted, ApiNotFound, ApiTimeout
from oxylabs_web_api_mcp.tools import (
    check_requests,
    list_scrapers,
    read_result,
    run_scraper,
    scrape,
)


def _within_budget(reply) -> bool:
    return _estimate_tokens(json.dumps(reply, ensure_ascii=False)) <= config.MAX_INLINE_TOKENS


def _clock(monkeypatch, wait: int = 30) -> list[float]:
    """Let calls wait for queued requests, on a clock that moves only when they sleep."""
    now = [0.0]

    async def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(config, "MAX_WAIT_FOR_QUEUED_SECONDS", wait)
    monkeypatch.setattr(results.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(results.asyncio, "sleep", sleep)
    return now


def test_search_rejects_bad_input_before_calling_the_api():
    for kwargs in (
        {"query": "   "},
        {"query": "ok", "max_results": 0},
        {"query": "ok", "max_results": 21},
    ):
        try:
            asyncio.run(app.mcp.call_tool("search", kwargs))
        except Exception:  # noqa: BLE001 — ApiError or FastMCP's own validation error
            continue
        raise AssertionError(f"expected an error for {kwargs}")


def test_scrape_rejects_relative_urls():
    try:
        asyncio.run(scrape("www.example.com"))
    except ApiError:
        return
    raise AssertionError("expected ApiError for a non-absolute URL")


def test_scrape_rejects_unknown_format():
    try:
        asyncio.run(app.mcp.call_tool("scrape", {"url": "https://ex.com", "format": "pdf"}))
    except Exception:  # noqa: BLE001 — FastMCP rejects it before the tool body runs
        return
    raise AssertionError("expected ApiError for an unsupported format")


def test_endpoint_names_cannot_walk_the_path():
    assert tools._endpoint_name("scrape") == "scrape"
    # /v1/scrapers returns slash-separated source paths; those must pass whole.
    assert tools._endpoint_name("scrape/amazon/search") == "scrape/amazon/search"
    assert tools._endpoint_name("/v1/scrape/youtube/search/max/") == "scrape/youtube/search/max"
    for bad in ("../admin", "scrape?x=1", "v1/../../etc", "scrape/../etc", "HTTP://evil", ""):
        try:
            tools._endpoint_name(bad)
        except ApiError:
            continue
        raise AssertionError(f"_endpoint_name must refuse {bad!r}")


def test_scrape_sends_the_location_in_uppercase():
    with _api(lambda p: _done(p, markdown="x" * 600)) as api:
        asyncio.run(scrape("https://ex.com", location=" de "))
    assert api.posts[0][1]["location"] == "DE", api.posts


def test_scrape_rejects_a_place_name_as_location():
    try:
        asyncio.run(app.mcp.call_tool("scrape", {"url": "https://ex.com", "location": "Germany"}))
    except Exception as exc:  # noqa: BLE001 — FastMCP rejects it before the tool body runs
        assert "pattern" in str(exc), exc
        return
    raise AssertionError("a place name must be rejected before calling the API")


def test_list_scrapers_groups_by_json_support():
    with _api() as api:
        out = asyncio.run(list_scrapers())
    assert api.calls == [("GET", "/v1/scrapers?group_by=json_output_support", None)]
    assert "json_supported" in out["scrapers"]


def test_list_scrapers_describes_batch_endpoints_at_their_async_path():
    with _api() as api:
        asyncio.run(list_scrapers("scrape/batch/amazon/product"))
        asyncio.run(list_scrapers("scrape/amazon/product"))
    assert [path for method, path, _ in api.calls if method == "OPTIONS"] == [
        "/v1/async/scrape/batch/amazon/product?format=json_schema",
        "/v1/scrape/amazon/product?format=json_schema",
    ]


def test_the_skill_ships_inside_the_package():
    text = skill._skill_text()
    assert not text.startswith("---"), "frontmatter is for a skill loader, not a resource"
    assert "search" in text and "run_js" in text, text[:200]


# Collecting queued requests.


def test_check_requests_collects_a_single_request():
    done = {
        "state": "done",
        "results": [{"markdown": "answer"}],
        "params": {"url": "https://ex.com", "output": ["markdown"]},
        "metadata": {"request_id": "7504857924934611969"},
    }
    gone = ApiNotFound("/v1/async/scrape/404404 returned 404: Query not found.")
    with _api(collected={"7504857924934611969": done, "404404": gone}) as api:
        pending = asyncio.run(check_requests(["111"]))
        assert pending["items"] == [{"request_id": "111", "state": "pending"}], pending
        assert "check_requests" in pending["note"]

        out = asyncio.run(check_requests(["7504857924934611969"]))
        (item,) = out["items"]
        assert item["state"] == "done" and item["input"] == "https://ex.com"
        assert item["result"]["results"][0]["markdown"] == "answer"
        # The id is on the item; the echoed params and metadata are dropped.
        assert item["request_id"] == "7504857924934611969"
        assert "params" not in item["result"] and "request_id" not in item["result"]

        (failed,) = asyncio.run(check_requests(["404404"]))["items"]
        assert failed["state"] == "failed" and "No request '404404'" in failed["error"]

        try:
            asyncio.run(check_requests(["nothex"]))
        except ApiError as exc:
            assert "not a request id" in str(exc), exc
        else:
            raise AssertionError("a non-numeric id must be refused")
    # The non-numeric id never reached the API.
    assert [path for _, path, _ in api.calls] == [
        "/v1/async/scrape/111",
        "/v1/async/scrape/7504857924934611969",
        "/v1/async/scrape/404404",
    ]


def test_check_requests_rejects_bad_ids_before_calling_the_api():
    with _api() as api:
        try:
            asyncio.run(check_requests(["123", "abc"]))
        except ApiError as exc:
            assert "not a request id" in str(exc), exc
        else:
            raise AssertionError("a non-numeric id must be refused")
    assert api.calls == []


def test_check_requests_waits_for_requests_to_finish(monkeypatch):
    now = _clock(monkeypatch)
    reads = []

    def answer():
        reads.append(now[0])
        if now[0] < 10:
            return {"state": "pending", "params": {"url": "https://ex.com"}}
        return {
            "state": "done",
            "params": {"url": "https://ex.com", "output": ["markdown"]},
            "results": [{"markdown": "x" * 600}],
        }

    with _api(collected={"7": answer}):
        out = asyncio.run(check_requests(["7"]))
    # One call polls every few seconds until it is done, instead of the agent polling.
    assert out["done"] == 1 and reads == [0, config.POLL_SECONDS, 2 * config.POLL_SECONDS]

    # Past the wait it answers with what is still running.
    now[0], reads[:] = 0.0, []
    with _api(collected={"7": lambda: reads.append(now[0]) or {"state": "pending"}}):
        out = asyncio.run(check_requests(["7"]))
    assert out["pending"] == 1 and max(reads) <= config.MAX_WAIT_FOR_QUEUED_SECONDS
    assert "check_requests" in out["note"]


def test_check_requests_reports_each_request():
    collected = {
        "1": {"state": "pending"},
        "2": {
            "state": "done",
            "params": {"output": ["markdown"]},
            "results": [{"markdown": "real text " * 100}],
            "metadata": {"request_id": "2"},
        },
        "3": ApiNotFound("/v1/async/scrape/3 returned 404: Not found."),
    }
    with _api(collected=collected) as api:
        out = asyncio.run(check_requests(["1", "2", "3", "2"]))

    # Each distinct id is read once.
    assert sorted(path for _, path, _ in api.calls) == [f"/v1/async/scrape/{n}" for n in (1, 2, 3)]
    assert (out["done"], out["pending"], out["failed"]) == (1, 1, 1)
    pending, done, failed = out["items"]
    assert pending == {"request_id": "1", "state": "pending"}
    assert done["state"] == "done" and done["result"]["results"][0]["markdown"].startswith("real")
    assert failed["state"] == "failed" and "No request '3'" in failed["error"]
    assert "check_requests" in out["note"]


def test_check_requests_fails_the_call_when_no_request_could_be_read(monkeypatch):
    async def bad_key(method, path, payload=None, **kwargs):
        raise ApiError("Authentication failed (401). Check the API key.")

    monkeypatch.setattr(client, "_request", bad_key)
    try:
        asyncio.run(check_requests(["1", "2", "3"]))
    except ApiError as exc:
        assert "Authentication failed" in str(exc), exc
    else:
        raise AssertionError("a bad key is not three failed requests")


def test_check_requests_keeps_polling_a_request_it_could_not_read():
    collected = {
        "1": ApiError("Could not reach the Oxylabs Web API: connection reset"),
        "2": {
            "state": "done",
            "params": {"output": ["markdown"]},
            "results": [{"markdown": "real text " * 100}],
        },
        "3": ApiFaulted("faulted (REQUEST_FAILED)", tag="REQUEST_FAILED"),
        "4": ApiTimeout("returned 408: scraping took too long"),
    }
    with _api(collected=collected):
        out = asyncio.run(check_requests(["1", "2", "3", "4"]))
    assert (out["done"], out["pending"], out["failed"]) == (1, 1, 2)
    unread, done, faulted, timed_out = out["items"]
    # The request may well be done: only reading it failed, so it stays on the poll list.
    assert unread["state"] == "pending" and "connection reset" in unread["error"]
    assert done["state"] == "done"
    assert faulted["state"] == timed_out["state"] == "failed"


def test_a_faulted_queued_dedicated_parse_suggests_the_ai_parser():
    params = {"output": ["json"], "json": {"prompt": None, "schema": None}}
    fault = ApiFaulted("faulted (PARSE_FAILED)", tag="PARSE_FAILED", params=params)
    with _api(collected={"7500000000000000001": fault}):
        (item,) = asyncio.run(check_requests(["7500000000000000001"]))["items"]
    assert item["state"] == "failed" and "parser='ai'" in item["error"], item


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

    real_client, real_delay = httpx.AsyncClient, config.RETRY_BASE_DELAY
    httpx.AsyncClient, config.RETRY_BASE_DELAY = _stub_client(faulted), 0
    try:
        (item,) = asyncio.run(check_requests(["77"]))["items"]
    finally:
        httpx.AsyncClient, config.RETRY_BASE_DELAY = real_client, real_delay
    assert item["state"] == "failed" and "REQUEST_FAILED_AFTER_TOO_MANY_RETRIES" in item["error"]
    assert len(calls) == 1, "a faulted request stays faulted; polling it again is waste"


def _collect(body):
    with _api(collected={"7500000000000000001": body}):
        out = asyncio.run(check_requests(["7500000000000000001"]))
    return out, out["items"][0]


def test_check_requests_reports_a_failed_dedicated_parse():
    body = {
        "state": "done",
        "params": {"prompt": "hi", "output": ["json"], "json": {"prompt": None, "schema": None}},
        "results": [
            {"json": None, "metadata": {"statuses": {"json_parse": {"tag": "PARSE_FAILED"}}}}
        ],
    }
    out, item = _collect(body)
    assert item["parser"] == "dedicated" and item["parser_note"] == "parser_notes[0]", item
    assert "PARSE_FAILED" in out["parser_notes"][0] and "parser='ai'" in out["parser_notes"][0]


def test_check_requests_labels_the_parser_that_answered():
    dedicated = {
        "state": "done",
        "params": {"prompt": "hi", "output": ["json"], "json": {"prompt": None, "schema": None}},
        "results": [{"json": {"response": "x"}}],
    }
    out, item = _collect(dedicated)
    assert item["parser"] == "dedicated" and "failed" not in out["parser_notes"][0], out

    ai = {**dedicated, "params": {**dedicated["params"], "json": {"prompt": "the answer"}}}
    assert _collect(ai)[1]["parser"] == "ai"

    page = {"state": "done", "params": {"output": ["markdown"]}, "results": [{"markdown": "x"}]}
    out, item = _collect(page)
    assert "parser" not in item and "parser_notes" not in out


def test_screenshot_comes_back_as_an_image_not_base64_text():
    """`format="screenshot"` forces run_js (the API demands it) and the PNG is an image block."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    shot = base64.b64encode(png).decode()

    with _api(lambda p: _done(p, screenshot=shot)) as api:
        out = asyncio.run(scrape("https://ex.com", format="screenshot"))
    assert api.posts[0][0] == "/v1/scrape" and api.posts[0][1]["run_js"] is True
    assert isinstance(out, list) and len(out) == 2, out
    envelope, image = out
    assert isinstance(image, Image) and image.data == png
    assert envelope["results"][0]["screenshot"]["bytes"] == len(png)

    # A screenshot that outlives the sync call is collected through check_requests instead.
    timeout = ApiTimeout("timed out")
    with _api(timeout, _pending):
        started = asyncio.run(scrape("https://ex.com", format="screenshot"))
    assert started["state"] == "pending"

    collected = {
        started["request_id"]: {
            "state": "done",
            "results": [{"screenshot": shot}],
            "params": {"output": ["screenshot"]},
        }
    }
    with _api(collected=collected):
        reply, image = asyncio.run(check_requests([started["request_id"]]))
    assert image.data == png
    assert reply["items"][0]["result"]["results"][0]["screenshot"]["bytes"] == len(png)


def test_check_requests_returns_a_few_screenshots_and_says_where_the_rest_are():
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 100
    limit = config.MAX_SCREENSHOTS_PER_REPLY

    def shot(n):
        return {
            "state": "done",
            "params": {"output": ["screenshot"]},
            "results": [
                {
                    "screenshot": base64.b64encode(png).decode(),
                    "metadata": {"url": f"https://e/{n}"},
                }
            ],
        }

    ids = [str(n) for n in range(1, limit + 3)]
    with _api(collected={n: shot(n) for n in ids}):
        reply, *images = asyncio.run(check_requests(ids))
    assert len(images) == limit and all(image.data == png for image in images)
    notes = [item["result"]["results"][0]["screenshot"]["note"] for item in reply["items"]]
    assert notes[:limit] == ["Returned as an image block."] * limit
    for n, note in enumerate(notes[limit:], start=limit + 1):
        assert f"check_requests(['{n}'])" in note, note


def test_check_requests_returns_results_whole_when_they_fit_together():
    def body(n):
        return {"state": "done", "params": {"output": ["json"]}, "results": [{"json": {"id": n}}]}

    with _api(collected={"1": body("1"), "2": body("2")}):
        out = asyncio.run(check_requests(["1", "2"]))
    assert [i["result"]["results"][0]["json"] for i in out["items"]] == [{"id": "1"}, {"id": "2"}]
    assert "results_note" not in out


def _pages(pages: dict[str, str]) -> dict:
    return {
        n: {
            "state": "done",
            "params": {"output": ["markdown"]},
            "results": [{"markdown": page, "metadata": {"url": f"https://ex.com/{n}"}}],
        }
        for n, page in pages.items()
    }


def test_check_requests_counts_a_page_by_its_tokens_not_its_characters(monkeypatch):
    # Three ~2,300-token pages, ~27k characters together: they fit a 10k-token budget.
    monkeypatch.setattr(config, "MAX_INLINE_TOKENS", 10000)
    with _api(collected=_pages({n: f"page {n} " + "word " * 1800 for n in "123"})):
        out = asyncio.run(check_requests(["1", "2", "3"]))
    assert not any(item.get("summarized") for item in out["items"]), out["items"]
    assert "results_note" not in out


def test_check_requests_shares_what_does_not_fit_as_the_start_of_each_page(monkeypatch):
    # Three ~5k-token pages: each fits the 10k budget alone, together they don't.
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    with _api(collected=_pages({n: f"page {n} " + "word " * 4000 for n in "123"})):
        out = asyncio.run(check_requests(["1", "2", "3"]))
    assert _within_budget(out)
    first, *rest = out["items"]
    # The first page fits on its own and comes whole; the others no longer fit beside it.
    assert first["result"]["results"][0]["markdown"].startswith("page 1 word")
    assert "summarized" not in first
    for n, item in enumerate(rest, start=2):
        assert item["summarized"] is True and "kept" not in item
        (summary,) = item["result"]["results"]
        assert "markdown" not in summary and "path" not in summary
        assert summary["url"] == f"https://ex.com/{n}" and summary["page"] == 0
        # What the whole page did not use is shared: a long start each, not a snippet.
        assert summary["preview"].startswith(f"page {n} ") and len(summary["preview"]) > 5000
        assert summary["next_offset"] == len(summary["preview"])
        assert summary["total_chars"] > 20000
    assert "2 of the 3" in out["results_note"] and "read_result" in out["results_note"]


def test_check_requests_shows_the_start_of_a_lone_result_that_does_not_fit(monkeypatch):
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    with _api(collected=_pages({"7": "word " * 20000})):
        out = asyncio.run(check_requests(["7"]))
    assert _within_budget(out)
    (summary,) = out["items"][0]["result"]["results"]
    # As much of the page as one reply holds, as a single call would give.
    assert len(summary["preview"]) > 30000 and summary["next_offset"] == len(summary["preview"])


def test_check_requests_still_returns_small_pages_after_a_large_one(monkeypatch):
    pages = {"1": "huge " * 12000, "2": "small page " * 50, "3": "also small " * 50}
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    with _api(collected=_pages(pages)):
        huge, small, also_small = asyncio.run(check_requests(["1", "2", "3"]))["items"]
    assert huge["summarized"] is True and "markdown" not in huge["result"]["results"][0]
    for item, n in ((small, "2"), (also_small, "3")):
        assert "summarized" not in item
        assert item["result"]["results"][0]["markdown"] == pages[n]


def test_check_requests_keeps_a_full_batch_within_the_budget(monkeypatch):
    """Fifty finished dedicated parses, each too big to inline: the reply still fits."""

    def body(n):
        status = {"json_parse": {"code": 12000, "tag": "PARSE_SUCCESS"}}
        return {
            "state": "done",
            "params": {"query": n, "output": ["json"]},
            "results": [
                {
                    "json": {"title": f"product {n}", "reviews": ["great " * 20] * 300},
                    "metadata": {"url": f"https://shop.example/{n}", "statuses": status},
                }
            ],
        }

    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    monkeypatch.setattr(config, "MAX_INLINE_TOKENS", 5000)
    ids = [str(7500000000000000000 + n) for n in range(50)]
    with _api(collected={n: body(n) for n in ids}):
        out = asyncio.run(check_requests(ids))
    assert _within_budget(out)
    # The dedicated parser's note comes once, not once per result.
    assert len(out["parser_notes"]) == 1 and "PARSE_SUCCESS" in out["parser_notes"][0]
    assert {item["parser_note"] for item in out["items"]} == {"parser_notes[0]"}
    # Every result is there, as a summary with its query.
    assert all(item["summarized"] for item in out["items"])
    assert [item["input"] for item in out["items"]] == ids
    assert "50 of the 50" in out["results_note"]


def test_check_requests_gives_each_request_its_own_file(monkeypatch, tmp_path):
    """The same URL fetched from two countries is two pages, not one file written twice."""

    def body(country):
        page = f"price in {country} " + "word " * 12000  # over the budget on its own
        return {
            "state": "done",
            "params": {"output": ["markdown"], "location": country},
            "results": [{"markdown": page, "metadata": {"url": "https://shop.example/p"}}],
        }

    monkeypatch.setattr(config, "SPILL_ENABLED", True)
    monkeypatch.setenv("OXYLABS_SPILL_DIR", str(tmp_path))
    with _api(collected={"1": body("DE"), "2": body("US")}):
        out = asyncio.run(check_requests(["1", "2"]))
    paths = [item["result"]["results"][0]["path"] for item in out["items"]]
    assert [path.rsplit("/", 1)[1] for path in paths] == ["1-0.md.txt", "2-0.md.txt"]
    assert [open(path, encoding="utf-8").read(11) for path in paths] == [
        "price in DE",
        "price in US",
    ]


def test_check_requests_writes_summarized_pages_to_disk_when_local(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SPILL_ENABLED", True)
    monkeypatch.setenv("OXYLABS_SPILL_DIR", str(tmp_path))
    with _api(collected=_pages({n: f"page {n} " + "word " * 4000 for n in "123"})) as api:
        out = asyncio.run(check_requests(["1", "2", "3"]))
        for n, item in enumerate(out["items"][1:], start=2):
            (summary,) = item["result"]["results"]
            assert open(summary["path"], encoding="utf-8").read().startswith(f"page {n} word")
            # Reading on goes to that file, not back to the API.
            calls = len(api.calls)
            part = asyncio.run(read_result(str(n), offset=summary["next_offset"], length=10))
            assert part["text"] == (f"page {n} " + "word " * 4000)[summary["next_offset"] :][:10]
            assert len(api.calls) == calls
    assert "read_result" in out["results_note"]


# Lists of URLs or queries.


def _batch_reply(key, items, errors=()):
    return {
        "data": [
            {
                "state": "pending",
                "params": {key: item, "output": ["markdown"], "device": "desktop"},
                "metadata": {"timestamp": 1, "request_id": str(7000 + n)},
            }
            for n, item in enumerate(items)
        ],
        "errors": list(errors),
    }


def test_scrape_queues_a_long_list_as_one_batch():
    urls = [f"https://ex.com/{n}" for n in range(6)] + ["https://bad.example"]
    # The API names a refused item by its place in the request.
    refused = {"detail": "Invalid URL.", "pointer": "#/url/6"}
    with _api(_batch_reply("url", urls[:6], [refused])) as api:
        out = asyncio.run(scrape(urls))
    # Markdown by default, like a single page.
    assert api.posts == [("/v1/async/scrape/batch", {"url": urls, "output": ["markdown"]})]
    assert (out["pending"], out["failed"]) == (6, 1)
    assert [item.get("request_id") for item in out["items"][:6]] == [
        str(7000 + n) for n in range(6)
    ]
    # Every item says which URL it is for; the refused one says why it was not queued.
    assert [item["input"] for item in out["items"]] == urls
    assert out["items"][6] == {
        "input": "https://bad.example",
        "state": "failed",
        "error": "Not queued: Invalid URL.",
    }
    assert "check_requests" in out["note"]


def test_a_list_waits_for_what_it_queued(monkeypatch):
    now = _clock(monkeypatch)
    urls = [f"https://ex.com/{n}" for n in range(6)]

    def finished(n):
        def answer():
            if now[0] < config.POLL_SECONDS:
                return {"state": "pending"}
            body = {"state": "done", "params": {"url": urls[n], "output": ["markdown"]}}
            return {**body, "results": [{"markdown": f"page {n} " * 100}]}

        return answer

    collected = {str(7000 + n): finished(n) for n in range(6)}
    with _api(_batch_reply("url", urls), collected=collected):
        out = asyncio.run(scrape(urls))
    assert out["done"] == 6 and now[0] == config.POLL_SECONDS
    assert [item["result"]["results"][0]["markdown"][:7] for item in out["items"]] == [
        f"page {n} " for n in range(6)
    ]


def test_a_short_list_runs_side_by_side_through_the_sync_endpoint():
    urls = ["https://ex.com/a", "https://ex.com/b", "https://ex.com/c"]
    ids = dict(zip(urls, ("7501", "7502", "7503"), strict=True))

    def page(payload):
        body = _done(payload, markdown=f"text of {payload['url']} " * 40)
        return {**body, "metadata": {"request_id": ids[payload["url"]]}}

    with _api(page, page, page) as api:
        out = asyncio.run(scrape(urls, location="de"))
    # One synchronous call per URL, each with the shared parameters; no batch, no queue.
    assert sorted(payload["url"] for _, payload in api.posts) == urls
    assert {path for path, _ in api.posts} == {"/v1/scrape"}
    assert all(
        p == {"url": p["url"], "output": ["markdown"], "location": "DE"} for _, p in api.posts
    )
    assert out["done"] == 3
    assert [(item["input"], item["request_id"]) for item in out["items"]] == list(ids.items())
    for item in out["items"]:
        assert item["result"]["results"][0]["markdown"].startswith(f"text of {item['input']}")


def test_a_short_list_says_which_pages_cannot_be_read_on(monkeypatch, tmp_path):
    urls = ["https://ex.com/a", "https://ex.com/b"]
    ids = dict(zip(urls, ("7501", "7502"), strict=True))

    def page(payload):
        body = _done(payload, markdown=f"{payload['url']} " + "word " * 12000)
        return {**body, "metadata": {"request_id": ids[payload["url"]]}}

    # Over HTTP: the API keeps no synchronous result, so there is nothing to read on from.
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    with _api(page, page):
        out = asyncio.run(scrape(urls))
    assert _within_budget(out)
    for item in out["items"]:
        (summary,) = item["result"]["results"]
        assert item["kept"] is False and "next_offset" not in summary and summary["preview"]
    assert "kept: false" in out["results_note"]

    # Locally the pages go to disk, so they can be read on like any other.
    monkeypatch.setattr(config, "SPILL_ENABLED", True)
    monkeypatch.setenv("OXYLABS_SPILL_DIR", str(tmp_path))
    with _api(page, page) as api:
        out = asyncio.run(scrape(urls))
        item = out["items"][0]
        (summary,) = item["result"]["results"]
        assert "kept" not in item and "kept: false" not in out["results_note"]
        part = asyncio.run(read_result(item["request_id"], offset=summary["next_offset"]))
        assert part["offset"] == summary["next_offset"] and part["text"]
    assert [path for method, path, _ in api.calls if path.startswith("/v1/async/scrape/")] == []


def test_a_list_that_fails_the_same_way_for_every_item_raises_it():
    refused = ApiError("Authentication failed (401). Check the API key.")
    with _api(refused, refused, refused):
        try:
            asyncio.run(scrape(["https://a.com", "https://b.com", "https://c.com"]))
        except ApiError as exc:
            assert "Authentication failed" in str(exc), exc
        else:
            raise AssertionError("three identical failures are one failed call")


def test_a_batch_that_queued_nothing_is_an_error():
    urls = [f"https://bad.example/{n}" for n in range(6)]
    errors = [{"detail": "Invalid URL.", "pointer": f"#/url/{n}"} for n in range(6)]
    with _api({"errors": errors}):
        try:
            asyncio.run(scrape(urls))
        except ApiError as exc:
            assert "queued nothing" in str(exc) and "'https://bad.example/0'" in str(exc), exc
        else:
            raise AssertionError("a batch with every item refused must be an error")


def test_run_scraper_batches_a_long_list_of_queries():
    asins = [f"B0{n:08d}" for n in range(6)]
    with _api(_batch_reply("query", asins)) as api:
        out = asyncio.run(
            run_scraper("scrape/amazon/product", {"query": asins, "output": ["json"]})
        )
    # The endpoint's own batch, with its dedicated parser; an `output` set is sent as is.
    assert [method for method, _, _ in api.calls if method == "OPTIONS"] == []
    assert api.posts == [
        ("/v1/async/scrape/batch/amazon/product", {"query": asins, "output": ["json"]})
    ]
    assert [item["input"] for item in out["items"]] == asins


def test_lists_are_checked_before_calling_the_api():
    for call in (
        lambda: scrape([]),
        lambda: scrape(["https://ex.com", "relative/page"]),
        lambda: run_scraper("scrape", {"url": ["https://a.com"], "query": ["shoes"]}),
        lambda: run_scraper("scrape/google/search", {"query": []}),
        lambda: run_scraper("scrape/google/search", {"query": ["shoes", ""]}),
        lambda: run_scraper("scrape/google/search", {"query": ["shoes", 7]}),
    ):
        with _api() as api:
            try:
                asyncio.run(call())
            except ApiError:
                pass
            else:
                raise AssertionError("an invalid list must be refused")
        assert api.posts == [], api.posts


def test_a_list_is_capped_and_charges_each_item(monkeypatch):
    sent = []

    async def fake_request(method, path, payload=None, **kwargs):
        if path.startswith("/v1/scrapers"):
            return GROUPS
        if method == "GET":
            return {"state": "pending"}
        sent.append(kwargs)
        return _batch_reply("url", payload["url"])

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr(scraping, "_JSON_GROUPS", {})
    urls = [f"https://ex.com/{n}" for n in range(config.BATCH_LIMIT)]
    out = asyncio.run(scrape(urls))
    assert out["pending"] == config.BATCH_LIMIT
    # Each item is one scrape against OXYLABS_RATE_LIMIT, and a failed POST is not resent.
    assert sent == [{"ctx": None, "idempotent": False, "cost": config.BATCH_LIMIT}]

    try:
        asyncio.run(scrape([*urls, "https://ex.com/x"]))
    except ApiError as exc:
        assert f"at most {config.BATCH_LIMIT}" in str(exc), exc
    else:
        raise AssertionError("a list over the limit must be refused")
    assert len(sent) == 1, "an oversized list must not reach the API"


def test_a_batch_wants_a_prompt_where_there_is_no_dedicated_parser():
    urls = [f"https://ex.com/{n}" for n in range(6)]
    # The API would queue these, and each item would fail on its own at poll time.
    for params in ({"url": urls, "output": ["json"]}, {"url": urls, "json": {}}):
        with _api() as api:
            try:
                asyncio.run(run_scraper("scrape", params))
            except ApiError as exc:
                assert "no dedicated parser" in str(exc) and "json_prompt" in str(exc), exc
            else:
                raise AssertionError(f"{params} must be refused")
        assert api.posts == [], api.posts
    with _api() as api:
        try:
            asyncio.run(run_scraper("scrape/media", {"url": urls, "output": ["json"]}))
        except ApiError as exc:
            assert "cannot return structured JSON" in str(exc), exc
        else:
            raise AssertionError("an endpoint without JSON output must be refused")
    assert api.posts == [], api.posts
    # With a prompt, the AI parser answers wherever JSON is offered.
    with _api(_batch_reply("url", urls)) as api:
        asyncio.run(scrape(urls, json_prompt="the page title"))
    assert api.posts[0][1] == {
        "url": urls,
        "output": ["json"],
        "json": {"prompt": "the page title"},
    }


def test_a_list_defaults_to_markdown_where_the_endpoint_offers_it():
    trends = "/v1/scrape/google/trends/explore"
    only_json = {"properties": {"output": {"type": "array", "items": {"enum": ["json"]}}}}
    queries = [f"q{n}" for n in range(6)]
    replies = [_batch_reply("query", queries) for _ in range(3)]
    with _api(*replies, schemas={trends: only_json}) as api:
        for _ in range(2):
            asyncio.run(run_scraper("scrape/google/search", {"query": queries}))
        asyncio.run(run_scraper("scrape/google/trends/explore", {"query": queries}))
    a, b, c = (payload for _, payload in api.posts)
    # Dedicated endpoints answer in HTML unless asked; Markdown is what an agent can read.
    assert a["output"] == b["output"] == ["markdown"]
    # Trends only answers in JSON, so it keeps its own default instead of being refused.
    assert "output" not in c
    # Each endpoint's schema is read once, not once per list.
    assert [path for method, path, _ in api.calls if method == "OPTIONS"] == [
        "/v1/scrape/google/search?format=json_schema",
        f"{trends}?format=json_schema",
    ]


def test_a_list_labels_each_item_with_its_own_query():
    # `limit_per_page` is a list too, and comes first; the items are still the queries.
    pages = [{"page": 1, "limit": 5}]
    queries = [f"shoes {n}" for n in range(6)]
    params = {"limit_per_page": pages, "query": queries, "output": ["json"]}
    reply = _batch_reply("query", queries)
    for request in reply["data"]:
        request["params"]["limit_per_page"] = pages
    with _api(reply) as api:
        out = asyncio.run(run_scraper("scrape/google/search", params))
    assert api.posts[0][1]["limit_per_page"] == pages
    assert [item["input"] for item in out["items"]] == queries


# Reading a long page in parts.


def test_read_result_takes_only_request_ids():
    for bad in ("../etc/passwd", "abc", "1/2", "١٢٣"):
        with _api() as api:
            try:
                asyncio.run(read_result(bad))
            except ApiError as exc:
                assert "not a request id" in str(exc), exc
            else:
                raise AssertionError(f"read_result must refuse {bad!r}")
        assert api.calls == []


def test_read_result_reads_a_long_page_in_parts_over_http(monkeypatch):
    page = "".join(f"line {n}\n" for n in range(20000))  # ~170k chars, far over one reply
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    with _api(collected=_pages({"7": page})) as api:
        parts, offset = [], 0
        for _ in range(20):
            part = asyncio.run(read_result("7", offset=offset))
            assert part["offset"] == offset and part["total_chars"] == len(page)
            assert _within_budget(part)
            parts.append(part["text"])
            if part["eof"]:
                break
            offset = part["next_offset"]
        else:
            raise AssertionError("the parts must reach the end of the page")
    assert len(parts) > 1 and "".join(parts) == page
    # Each part is read from the API, which keeps the result: no new scrape.
    assert {path for _, path, _ in api.calls} == {"/v1/async/scrape/7"}


def test_read_result_reads_from_disk_when_local(monkeypatch, tmp_path):
    page = "word " * 3000
    monkeypatch.setattr(config, "SPILL_ENABLED", True)
    monkeypatch.setenv("OXYLABS_SPILL_DIR", str(tmp_path))
    with _api(collected=_pages({"7": page})) as api:
        first = asyncio.run(read_result("7", length=100))
        second = asyncio.run(read_result("7", offset=first["next_offset"], length=100))
    assert first["text"] + second["text"] == page[:200]
    # The API is read once; the page then comes from the file it was written to.
    assert len(api.calls) == 1 and (tmp_path / "7-0.md.txt").read_text() == page


def test_read_result_fits_its_reply_to_the_token_budget(monkeypatch):
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    with _api(collected=_pages({"7": "漢" * 30000})):  # a token a character
        part = asyncio.run(read_result("7"))
    assert _within_budget(part) and 0 < part["returned_chars"] < 10000 and not part["eof"]


def test_read_result_explains_what_it_cannot_read(monkeypatch):
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    shot = {"state": "done", "params": {"output": ["screenshot"]}, "results": [{"screenshot": "x"}]}
    gone = ApiNotFound("/v1/async/scrape/8 returned 404: Not found.")
    with _api(collected={"7": shot, "8": gone, "9": {"state": "pending"}}):
        for request_id, reason in (
            ("7", "a screenshot is an image"),
            ("8", "synchronous scrape"),
            ("9", "has not finished"),
        ):
            try:
                asyncio.run(read_result(request_id))
            except ApiError as exc:
                assert reason in str(exc), exc
            else:
                raise AssertionError(f"request {request_id} has no text to read")
        try:
            asyncio.run(read_result("7", page=3))
        except ApiError as exc:
            assert "has 1 page(s)" in str(exc), exc
        else:
            raise AssertionError("a page past the end must be refused")


def test_a_list_scrapes_every_item_it_is_given_even_a_repeated_one():
    """Five times the same URL is five scrapes, run side by side; six go to a batch."""
    url = "https://delfi.lt"
    page = lambda p: _done(p, markdown="news " * 200)  # noqa: E731
    with _api(*[page] * 5) as api:
        out = asyncio.run(scrape([url] * 5))
    assert api.posts == [("/v1/scrape", {"url": url, "output": ["markdown"]})] * 5
    assert out["done"] == 5 and [item["input"] for item in out["items"]] == [url] * 5

    with _api(_batch_reply("url", [url] * 6)) as api:
        out = asyncio.run(scrape([url] * 6))
    assert api.posts == [("/v1/async/scrape/batch", {"url": [url] * 6, "output": ["markdown"]})]
    assert len(out["items"]) == 6

"""Tool input validation, polling, offloaded reads, listing, and the skill."""

from __future__ import annotations

import asyncio
import os

import httpx
from fastmcp.utilities.types import Image
from helpers import _api, _done, _pending, _stub_client

from oxylabs_web_api_mcp import app, client, config, skill, tools
from oxylabs_web_api_mcp.errors import ApiError, ApiFaulted, ApiTimeout
from oxylabs_web_api_mcp.tools import check_scrape, list_scrapers, read_scraped, scrape


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


def test_read_scraped_refuses_paths_outside_the_spill_dir():
    for bad in ("/etc/passwd", "/etc/hosts.txt", os.path.join(os.getcwd(), "pyproject.toml")):
        try:
            asyncio.run(read_scraped(bad))
        except ApiError:
            continue
        raise AssertionError(f"read_scraped must refuse {bad}")


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

    real = client._request
    client._request = fake_request
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
        client._request = real


def test_a_faulted_queued_dedicated_parse_suggests_the_ai_parser():
    params = {"output": ["json"], "json": {"prompt": None, "schema": None}}
    fault = ApiFaulted("faulted (PARSE_FAILED)", tag="PARSE_FAILED", params=params)

    async def fake_request(method, path, payload=None, **kwargs):
        raise fault

    real = client._request
    client._request = fake_request
    try:
        asyncio.run(check_scrape("7500000000000000001"))
    except ApiFaulted as exc:
        assert "parser='ai'" in str(exc), exc
    else:
        raise AssertionError("a faulted request must raise")
    finally:
        client._request = real


def test_scrape_rejects_a_place_name_as_location():
    try:
        asyncio.run(app.mcp.call_tool("scrape", {"url": "https://ex.com", "location": "Germany"}))
    except Exception as exc:  # noqa: BLE001 — FastMCP rejects it before the tool body runs
        assert "pattern" in str(exc), exc
        return
    raise AssertionError("a place name must be rejected before calling the API")


def _collect(body):
    async def fake_request(method, path, payload=None, **kwargs):
        return body

    real = client._request
    client._request = fake_request
    try:
        return asyncio.run(check_scrape("7500000000000000001"))
    finally:
        client._request = real


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


def test_list_scrapers_groups_by_json_support():
    with _api() as api:
        out = asyncio.run(list_scrapers())
    assert api.calls == [("GET", "/v1/scrapers?group_by=json_output_support", None)]
    assert "json_supported" in out["scrapers"]


def test_the_skill_ships_inside_the_package():
    text = skill._skill_text()
    assert not text.startswith("---"), "frontmatter is for a skill loader, not a resource"
    assert "search" in text and "run_js" in text, text[:200]


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
        asyncio.run(check_scrape("77"))
    except ApiFaulted as exc:
        assert exc.tag == "REQUEST_FAILED_AFTER_TOO_MANY_RETRIES", exc.tag
        assert len(calls) == 1, "a faulted request stays faulted; polling it again is waste"
    else:
        raise AssertionError("a faulted async request is a failure")
    finally:
        httpx.AsyncClient, config.RETRY_BASE_DELAY = real_client, real_delay


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
    assert isinstance(image, Image) and image.data == png
    assert envelope["results"][0]["screenshot"]["bytes"] == len(png)

    # A screenshot that outlives the sync call is collected through check_scrape instead.
    timeout = ApiTimeout("timed out")
    with _api(timeout, _pending):
        started = asyncio.run(scrape("https://ex.com", format="screenshot"))
    assert started["state"] == "pending"

    async def collected(method, path, payload=None, **kwargs):
        return {
            "state": "done",
            "results": [{"screenshot": shot}],
            "params": {"output": ["screenshot"]},
        }

    real = client._request
    client._request = collected
    try:
        out = asyncio.run(check_scrape(started["request_id"]))
    finally:
        client._request = real
    envelope, image = out
    assert image.data == png and envelope["result"]["results"][0]["screenshot"]["bytes"] == len(png)

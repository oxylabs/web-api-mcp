"""Offline checks for the bits that aren't just pass-through: error parsing and validation.

Run: python tests/test_server.py   (or: pytest)
"""

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("OXYLABS_API_KEY", "test-key")

from oxylabs_web_api_mcp.server import ApiError, _detail, scrape, search  # noqa: E402


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
    for kwargs in ({"query": "   "}, {"query": "ok", "max_results": 0}, {"query": "ok", "max_results": 21}):
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all checks passed")

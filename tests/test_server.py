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
from oxylabs_web_api_mcp.server import ApiError, _detail, read_scraped, scrape, search  # noqa: E402


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all checks passed")

"""Token budgets, offloading, trimming and empty-page hints."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import time
from pathlib import Path

from helpers import _headers

from oxylabs_web_api_mcp import config, content
from oxylabs_web_api_mcp.errors import ApiError
from oxylabs_web_api_mcp.tools import read_result


def _big_payload(chars: int, request_id: str | None = "7504857924934611969") -> dict:
    # The live API's result shape: page text under the format's own key, the scraped
    # URL under the result's `metadata` — there is no fused `content` field.
    payload = {
        "state": "done",
        "results": [{"markdown": "x" * chars, "metadata": {"url": "https://ex.com/a"}}],
        "params": {"url": "https://ex.com/a"},
    }
    if request_id:
        payload["metadata"] = {"timestamp": 1, "request_id": request_id}
    return payload


def test_oversized_content_spills_to_disk_when_local(tmp_dir=None):
    import tempfile

    spill = tmp_dir or tempfile.mkdtemp()
    os.environ["OXYLABS_SPILL_DIR"] = spill
    config.SPILL_ENABLED = True
    try:
        size = config.MAX_INLINE_TOKENS * 4 + 5000
        out = content._process_content(_big_payload(size), "markdown", config.MAX_INLINE_TOKENS)
        result = out["results"][0]
        info = result["content_offloaded"]

        assert info["total_chars"] == size, info
        assert len(result["markdown"]) == config.PREVIEW_CHARS, len(result["markdown"])
        assert "content_truncated" not in result
        assert os.path.isfile(info["path"]), info
        assert "read_result('7504857924934611969', page=0, offset=2000)" in info["note"]

        # The agent walks the page in chunks, by its request id, and reaches the end.
        first = asyncio.run(read_result("7504857924934611969", offset=0, length=1000))
        assert first["returned_chars"] == 1000 and not first["eof"], first
        last = asyncio.run(read_result("7504857924934611969", offset=size - 10, length=1000))
        assert last["eof"] and last["returned_chars"] == 10, last

        # A page without a request id could not be found again, so it is cut short instead.
        out = content._process_content(
            _big_payload(size, request_id=None), "markdown", config.MAX_INLINE_TOKENS
        )
        assert "content_truncated" in out["results"][0] and "content_offloaded" not in out
    finally:
        config.SPILL_ENABLED = False
        del os.environ["OXYLABS_SPILL_DIR"]


def test_oversized_content_truncates_when_remote():
    config.SPILL_ENABLED = False
    size = config.MAX_INLINE_TOKENS * 4 + 5000
    result = content._process_content(_big_payload(size), "markdown", config.MAX_INLINE_TOKENS)[
        "results"
    ][0]
    assert result["content_truncated"]["total_chars"] == size
    assert result["content_truncated"]["returned_chars"] < size
    assert "content_offloaded" not in result


def test_small_content_is_left_alone():
    out = content._process_content(_big_payload(100), "markdown", config.MAX_INLINE_TOKENS)[
        "results"
    ][0]
    assert out["markdown"] == "x" * 100
    assert "content_offloaded" not in out and "content_truncated" not in out


def test_trim_drops_the_echo_but_keeps_the_request_id():
    out = content._trim(
        {
            "state": "done",
            "results": [1],
            "params": {"query": "x"},
            "metadata": {"timestamp": 1, "request_id": "abc"},
        }
    )
    assert out == {"state": "done", "results": [1], "request_id": "abc"}, out
    # Nothing to keep is fine too.
    assert content._trim({"results": []}) == {"results": []}


def _page(content, output="markdown"):
    return {
        "results": [{output: content, "metadata": {"url": "https://ex.com/a"}}],
        "params": {"output": [output]},
    }


def test_an_empty_shell_is_flagged_for_a_js_retry():
    out = content._flag_thin_content(_page("# Loading\n\n"))["results"][0]
    assert out["content_thin"]["visible_chars"] < config.THIN_CONTENT_CHARS, out
    assert "run_js=True" in out["content_thin"]["note"]


def test_an_empty_rendered_page_is_not_sent_back_to_run_js():
    page = _page("# Loading\n\n")
    page["params"]["run_js"] = True
    note = content._flag_thin_content(page)["results"][0]["content_thin"]["note"]
    assert "Do not retry with run_js" in note and "login" in note, note


def test_a_null_params_echo_does_not_crash_the_content_checks():
    payload = {"results": [{"markdown": "# x"}], "params": None}
    assert "content_thin" in content._flag_thin_content(payload)["results"][0]
    content._process_content(payload, "markdown", 1)


def test_a_real_page_is_left_alone():
    out = content._flag_thin_content(_page("word " * 400))["results"][0]
    assert "content_thin" not in out, out


def test_html_is_measured_on_its_visible_text_not_its_markup():
    shell = (
        "<html><head>"
        + '<meta name="x" content="y">' * 60
        + '</head><body><div id="root"></div><script>var a=1;</script></body></html>'
    )
    assert len(shell) > config.THIN_CONTENT_CHARS
    out = content._flag_thin_content(_page(shell, output="html"))["results"][0]
    assert "content_thin" in out, out


def test_a_javascript_notice_is_flagged_even_above_the_length_floor():
    page = "Home About Contact " * 40 + " You need to enable JavaScript to run this app."
    assert len(page) > config.THIN_CONTENT_CHARS
    out = content._flag_thin_content(_page(page))["results"][0]
    assert out["content_thin"]["reason"] == "the page says it needs JavaScript", out


def test_a_long_article_about_javascript_is_not_a_shell():
    article = "Turn on JavaScript to see the demo. " + ("prose " * 2000)
    out = content._flag_thin_content(_page(article))["results"][0]
    assert "content_thin" not in out, out


def test_offloaded_pages_are_never_called_thin():
    payload = _page("x" * (config.MAX_INLINE_TOKENS * 4 + 100))
    config.SPILL_ENABLED = False
    out = content._flag_thin_content(
        content._process_content(payload, "markdown", config.MAX_INLINE_TOKENS)
    )["results"][0]
    assert "content_truncated" in out and "content_thin" not in out, out


def test_token_estimate_is_script_aware():
    latin = "word " * 200  # 1000 chars of English
    cjk = "\u4e2d" * 1000  # 1000 Chinese characters
    assert content._estimate_tokens(latin) < 300, content._estimate_tokens(latin)
    assert content._estimate_tokens(cjk) == 1000, content._estimate_tokens(cjk)
    # The old char-based rule treated these as equal; they are 4x apart.
    assert content._estimate_tokens(cjk) > content._estimate_tokens(latin) * 3


def test_a_cjk_page_that_fits_in_chars_is_still_offloaded():
    config.SPILL_ENABLED = False
    page = {"results": [{"markdown": "\u4e2d" * 20000}], "params": {}}
    out = content._process_content(page, "markdown", config.MAX_INLINE_TOKENS)["results"][0]
    assert "content_truncated" in out, "20k CJK chars is ~20k tokens and must not go inline"
    assert out["content_truncated"]["estimated_tokens"] == 20000


def test_truncation_notice_says_how_to_get_the_rest():
    config.SPILL_ENABLED = False
    page = {"results": [{"markdown": "word " * 40000}], "params": {}}
    note = content._process_content(page, "markdown", 1000)["results"][0]["content_truncated"][
        "note"
    ]
    for hint in ("X-MCP-Max-Tokens", "stdio", "json_prompt"):
        assert hint in note, note


def test_client_can_declare_its_own_token_budget():
    assert content._token_budget() == config.MAX_INLINE_TOKENS
    with _headers({"x-mcp-max-tokens": "50000"}):
        assert content._token_budget() == 50000
    with _headers({"x-mcp-max-tokens": "0"}):
        assert content._token_budget() is None
    with _headers({"x-mcp-max-tokens": "nonsense"}):
        assert content._token_budget() == config.MAX_INLINE_TOKENS


def test_spill_files_are_named_by_request_id_and_page():
    url = "https://ex.com/a"
    # Each page of a request gets its own file, traceable to the request.
    assert content._spill_stem(url, "7504857924934611969") == "7504857924934611969-0"
    assert content._spill_stem(url, 7504857924934611969, 2) == "7504857924934611969-2"
    # Without an id, or with one that is not all digits, the name is a hash of the URL:
    # an id from the API never shapes a path.
    hashed = content._spill_stem(url)
    assert len(hashed) == 16 and int(hashed, 16) >= 0
    for odd in ("", "../../etc/passwd", "12a", "١٢٣"):
        assert content._spill_stem(url, odd) == hashed, odd


def _result(*pages, output="markdown", request_id="7504857924934611969"):
    """One request's response holding several pages, as a search over a few pages returns."""
    return {
        "state": "done",
        "params": {"output": [output]},
        "metadata": {"request_id": request_id},
        "results": [
            {output: page, "metadata": {"url": f"https://ex.com/{n}"}}
            for n, page in enumerate(pages)
        ],
    }


def _reply_tokens(payload) -> int:
    return content._estimate_tokens(json.dumps(content._trim(payload), ensure_ascii=False))


def test_the_pages_of_one_result_share_one_budget(monkeypatch):
    """Three pages that each fit the budget alone do not all fit one reply."""
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    page = "word " * 7600  # ~9.5k tokens
    out = content._process_content(_result(page, page, page), "markdown", 10000)
    assert _reply_tokens(out) <= 10000
    first, *rest = out["results"]
    assert first["markdown"] == page and "content_truncated" not in first
    assert all(result["content_truncated"]["total_chars"] == len(page) for result in rest)


def test_each_cut_page_of_a_result_can_be_read_on_locally(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SPILL_ENABLED", True)
    monkeypatch.setenv("OXYLABS_SPILL_DIR", str(tmp_path))
    page = "word " * 7600
    out = content._process_content(_result(page, page, page), "markdown", 10000)
    assert _reply_tokens(out) <= 10000
    for index, result in enumerate(out["results"][1:], start=1):
        assert f"page={index}" in result["content_offloaded"]["note"], result
        assert (tmp_path / f"7504857924934611969-{index}.md.txt").read_text() == page


def test_a_large_parsed_result_is_held_to_the_budget(monkeypatch, tmp_path):
    parsed = {
        "reviews": [{"title": f"Review {n}", "content": "Great product. " * 40} for n in range(400)]
    }
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    out = content._process_content(_result(parsed, output="json"), "json", 10000)
    assert _reply_tokens(out) <= 10000
    (result,) = out["results"]
    assert result["content_truncated"]["format"] == "json"
    assert result["json"].startswith('{"reviews"')

    # Locally the whole parse goes to disk, where read_result walks it.
    monkeypatch.setattr(config, "SPILL_ENABLED", True)
    monkeypatch.setenv("OXYLABS_SPILL_DIR", str(tmp_path))
    out = content._process_content(_result(parsed, output="json"), "json", 10000)
    info = out["results"][0]["content_offloaded"]
    assert info["path"].endswith("7504857924934611969-0.json.txt")
    assert json.loads(Path(info["path"]).read_text()) == parsed


def test_every_format_of_a_result_counts(monkeypatch):
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    payload = {
        "params": {"output": ["markdown", "html"]},
        "results": [{"markdown": "word " * 7000, "html": "<p>" + "word " * 7000 + "</p>"}],
    }
    out = content._process_content(payload, "markdown", 10000)
    assert _reply_tokens(out) <= 10000
    (result,) = out["results"]
    assert result["markdown"] == "word " * 7000
    assert result["content_truncated"]["format"] == "html"


def test_pruning_deletes_only_this_servers_old_files(tmp_path):
    stale = time.time() - config.SPILL_TTL_SECONDS - 60
    kept = {
        "notes.txt": True,  # not a name this server writes
        "photo.png": True,
        "7-0.md.txt": False,  # this server's, and stale
        "0123456789abcdef.png": False,
        "7-1.json.txt": True,  # this server's, but fresh
    }
    for name in kept:
        (tmp_path / name).write_text("x")
        if name != "7-1.json.txt":
            os.utime(tmp_path / name, (stale, stale))
    content._prune_spills(tmp_path)
    assert {path.name for path in tmp_path.iterdir()} == {n for n, keep in kept.items() if keep}


def test_the_default_spill_dir_is_private_to_its_user(monkeypatch, tmp_path):
    if not hasattr(os, "getuid"):
        return  # Windows keeps a temp dir per user already
    monkeypatch.setattr(content.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("OXYLABS_SPILL_DIR", "")
    path = content._spill_dir()
    assert path == tmp_path / f"oxylabs-web-api-mcp-{os.getuid()}"
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    # One an older version left readable to others is closed up.
    path.chmod(0o755)
    content._spill_dir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    # A link planted where the directory belongs is refused, not followed.
    path.rmdir()
    (tmp_path / "elsewhere").mkdir()
    path.symlink_to(tmp_path / "elsewhere")
    try:
        content._spill_dir()
    except ApiError as exc:
        assert "not a directory of this user's own" in str(exc), exc
    else:
        raise AssertionError("a symlinked spill directory must be refused")


def test_a_spill_never_writes_through_a_link(monkeypatch, tmp_path):
    monkeypatch.setenv("OXYLABS_SPILL_DIR", str(tmp_path))
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (tmp_path / "7-0.md.txt").symlink_to(victim)
    try:
        content._spill("page", "https://ex.com", "markdown", "7", 0)
    except ApiError:
        pass
    else:
        raise AssertionError("a write must not follow a link")
    assert victim.read_text() == "precious"


def _png(width: int, height: int, padding: int = 5) -> str:
    header = (
        (13).to_bytes(4, "big") + b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")
    )
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + header + b"\0" * padding).decode()


def test_a_screenshot_too_big_for_a_model_is_not_sent_as_an_image(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SPILL_ENABLED", False)
    tall = {"results": [{"screenshot": _png(1280, 20000)}]}
    assert content._decode_screenshots(tall) == []
    info = tall["results"][0]["screenshot"]
    assert (info["width"], info["height"]) == (1280, 20000)
    assert "Not returned as an image" in info["note"] and "markdown" in info["note"], info

    # Locally the file is still there to open.
    monkeypatch.setattr(config, "SPILL_ENABLED", True)
    monkeypatch.setenv("OXYLABS_SPILL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MAX_SCREENSHOT_BYTES", 100)
    heavy = {"results": [{"screenshot": _png(1280, 900, padding=200)}]}
    assert content._decode_screenshots(heavy, "7") == []
    info = heavy["results"][0]["screenshot"]
    assert Path(info["path"]).exists() and "Open the file at `path`" in info["note"], info

    # One that fits goes out as an image, its size noted.
    monkeypatch.setattr(config, "MAX_SCREENSHOT_BYTES", 3_750_000)
    fits = {"results": [{"screenshot": _png(1280, 4000)}]}
    ((info, _),) = content._decode_screenshots(fits, "8")
    assert info["note"] == "Returned as an image block." and info["height"] == 4000


def test_an_error_page_is_not_sent_to_run_js():
    page = _page("# Not Found")
    page["results"][0]["metadata"]["statuses"] = {
        "http_request": {"code": 404, "tag": "HTTP_NOT_FOUND"}
    }
    thin = content._flag_thin_content(page)["results"][0]["content_thin"]
    assert "HTTP 404" in thin["note"] and "run_js will not help" in thin["note"], thin
    assert "run_js=True" not in thin["note"]

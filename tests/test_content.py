"""Token budgets, offloading, trimming and empty-page hints."""

from __future__ import annotations

import asyncio
import os

from helpers import _headers

from oxylabs_web_api_mcp import config, content
from oxylabs_web_api_mcp.tools import read_scraped


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

        # The agent walks the file in chunks and reaches the end.
        first = asyncio.run(read_scraped(info["path"], offset=0, length=1000))
        assert first["returned_chars"] == 1000 and not first["eof"], first
        last = asyncio.run(read_scraped(info["path"], offset=size - 10, length=1000))
        assert last["eof"] and last["returned_chars"] == 10, last
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

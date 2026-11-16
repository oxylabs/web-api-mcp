from __future__ import annotations

import json

import pytest

from oxylabs_web_api_mcp import companies, config
from oxylabs_web_api_mcp.companies import _fit_companies
from oxylabs_web_api_mcp.content import _tokens
from oxylabs_web_api_mcp.errors import ApiError


def _profile(i: int, description: str = "word " * 200, history: int = 6) -> dict:
    return {
        "name": f"Company {i}",
        "website_url": f"https://c{i}.example",
        "linkedin_url": f"https://www.linkedin.com/company/c{i}",
        "description": description,
        "industry": "Software",
        "type": "privately held",
        "founded_year": 2012,
        "employees_count": 100 + i,
        "headquarters": {
            "address": "1 Main Street, Berlin, Germany",
            "city": "Berlin",
            "state": None,
            "country": "Germany",
        },
        "funding": {
            "total_usd": 1_000_000,
            "latest_round": {"type": "series a", "date": "2025-01-01", "amount_usd": None},
            "investors": [f"Investor {n}" for n in range(20)],
        },
        "revenue_annual_usd": None,
        "web_traffic": {
            "visits_monthly": 1000,
            "history": [{"month": f"2026-{m + 1:02d}", "value": m * 1000} for m in range(history)],
        },
    }


def test_fit_companies_leaves_what_fits_alone():
    reply = {"state": "done", "results": [_profile(i) for i in range(3)]}
    assert _fit_companies(reply, None) is reply
    assert _fit_companies(reply, 100_000) is reply
    assert _fit_companies({"state": "done"}, 10) == {"state": "done"}
    assert _fit_companies(["not", "a", "dict"], 1) == ["not", "a", "dict"]


def test_fit_companies_keeps_every_company_as_key_facts_when_that_fits():
    reply = {"state": "done", "results": [_profile(i) for i in range(10)]}
    budget = 2500
    assert _tokens(reply) > budget
    cut = _fit_companies(reply, budget)

    assert [r["name"] for r in cut["results"]] == [f"Company {i}" for i in range(10)]
    assert cut["results"][0] == {
        "name": "Company 0",
        "website_url": "https://c0.example",
        "linkedin_url": "https://www.linkedin.com/company/c0",
        "description": "word " * 39 + "word…",
        "industry": "Software",
        "type": "privately held",
        "founded_year": 2012,
        "employees_count": 100,
        "headquarters": {"city": "Berlin", "country": "Germany"},
        "funding": {"total_usd": 1_000_000},
    }
    assert _tokens(cut) <= budget
    assert "key facts" in cut["note"] and "best of" not in cut["note"]
    assert "its website domain as the `query` and `max_results=1`" in cut["note"]
    assert cut["note"].endswith(companies._ADVICE)


def test_fit_companies_skips_what_a_profile_does_not_have():
    sparse = {"name": "Acme", "funding": None, "headquarters": {"city": None, "country": "Peru"}}
    reply = {"results": [sparse, *[_profile(i) for i in range(20)]]}
    cut = _fit_companies(reply, 2500)
    assert cut["results"][0] == {"name": "Acme", "headquarters": {"country": "Peru"}}


def test_fit_companies_cuts_to_the_name_website_and_country_when_key_facts_do_not_fit():
    reply = {"state": "done", "results": [_profile(i) for i in range(100)]}
    budget = 3500
    cut = _fit_companies(reply, budget)

    assert len(cut["results"]) == 100
    assert cut["results"][0] == {
        "name": "Company 0",
        "website_url": "https://c0.example",
        "headquarters": {"country": "Germany"},
    }
    assert _tokens(cut) <= budget
    assert "name, website and country" in cut["note"] and "best of" not in cut["note"]


def test_fit_companies_drops_the_lowest_ranked_when_even_identities_do_not_fit():
    reply = {"state": "done", "results": [_profile(i) for i in range(100)]}
    budget = 800
    cut = _fit_companies(reply, budget)

    shown = len(cut["results"])
    assert 1 < shown < 100
    assert [r["name"] for r in cut["results"]] == [f"Company {i}" for i in range(shown)]
    assert f"Showing the {shown} best of 100" in cut["note"]
    assert _tokens(cut) <= budget


def test_fit_companies_shortens_a_single_oversized_profile_rather_than_cutting_it_off():
    one = {"state": "done", "results": [_profile(0, description="x " * 5000, history=5000)]}
    budget = 800
    cut = _fit_companies(one, budget)

    (only,) = cut["results"]
    assert only["name"] == "Company 0" and only["funding"] == {"total_usd": 1_000_000}
    assert _tokens(cut) <= budget

    # A budget under the reserve and the envelope must still return something, not crash.
    tiny = _fit_companies(one, 1)
    assert len(tiny["results"]) == 1


def test_fit_companies_goes_over_a_tiny_budget_by_a_bounded_amount_and_none_above_it():
    reply = {"state": "done", "request_id": "1795001684465901673"}
    reply["results"] = [_profile(i) for i in range(30)]
    fixed = _tokens({**reply, "results": []})
    longest_note = _tokens({"note": "x " * 60})  # the real note is about this long

    for budget in (1, 50, 100, 200):
        cut = _fit_companies(reply, budget)
        assert cut["results"]
        assert _tokens(cut) <= fixed + longest_note + companies._MIN_ROOM_TOKENS + 60, budget

    # Once the budget holds the fixed fields, the note and the minimum, it is honoured.
    for budget in range(400, 1200, 50):
        assert _tokens(_fit_companies(reply, budget)) <= budget, budget


def test_fit_companies_does_not_count_the_replys_own_note_twice():
    """The note we add contains the API's, so the API's must not also eat the room."""
    reply = {"state": "done", "results": [_profile(i) for i in range(30)]}
    cut = _fit_companies(reply, 1200)
    assert len(cut["results"]) == 30

    reply["note"] = "Credits remaining: 40. " + "The API says hello. " * 40
    budget = 1200
    cut = _fit_companies(reply, budget)
    assert len(cut["results"]) == 30, len(cut["results"])
    assert cut["note"].startswith(reply["note"]) and _tokens(cut) <= budget


def test_fit_companies_spills_the_full_list_for_read_result(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SPILL_ENABLED", True)
    monkeypatch.setenv("OXYLABS_SPILL_DIR", str(tmp_path))
    results = [_profile(i) for i in range(10)]
    reply = {"state": "done", "request_id": "123456789", "results": results}
    cut = _fit_companies(reply, 2500)

    assert "read_result('123456789', page=0)" in cut["note"]
    spilled = json.loads((tmp_path / "123456789-0.json.txt").read_text())
    assert spilled == results


def test_fit_companies_does_not_spill_without_a_request_id(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SPILL_ENABLED", True)
    monkeypatch.setenv("OXYLABS_SPILL_DIR", str(tmp_path))
    reply = {"state": "done", "results": [_profile(i) for i in range(10)]}
    cut = _fit_companies(reply, 2500)
    assert "read_result" not in cut["note"] and not list(tmp_path.iterdir())


def test_fit_companies_expects_a_trimmed_reply_and_does_not_spill_an_untrimmed_one(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "SPILL_ENABLED", True)
    monkeypatch.setenv("OXYLABS_SPILL_DIR", str(tmp_path))
    raw = {
        "state": "done",
        "results": [_profile(i) for i in range(10)],
        "metadata": {"request_id": "123456789"},
    }
    cut = _fit_companies(raw, 2500)

    assert "key facts" in cut["note"]
    assert "read_result" not in cut["note"] and not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("note", "prefix"), [("Existing note.", "Existing note. "), ({"credits": 3}, '{"credits": 3} ')]
)
def test_fit_companies_keeps_a_note_the_reply_already_had(note, prefix):
    reply = {"note": note, "results": [_profile(i) for i in range(10)]}
    cut = _fit_companies(reply, 2500)
    assert cut["note"].startswith(prefix) and "key facts" in cut["note"]


def test_fit_companies_counts_the_note_against_the_budget():
    """A list that fits only until the note is added must still come back within budget."""
    reply = {"results": [_profile(i, description="word " * 20) for i in range(10)]}
    full = _tokens(reply)
    for slack in range(-30, 30, 3):
        budget = full + slack
        cut = _fit_companies(reply, budget)
        assert _tokens(cut) <= budget, slack


def test_fit_companies_fits_profiles_in_a_script_that_costs_a_token_a_character():
    reply = {"results": [_profile(i, description="公司" * 300) for i in range(6)]}
    budget = 1200
    cut = _fit_companies(reply, budget)
    assert len(cut["results"]) == 6 and _tokens(cut) <= budget


def test_fit_companies_cuts_an_unrecognised_item_to_its_start_and_keeps_the_rest():
    odd = {"名称": "公司", "history": ["月" * 50] * 100}
    reply = {"results": [_profile(0), odd, *[_profile(i) for i in range(1, 29)]]}
    budget = 1500
    cut = _fit_companies(reply, budget)

    assert len(cut["results"]) == 30 and _tokens(cut) <= budget
    stub = cut["results"][1]["truncated_json"]
    assert stub.startswith('{"名称": "公司"') and len(stub) == companies._UNKNOWN_KEEP


def test_fit_companies_cuts_a_first_item_that_is_too_long_on_its_own():
    wide = {"name": "x" * 50_000}
    reply = {"results": [wide, _profile(1)]}
    budget = 600
    cut = _fit_companies(reply, budget)

    (only,) = cut["results"]
    assert isinstance(only, dict) and only["truncated_json"].startswith('{"name": "xxx')
    assert "first item is itself too long" in cut["note"]
    assert _tokens(cut) <= budget


@pytest.mark.parametrize(
    "error",
    [
        ApiError("no spill for you"),
        OSError("disk full"),
        UnicodeEncodeError("utf-8", "", 0, 1, "x"),
    ],
)
def test_fit_companies_survives_a_spill_directory_it_cannot_write(monkeypatch, caplog, error):
    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(config, "SPILL_ENABLED", True)
    monkeypatch.setattr(companies, "_spill", refuse)
    reply = {"request_id": "123456789", "results": [_profile(i) for i in range(10)]}
    with caplog.at_level("WARNING", logger="oxylabs_web_api_mcp"):
        cut = _fit_companies(reply, 250)
    assert cut["results"] and "read_result" not in cut["note"]
    assert "123456789" in caplog.text and str(error) in caplog.text


def test_fit_companies_leaves_a_short_unrecognised_item_as_it_is():
    odd = {"id": 5, "foo": "bar"}
    reply = {"results": [odd, *[_profile(i) for i in range(20)]]}
    cut = _fit_companies(reply, 2500)
    assert cut["results"][0] == odd


def test_fit_companies_drops_a_blank_description_rather_than_keeping_it_empty():
    blank = {**_profile(0), "description": "   \n "}
    reply = {"results": [blank, *[_profile(i) for i in range(1, 20)]]}
    cut = _fit_companies(reply, 2500)
    assert "description" not in cut["results"][0]

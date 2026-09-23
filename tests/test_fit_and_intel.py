"""Tests for the deterministic halves of drafting: the ICP screen and company intel.

Neither may spend a LinkedIn request or a model token on a lead it can
settle from stored data.
"""

import asyncio
import json
import urllib.error

from test_compose_capture import MEMBER_ID, PID, panel
from test_live_outreach import FakeLivePage, H, env, no_pause, use_page  # noqa: F401

from sales_nav_mcp.company_intel import company_intel, summarize_dossier, tinyfish_news
from sales_nav_mcp.fit_rules import FitRules, load_rules, screen
from sales_nav_mcp.live_outreach import next_lead
from sales_nav_mcp.store import Store

LEAD = {
    "title": "Founder & CEO",
    "companyName": "Pixel Agency",
    "companyIndustry": "Advertising Services",
    "summary": "We run a boutique marketing agency for SaaS brands.",
    "positions": [{"description": "Leading client delivery."}],
}

DOSSIER = {
    "strategicPriorities": [
        {"text": "AI-First CRM", "description": "Rebuilding the CRM around agents."},
        {"text": "Partner channel", "description": "Growing resellers in EMEA."},
    ],
    "challenges": [{"text": "Data silos", "description": "CRM and billing disagree."}],
    "revenueDetails": {"text": "Subscription SaaS priced per seat."},
    "workforcePlanningTextViewModel": [{"text": "Sales headcount up 28%."}],
    "bingCompanyNews": {"newsArticle": [{"newsTitle": "Acme raises Series A"}]},
    "entityUrn": "urn:li:fs_salesAccountIq:(-1,1)",
}


# ---------------------------------------------------------------- fit rules


class TestScreen:
    def test_empty_rules_pass_everyone(self):
        assert screen(LEAD, FitRules()) is None

    def test_keyword_in_summary(self):
        assert (
            screen(LEAD, FitRules(exclude_keywords=["agency"]))
            == "fit: keyword 'agency'"
        )

    def test_whole_words_only(self):
        # "agency" must not fire on "agencies", nor "ai" on "email".
        lead = {**LEAD, "companyName": "X", "summary": "We help agencies with email."}
        assert screen(lead, FitRules(exclude_keywords=["agency", "ai"])) is None

    def test_field_specific_lists(self):
        assert screen(LEAD, FitRules(exclude_industries=["Advertising Services"])) == (
            "fit: industry 'Advertising Services'"
        )
        assert screen(LEAD, FitRules(exclude_title_keywords=["founder"])) == (
            "fit: title 'founder'"
        )
        assert screen(LEAD, FitRules(exclude_companies=["pixel agency"])) == (
            "fit: company 'pixel agency'"
        )

    def test_load_rules(self, tmp_path):
        assert load_rules(None).empty
        assert load_rules(tmp_path / "missing.json").empty
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert load_rules(bad).empty
        good = tmp_path / "rules.json"
        good.write_text(json.dumps({"exclude_keywords": ["agency"], "junk": 1}))
        assert load_rules(good).exclude_keywords == ["agency"]


# ------------------------------------------------------------- company intel


class TestSummarizeDossier:
    def test_compacts_the_dossier(self):
        s = summarize_dossier(DOSSIER)
        assert s["source"] == "linkedin_account_iq"
        assert s["priorities"][0] == "AI-First CRM: Rebuilding the CRM around agents."
        assert s["challenges"] == ["Data silos: CRM and billing disagree."]
        assert s["revenueModel"] == "Subscription SaaS priced per seat."
        assert s["workforce"] == ["Sales headcount up 28%."]
        assert s["news"] == ["Acme raises Series A"]

    def test_empty_shell_is_none(self):
        assert summarize_dossier({"entityUrn": "x", "uuid": "y"}) is None
        assert summarize_dossier(None) is None

    def test_long_text_is_clipped(self):
        raw = {"challenges": [{"text": "T", "description": "word " * 200}]}
        assert len(summarize_dossier(raw)["challenges"][0]) <= 190


def fake_fetch(results, calls=None):
    def fetch(url, headers):
        if calls is not None:
            calls.append((url, headers))
        return {"results": results}

    return fetch


class TestTinyfish:
    def test_keeps_only_items_naming_the_company(self):
        results = [
            {"title": "Acme raises $10M", "date": "Jan 2026", "url": "u1"},
            {"title": "Unrelated story", "snippet": "nothing here", "url": "u2"},
            {"title": "Funding news", "snippet": "Acme expands to EMEA", "url": "u3"},
        ]
        calls = []
        out = tinyfish_news("Acme", "k", fetch=fake_fetch(results, calls))
        assert [n["url"] for n in out["news"]] == ["u1", "u3"]
        url, headers = calls[0]
        assert url.startswith("https://api.search.tinyfish.ai/?")
        assert "domain_type=news" in url
        assert headers["X-API-Key"] == "k"

    def test_failure_is_none(self):
        def boom(url, headers):
            raise urllib.error.URLError("down")

        assert tinyfish_news("Acme", "k", fetch=boom) is None
        assert tinyfish_news("Acme", "", fetch=boom) is None


class TestCompanyIntel:
    def test_dossier_wins_and_skips_tinyfish(self, tmp_path):
        s = Store(tmp_path / "t.db")
        s.record_capture(
            {
                "member_id": 1,
                "http_status": 200,
                "member_badges": {"openLink": True},
                "dossier": {"company_id": 7, "raw": DOSSIER},
            }
        )
        calls = []
        out = company_intel(
            s,
            company_id=7,
            company_name="Acme",
            api_key="k",
            fetch=fake_fetch([], calls),
        )
        assert out["source"] == "linkedin_account_iq"
        assert calls == []
        s.close()

    def test_tinyfish_fallback_is_cached(self, tmp_path):
        s = Store(tmp_path / "t.db")
        calls = []
        fetch = fake_fetch([{"title": "Acme ships v2", "url": "u"}], calls)
        first = company_intel(
            s, company_id=None, company_name="Acme", api_key="k", fetch=fetch
        )
        again = company_intel(
            s, company_id=None, company_name="Acme", api_key="k", fetch=fetch
        )
        assert first == again
        assert first["source"] == "tinyfish_news"
        assert len(calls) == 1

    def test_nothing_found_is_cached_too(self, tmp_path):
        s = Store(tmp_path / "t.db")
        calls = []
        fetch = fake_fetch([], calls)
        for _ in range(2):
            assert (
                company_intel(
                    s, company_id=None, company_name="Acme", api_key="k", fetch=fetch
                )
                is None
            )
        assert len(calls) == 1

    def test_no_key_no_fallback(self, tmp_path):
        s = Store(tmp_path / "t.db")
        assert (
            company_intel(s, company_id=None, company_name="Acme", api_key="") is None
        )


# -------------------------------------------------------- screen in the loop


def test_fit_screen_skips_before_any_panel_opens(env, monkeypatch, tmp_path):  # noqa: F811
    store, app = env
    rules = tmp_path / "fit_rules.json"
    # REAL_LEAD's company appears in its record; exclude it by name.
    record = next(iter(store.iter_records(H)))
    rules.write_text(json.dumps({"exclude_companies": [record["companyName"]]}))
    app.outreach.fit_rules_file = str(rules)
    page = FakeLivePage(panel(), [PID])
    use_page(monkeypatch, page)
    out = asyncio.run(next_lead(H, "live-test", "supervised", pacer=no_pause()))
    assert out["status"] == "done"
    assert out["skipped_so_far"] == {"fit rules": 1}
    assert page.clicks == []  # no panel was opened
    row = store.outreach_row(MEMBER_ID, "live-test")
    assert row["status"] == "skipped"
    assert row["last_error"].startswith("fit: company")

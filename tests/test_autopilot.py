"""Tests for unattended batches.

The point of these is that automation gets no shortcuts: a model-written draft
goes through exactly the same gates as a hand-written one, and a bad draft is
skipped rather than sent or allowed to end the run.
"""

import asyncio

import pytest
from test_logic import REAL_LEAD

from sales_nav_mcp.autopilot import extract_draft, lead_payload, run_batch
from sales_nav_mcp.config import AppConfig, OutreachConfig
from sales_nav_mcp.normalize import normalize_person
from sales_nav_mcp.store import Store, query_hash

PEOPLE_URL = "https://www.linkedin.com/sales/search/people?query=(filters:List())"
MEMBER_ID = 100000001

GOOD = '{"subject": "a subject", "body": "grounded body", "evidence_used": ["title"]}'


class FakeCtx:
    """Stands in for the MCP client's model."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def sample(self, messages, **kw):
        self.prompts.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return type("R", (), {"text": reply})()


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    offer = tmp_path / "offer.md"
    offer.write_text("We automate manual data work.", encoding="utf-8")
    app = AppConfig()
    app.outreach = OutreachConfig(enabled=True, daily_cap=10, offer_file=str(offer))
    monkeypatch.setattr("sales_nav_mcp.autopilot.get_config", lambda: app)
    monkeypatch.setattr("sales_nav_mcp.outreach.get_config", lambda: app)
    return app


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = Store(tmp_path / "test.db")
    s.upsert_query(PEOPLE_URL, "contacts")
    h = query_hash(PEOPLE_URL)
    s.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
    s.upsert_enrichment(
        [
            {
                "member_id": MEMBER_ID,
                "http_status": 200,
                "member_badges": {"openLink": True},
            }
        ]
    )
    monkeypatch.setattr("sales_nav_mcp.autopilot.get_store", lambda: s)
    monkeypatch.setattr("sales_nav_mcp.outreach.get_store", lambda: s)
    yield s
    s.close()


def batch(ctx, **kw):
    return asyncio.run(
        run_batch(query_hash(PEOPLE_URL), "c1", ctx, **{"dry_run": True, **kw})
    )


class TestExtractDraft:
    def test_bare_json(self):
        d = extract_draft(GOOD)
        assert d["subject"] == "a subject"
        assert d["evidence_used"] == ["title"]

    def test_fenced_json(self):
        assert extract_draft(f"```json\n{GOOD}\n```")["body"] == "grounded body"

    def test_json_embedded_in_prose(self):
        text = f"Sure, here you go:\n{GOOD}\nHope that helps!"
        assert extract_draft(text)["subject"] == "a subject"

    def test_string_evidence_is_wrapped(self):
        d = extract_draft('{"subject":"s","body":"b","evidence_used":"title"}')
        assert d["evidence_used"] == ["title"]

    def test_missing_evidence_becomes_empty(self):
        """Empty evidence is not silently invented; the gate rejects it later."""
        assert extract_draft('{"subject":"s","body":"b"}')["evidence_used"] == []

    def test_rejects_unusable(self):
        assert extract_draft("") is None
        assert extract_draft("no json here") is None
        assert extract_draft("[1,2,3]") is None
        assert extract_draft('{"subject":"only"}') is None


class TestLeadPayload:
    def test_includes_useful_fields_only(self):
        payload = lead_payload(normalize_person(REAL_LEAD))
        assert "companyName" in payload
        assert "entityUrn" not in payload  # no auth tokens handed to the model


class TestBatch:
    def test_dry_run_drafts_but_sends_nothing(self, store, cfg):
        res = batch(FakeCtx([GOOD]))
        assert res["drafted"] == 1
        assert res["sent"] == 0
        assert res["results"][0]["outcome"] == "would_send"
        assert store.outreach_row(MEMBER_ID, "c1") is None

    def test_ungrounded_draft_is_rejected_not_sent(self, store, cfg):
        """The evidence gate applies to model output exactly as to a human's."""
        bad = '{"subject":"s","body":"b","evidence_used":["conferences[0].name"]}'
        res = batch(FakeCtx([bad]))
        assert res["sent"] == 0
        assert res["results"][0]["outcome"] == "rejected"
        assert any("not a field" in p for p in res["results"][0]["problems"])

    def test_unparseable_draft_is_skipped(self, store, cfg):
        res = batch(FakeCtx(["I'm afraid I can't do that"]))
        assert res["drafted"] == 0
        assert res["results"][0]["outcome"] == "unparseable_draft"

    def test_sampling_failure_does_not_end_the_run(self, store, cfg):
        res = batch(FakeCtx([RuntimeError("client refused")]))
        assert res["results"][0]["outcome"] == "sampling_failed"
        assert res["sent"] == 0

    def test_already_contacted_lead_is_not_offered(self, store, cfg):
        store.record_outreach(MEMBER_ID, "earlier", "sent")
        res = batch(FakeCtx([GOOD]))
        assert res["considered"] == 0
        assert res["drafted"] == 0

    def test_missing_offer_file_refuses(self, store, cfg):
        cfg.outreach.offer_file = ""
        res = batch(FakeCtx([GOOD]))
        assert res["error"] == "no_offer_file"
        assert res["sent"] == 0

    def test_offer_text_reaches_the_model(self, store, cfg):
        ctx = FakeCtx([GOOD])
        batch(ctx)
        assert "We automate manual data work." in ctx.prompts[0]

    def test_lead_data_reaches_the_model(self, store, cfg):
        ctx = FakeCtx([GOOD])
        batch(ctx)
        assert "Jordan Rivera" in ctx.prompts[0]

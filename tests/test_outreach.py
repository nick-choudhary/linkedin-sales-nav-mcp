"""Tests for outreach: the evidence gate, dedupe, caps, and the dry run.

The DOM send itself is not exercised here — it needs a real browser and a real
recipient. Everything that decides *whether* to send is, because that is where
a mistake is unrecoverable: an unsent bad message costs nothing, a sent one
cannot be taken back.
"""

import asyncio
import time

import pytest
from test_logic import REAL_LEAD

from sales_nav_mcp.config import AppConfig, OutreachConfig
from sales_nav_mcp.normalize import normalize_person
from sales_nav_mcp.outreach import (
    MessageRejected,
    build_compose_prompt,
    flatten_record,
    send_message,
    validate_message,
)
from sales_nav_mcp.store import Store, query_hash

PEOPLE_URL = "https://www.linkedin.com/sales/search/people?query=(filters:List())"
MEMBER_ID = 100000001
OTHER_ID = 100000002


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.upsert_query(PEOPLE_URL, "contacts")
    h = query_hash(PEOPLE_URL)
    s.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
    yield s
    s.close()


@pytest.fixture
def cfg(monkeypatch):
    """Config with sending ON, so tests exercise guards not the master switch."""
    app = AppConfig()
    app.outreach = OutreachConfig(enabled=True, daily_cap=10)
    monkeypatch.setattr("sales_nav_mcp.outreach.get_config", lambda: app)
    return app


@pytest.fixture
def wired(store, cfg, monkeypatch):
    monkeypatch.setattr("sales_nav_mcp.outreach.get_store", lambda: store)
    return store, cfg


RECORD = normalize_person(REAL_LEAD)
GOOD_EVIDENCE = ["positions[0].title", "companyName"]


class TestFlatten:
    def test_dotted_and_indexed_paths(self):
        flat = flatten_record(RECORD)
        assert "companyName" in flat
        assert "positions[0].title" in flat

    def test_raw_is_excluded(self):
        flat = flatten_record(normalize_person(REAL_LEAD, include_raw=True))
        assert not any(k.startswith("_raw") for k in flat)


class TestValidation:
    def test_clean_message_passes(self, cfg):
        assert (
            validate_message("A specific subject", "Short body.", GOOD_EVIDENCE, RECORD)
            == []
        )

    def test_empty_subject_or_body(self, cfg):
        assert any(
            "subject is empty" in p
            for p in validate_message("", "b", GOOD_EVIDENCE, RECORD)
        )
        assert any(
            "body is empty" in p
            for p in validate_message("s", "  ", GOOD_EVIDENCE, RECORD)
        )

    def test_length_caps(self, cfg):
        cfg.outreach.subject_max_chars = 5
        cfg.outreach.body_max_chars = 5
        problems = validate_message(
            "way too long", "also too long", GOOD_EVIDENCE, RECORD
        )
        assert any("subject is" in p for p in problems)
        assert any("body is" in p for p in problems)

    def test_banned_phrases_rejected(self, cfg):
        problems = validate_message(
            "Hi",
            "I hope this finds you well, quick question for you.",
            GOOD_EVIDENCE,
            RECORD,
        )
        assert any("banned phrase" in p for p in problems)

    def test_empty_evidence_rejected(self, cfg):
        """A message with no grounding is a template, not personalization."""
        problems = validate_message("Subject", "Body", [], RECORD)
        assert any("evidence_used is empty" in p for p in problems)

    def test_ungrounded_evidence_rejected(self, cfg):
        """The anti-hallucination gate: you cannot cite what was never fetched."""
        problems = validate_message(
            "Subject", "Loved your DMA talk", ["conferences[0].name"], RECORD
        )
        assert any("not a field of the fetched lead record" in p for p in problems)

    def test_evidence_with_suffix_resolves(self, cfg):
        """`field:qualifier` resolves on the field part."""
        assert validate_message("S", "B", ["companyName:exact"], RECORD) == []


class TestStoreState:
    def test_candidate_requires_open_profile(self, store):
        h = query_hash(PEOPLE_URL)
        assert store.outreach_candidates(h, "c1") == []
        store.upsert_enrichment(
            [
                {
                    "member_id": MEMBER_ID,
                    "http_status": 200,
                    "member_badges": {"openLink": True},
                }
            ]
        )
        assert len(store.outreach_candidates(h, "c1")) == 1

    def test_open_profile_only_false_includes_unenriched(self, store):
        h = query_hash(PEOPLE_URL)
        assert len(store.outreach_candidates(h, "c1", open_profile_only=False)) == 1

    def test_sent_excluded_from_every_campaign(self, store):
        """The core dedupe rule: contacted once, never again."""
        h = query_hash(PEOPLE_URL)
        store.upsert_enrichment(
            [
                {
                    "member_id": MEMBER_ID,
                    "http_status": 200,
                    "member_badges": {"openLink": True},
                }
            ]
        )
        store.record_outreach(MEMBER_ID, "campaign-one", "sent", channel="open_profile")
        assert store.outreach_candidates(h, "campaign-two") == []
        assert store.already_contacted(MEMBER_ID) == "campaign-one"

    def test_skipped_excluded_from_same_campaign_only(self, store):
        h = query_hash(PEOPLE_URL)
        store.upsert_enrichment(
            [
                {
                    "member_id": MEMBER_ID,
                    "http_status": 200,
                    "member_badges": {"openLink": True},
                }
            ]
        )
        store.record_outreach(MEMBER_ID, "c1", "skipped")
        assert store.outreach_candidates(h, "c1") == []
        assert len(store.outreach_candidates(h, "c2")) == 1

    def test_attempts_and_upsert(self, store):
        store.record_outreach(
            MEMBER_ID, "c1", "failed", last_error="boom", bump_attempts=True
        )
        store.record_outreach(
            MEMBER_ID, "c1", "failed", last_error="boom2", bump_attempts=True
        )
        row = store.outreach_row(MEMBER_ID, "c1")
        assert row["attempts"] == 2
        assert row["last_error"] == "boom2"

    def test_sent_since_counts_across_campaigns(self, store):
        store.record_outreach(MEMBER_ID, "c1", "sent")
        store.record_outreach(OTHER_ID, "c2", "sent")
        assert store.sent_since(time.time() - 60) == 2
        assert store.sent_since(time.time() + 60) == 0

    def test_unknown_status_rejected(self, store):
        with pytest.raises(ValueError):
            store.record_outreach(MEMBER_ID, "c1", "definitely-not-a-status")


class TestDryRun:
    def _send(self, **kw):
        args = dict(
            member_id=MEMBER_ID,
            campaign="c1",
            subject="A specific subject",
            body="Short grounded body.",
            evidence_used=GOOD_EVIDENCE,
            dry_run=True,
            url_hash=query_hash(PEOPLE_URL),
        )
        args.update(kw)
        member_id = args.pop("member_id")
        campaign = args.pop("campaign")
        subject = args.pop("subject")
        body = args.pop("body")
        return asyncio.run(send_message(member_id, campaign, subject, body, **args))

    def test_dry_run_reports_would_send_and_sends_nothing(self, wired):
        store, _ = wired
        result = self._send()
        assert result["would_send"] is True
        assert result["sent"] is False
        assert result["dry_run"] is True
        # nothing persisted — a dry run must leave no trace
        assert store.outreach_row(MEMBER_ID, "c1") is None

    def test_dry_run_surfaces_validation_problems(self, wired):
        result = self._send(evidence_used=[])
        assert result["would_send"] is False
        assert any("evidence_used is empty" in p for p in result["problems"])

    def test_already_contacted_blocks_send(self, wired):
        store, _ = wired
        store.record_outreach(MEMBER_ID, "earlier", "sent")
        result = self._send()
        assert result["would_send"] is False
        assert any("already messaged" in p for p in result["problems"])

    def test_daily_cap_blocks_send(self, wired):
        store, cfg = wired
        cfg.outreach.daily_cap = 1
        store.record_outreach(OTHER_ID, "other", "sent")
        result = self._send()
        assert result["would_send"] is False
        assert any("daily cap" in p for p in result["problems"])

    def test_unknown_member_is_refused(self, wired):
        with pytest.raises(MessageRejected):
            self._send(member_id=999999999)


class TestComposePrompt:
    def test_refuses_without_offer_file(self, cfg):
        """A public install has nothing to sell and must not pretend to."""
        text = build_compose_prompt('{"fullName": "X"}')
        assert "no offer file is configured" in text
        assert "OFFER_FILE" in text

    def test_includes_offer_and_lead_when_configured(self, cfg, tmp_path):
        offer = tmp_path / "offer.md"
        offer.write_text("We clean mailing files.", encoding="utf-8")
        cfg.outreach.offer_file = str(offer)
        text = build_compose_prompt('{"fullName": "Jordan Rivera"}')
        assert "We clean mailing files." in text
        assert "Jordan Rivera" in text
        assert "evidence_used" in text

    def test_offer_text_is_read_fresh_each_render(self, cfg, tmp_path):
        offer = tmp_path / "offer.md"
        offer.write_text("v1", encoding="utf-8")
        cfg.outreach.offer_file = str(offer)
        assert "v1" in build_compose_prompt("{}")
        offer.write_text("v2", encoding="utf-8")
        assert "v2" in build_compose_prompt("{}")

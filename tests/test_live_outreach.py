"""Tests for live outreach: open a lead from its row, draft, count down, send.

What must hold: a lead is only ever messaged through the two-phase commit,
only when Open Profile, and what is sent is what the panel holds at zero --
edits included. Draft mode and Skip never click Send.
"""

import asyncio
import json

import pytest
from test_compose_capture import (
    CONTACT_INFO,
    INSIGHTS_OBSERVED,
    MEMBER_ID,
    PEOPLE_URL,
    PID,
    FakeBrowser,
    FakeButton,
    FakeSearchPage,
    panel,
)
from test_logic import REAL_LEAD

from sales_nav_mcp import live_outreach
from sales_nav_mcp.config import AppConfig, OutreachConfig, PacingConfig
from sales_nav_mcp.live_outreach import countdown, next_lead, submit
from sales_nav_mcp.normalize import normalize_person
from sales_nav_mcp.pacing import Pacer
from sales_nav_mcp.store import Store, query_hash

H = query_hash(PEOPLE_URL)
CAMPAIGN = "live-test"


class FakeLivePage(FakeSearchPage):
    """A search page whose panel can be filled, counted down and sent."""

    def __init__(self, panel_responses, row_pids, *, decision=None, edit=None):
        super().__init__(panel_responses, row_pids)
        self.values = {}
        self.decision = decision
        self.edit = edit  # applied to the panel during the countdown
        self.state_reads = 0

    def get_by_role(self, role, name=None, exact=None):
        assert name == "Send", f"unexpected button {name!r}"
        return FakeButton(self, "Send")

    async def fill(self, selector, value):
        self.values[selector] = value

    async def input_value(self, selector):
        return self.values.get(selector, "")

    async def evaluate(self, js, *args):
        if "free to open profile" in js:
            return json.dumps(
                {"free": True, "credit": False, "credits_remaining": None}
            )
        if "__snmLive ||" in js:
            self.state_reads += 1
            if self.edit:
                self.values.update(self.edit)
                self.edit = None
            return json.dumps({"decision": self.decision, "paused": False})
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = Store(tmp_path / "t.db")
    s.upsert_query(PEOPLE_URL, "contacts")
    s.add_records(H, "contacts", [normalize_person(REAL_LEAD)])
    app = AppConfig()
    app.outreach = OutreachConfig(enabled=True, daily_cap=5, enable_enrich=True)
    for mod in ("live_outreach", "outreach", "compose_capture"):
        monkeypatch.setattr(f"sales_nav_mcp.{mod}.get_config", lambda: app)
    monkeypatch.setattr("sales_nav_mcp.live_outreach.get_store", lambda: s)
    monkeypatch.setattr("sales_nav_mcp.live_outreach.get_browser", FakeBrowser)
    monkeypatch.setattr("sales_nav_mcp.live_outreach._RESULTS_SETTLE_S", 0)
    monkeypatch.setattr("sales_nav_mcp.compose_capture._PANEL_TIMEOUT_S", 1.0)
    live_outreach._sessions.clear()
    yield s, app
    live_outreach._sessions.clear()
    s.close()


def use_page(monkeypatch, page):
    monkeypatch.setattr(FakeBrowser, "page", page)


def no_pause():
    return Pacer(PacingConfig(enabled=False))


def open_lead(mode="supervised"):
    return asyncio.run(next_lead(H, CAMPAIGN, mode, pacer=no_pause()))


# ---------------------------------------------------------------- countdown


class CountdownPage:
    def __init__(self, states):
        self.states = list(states)
        self.ticks = []

    async def evaluate(self, js, *args):
        if "__snmLive ||" in js:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return json.dumps(state)
        if "-label" in js and args:
            self.ticks.append(args[0][2])
        return None


async def _no_sleep(_):
    return None


class TestCountdown:
    def test_runs_out(self):
        page = CountdownPage([{"decision": None, "paused": False}])
        out = asyncio.run(countdown(page, "Ann", 2, tick=0.5, sleep=_no_sleep))
        assert out == "timeout"
        assert page.ticks[-1] == 0

    def test_skip_and_send_now(self):
        for decision in ("skip", "send"):
            page = CountdownPage([{"decision": decision, "paused": False}])
            assert asyncio.run(countdown(page, "Ann", 30, sleep=_no_sleep)) == decision

    def test_pause_holds_the_clock(self):
        paused = {"decision": None, "paused": True}
        page = CountdownPage([paused] * 10 + [{"decision": "skip", "paused": False}])
        out = asyncio.run(countdown(page, "Ann", 1, tick=0.5, sleep=_no_sleep))
        # Ten paused ticks would have run a 1s clock out five times over.
        assert out == "skip"


# ---------------------------------------------------------------- next_lead


class TestNextLead:
    def test_opens_the_row_and_leaves_panel_open(self, env, monkeypatch):
        page = FakeLivePage(panel(), ["OTHER", PID])
        use_page(monkeypatch, page)
        out = open_lead()
        assert out["status"] == "ready"
        assert out["member_id"] == MEMBER_ID
        assert out["lead"]["posts"][0]["text"].startswith("I am pleased")
        # Contact info is stored, not sent to the drafter.
        assert "contactInfo" not in out["lead"]
        # The offer and drafting pointer come with the first lead only.
        assert "outreach-drafting" in out["briefing"]["drafting"]
        assert page.clicks == [f"row-message:{PID}"]  # opened, not closed
        assert page.panel_open

    def test_not_open_profile_is_skipped_and_recorded(self, env, monkeypatch):
        store, _ = env
        page = FakeLivePage(panel({"memberBadges": {"openLink": False}}), [PID])
        use_page(monkeypatch, page)
        out = open_lead()
        assert out["status"] == "done"
        assert out["skipped_so_far"] == {"not Open Profile": 1}
        assert store.outreach_row(MEMBER_ID, CAMPAIGN)["status"] == "skipped"
        assert not page.panel_open

    def test_sending_disabled_blocks_live_modes_not_draft(self, env, monkeypatch):
        _, app = env
        app.outreach.enabled = False
        use_page(monkeypatch, FakeLivePage(panel(), [PID]))
        assert open_lead("supervised")["error"] == "sending_disabled"
        assert open_lead("draft")["status"] == "ready"


# ------------------------------------------------------------------- submit


GOOD = {
    "subject": "Your recruiting post",
    "body": "Saw your post about recruiting. What part is still manual?",
    "evidence_used": ["posts[0].text"],
}


def do_submit(**overrides):
    args = {**GOOD, **overrides}
    return asyncio.run(
        submit(
            H,
            CAMPAIGN,
            args["subject"],
            args["body"],
            args["evidence_used"],
            countdown_seconds=0,
        )
    )


class TestSubmit:
    def test_ungrounded_draft_is_rejected_before_filling(self, env, monkeypatch):
        page = FakeLivePage(panel(), [PID])
        use_page(monkeypatch, page)
        open_lead()
        out = do_submit(evidence_used=["conferenceTalk"])
        assert out["status"] == "rejected"
        assert page.values == {}
        assert "Send" not in page.clicks

    def test_sends_what_the_panel_holds_at_zero(self, env, monkeypatch):
        store, _ = env
        edited = "Saw your post about recruiting. Which step is still manual?"
        page = FakeLivePage(panel(), [PID], edit={'textarea[name="message"]': edited})
        use_page(monkeypatch, page)
        open_lead()
        out = do_submit()
        assert out["status"] == "sent"
        assert out["edited_during_countdown"] is True
        row = store.outreach_row(MEMBER_ID, CAMPAIGN)
        assert row["status"] == "sent"
        assert row["body"] == edited
        assert page.clicks.count("Send") == 1

    def test_reviewer_skip_never_sends(self, env, monkeypatch):
        store, _ = env
        page = FakeLivePage(panel(), [PID], decision="skip")
        use_page(monkeypatch, page)
        open_lead()
        out = do_submit()
        assert out["status"] == "skipped"
        assert "Send" not in page.clicks
        assert store.outreach_row(MEMBER_ID, CAMPAIGN)["status"] == "skipped"
        assert store.already_contacted(MEMBER_ID) is None

    def test_draft_mode_saves_and_closes(self, env, monkeypatch):
        store, _ = env
        page = FakeLivePage(panel(), [PID])
        use_page(monkeypatch, page)
        open_lead("draft")
        out = do_submit()
        assert out["status"] == "drafted"
        assert "Send" not in page.clicks
        assert not page.panel_open
        row = store.outreach_row(MEMBER_ID, CAMPAIGN)
        assert (row["status"], row["subject"]) == ("drafted", GOOD["subject"])

    def test_no_open_lead(self, env):
        assert do_submit()["error"] == "no_open_lead"


def test_fixture_payloads_are_shared():
    # Guard: these tests lean on the capture fixtures staying realistic.
    assert INSIGHTS_OBSERVED["elements"][0]["insights"]
    assert CONTACT_INFO["emails"]

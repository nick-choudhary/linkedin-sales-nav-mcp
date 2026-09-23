"""Tests for compose capture: reading what the message panel loads.

Two things matter most. The panel must only ever be *closed* -- no path here
may click Send. And a failed capture must never erase what a good one found.
"""

import asyncio
import csv
import json

import pytest
from test_logic import REAL_LEAD

from sales_nav_mcp.compose_capture import (
    capture_lead,
    capture_leads,
    parse_contact_info,
    parse_insights,
    thread_message_count,
)
from sales_nav_mcp.config import AppConfig, OutreachConfig, PacingConfig
from sales_nav_mcp.enrich import EnrichDepthDisabled
from sales_nav_mcp.export import export_query
from sales_nav_mcp.normalize import normalize_person
from sales_nav_mcp.pacing import Pacer
from sales_nav_mcp.store import Store, query_hash

PEOPLE_URL = "https://www.linkedin.com/sales/search/people?query=(filters:List())"
MEMBER_ID = 100000001
PID = "ACwAAAA1B2C3"

# The shape observed live (2026-09-22) for a lead whose panel showed one post.
INSIGHTS_OBSERVED = {
    "metadata": {"paginationToken": "x"},
    "elements": [
        {
            "company": "urn:li:fs_salesCompany:-1",
            "companyResolutionError": {"message": "Call to downstream service failed."},
            "insights": [
                {
                    "details": {
                        "share": "urn:li:share:7488964006121074688",
                        "commentary": "I am pleased to share that we are recruiting.",
                        "content": {"title": " "},
                    },
                    "insightType": "LEAD_SHARE_CONTENT_INSIGHT",
                    "$anti_abuse_metadata": {"$anti_abuse_uuid": "u"},
                }
            ],
        }
    ],
    "paging": {},
}

# A populated contactInfo has not been observed yet; this is the plausible
# shape the parser must cope with, plus an unfamiliar key it must keep.
CONTACT_INFO = {
    "emails": [{"emailAddress": "jane@acme.io", "type": "WORK"}],
    "websites": [
        {"url": "https://acme.io", "category": "COMPANY"},
        {"url": "https://jane.blog", "category": "BLOG"},
    ],
    "phoneNumbers": [{"number": "+1 555 0100", "type": "MOBILE"}],
    "twitterHandles": ["janeacme"],
    "somethingNew": [{"value": "xyz"}],
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = Store(tmp_path / "test.db")
    s.upsert_query(PEOPLE_URL, "contacts")
    s.add_records(query_hash(PEOPLE_URL), "contacts", [normalize_person(REAL_LEAD)])
    monkeypatch.setattr("sales_nav_mcp.compose_capture.get_store", lambda: s)
    yield s
    s.close()


@pytest.fixture
def cfg(monkeypatch):
    app = AppConfig()
    app.outreach = OutreachConfig(enable_enrich=True)
    monkeypatch.setattr("sales_nav_mcp.compose_capture.get_config", lambda: app)
    return app


def good_capture(**extra):
    base = {
        "member_id": MEMBER_ID,
        "profile_id": PID,
        "http_status": 200,
        "member_badges": {"openLink": True, "premium": True},
        "inmail_restriction": "NO_RESTRICTION",
        "contact_info": CONTACT_INFO,
        "contact_items": parse_contact_info(CONTACT_INFO),
        "insights": parse_insights(INSIGHTS_OBSERVED)[0],
        "insights_company": None,
        "thread_messages": 0,
        "raw": {"memberBadges": {"openLink": True}},
    }
    base.update(extra)
    return base


# ------------------------------------------------------------------ parsers


class TestParseContactInfo:
    def test_empty_and_missing(self):
        assert parse_contact_info({}) == []
        assert parse_contact_info(None) == []

    def test_every_kind_is_kept(self):
        rows = parse_contact_info(CONTACT_INFO)
        kinds = {(r["kind"], r["value"], r["label"]) for r in rows}
        assert ("email", "jane@acme.io", "WORK") in kinds
        assert ("website", "https://acme.io", "COMPANY") in kinds
        assert ("website", "https://jane.blog", "BLOG") in kinds
        assert ("phone", "+1 555 0100", "MOBILE") in kinds
        assert ("social", "janeacme", None) in kinds
        # Unknown keys are kept as "other", not dropped.
        assert ("other", "xyz", None) in kinds

    def test_duplicates_collapse(self):
        rows = parse_contact_info(
            {"emails": ["a@b.co", {"emailAddress": "A@b.co"}], "primaryEmail": "a@b.co"}
        )
        assert [r["value"] for r in rows] == ["a@b.co"]


class TestParseInsights:
    def test_observed_shape(self):
        insights, company = parse_insights(INSIGHTS_OBSERVED)
        assert company is None  # companyResolutionError, not a name
        assert len(insights) == 1
        ins = insights[0]
        assert ins["insight_type"] == "LEAD_SHARE_CONTENT_INSIGHT"
        assert ins["commentary"].startswith("I am pleased")
        assert ins["share_urn"] == "urn:li:share:7488964006121074688"
        assert ins["content_title"] is None  # whitespace-only title

    def test_job_change_and_shared(self):
        payload = {
            "elements": [
                {
                    "companyResolutionResult": {"name": "Acme"},
                    "insights": [
                        {
                            "insightType": "LEAD_POSITION_CHANGE_INSIGHT",
                            "details": {
                                "positionChangeTitle": "VP Sales",
                                "positionChangeCompanyResolutionResult": {
                                    "name": "Acme"
                                },
                            },
                        },
                        {
                            "insightType": "SHARED_CONNECTIONS",
                            "details": {
                                "totalCount": 3,
                                "sharedConnections": [
                                    {"firstName": "Ann", "lastName": "Lee"}
                                ],
                            },
                        },
                    ],
                }
            ]
        }
        insights, company = parse_insights(payload)
        assert company == "Acme"
        assert insights[0]["position_change_title"] == "VP Sales"
        assert insights[0]["position_change_company"] == "Acme"
        assert insights[1]["total_count"] == 3
        assert insights[1]["shared_names"] == ["Ann Lee"]

    def test_shared_connections_as_urn_strings(self):
        # Observed live: the list holds URNs, names sit in a sibling map.
        urn = "urn:li:fs_salesProfile:(ACwAAAZZZ,NAME_SEARCH,q)"
        payload = {
            "elements": [
                {
                    "insights": [
                        {
                            "insightType": "SHARED_CONNECTIONS",
                            "details": {
                                "totalCount": 2,
                                "sharedConnections": [urn, "urn:unresolved"],
                                "sharedConnectionsResolutionResults": {
                                    urn: {"firstName": "Bo", "lastName": "Kim"}
                                },
                            },
                        }
                    ]
                }
            ]
        }
        insights, _ = parse_insights(payload)
        assert insights[0]["shared_names"] == ["Bo Kim"]
        assert insights[0]["total_count"] == 2

    def test_garbage(self):
        assert parse_insights(None) == ([], None)
        assert parse_insights({"elements": ["x"]}) == ([], None)


def test_thread_message_count():
    assert thread_message_count({"elements": []}) == 0
    assert thread_message_count({"elements": [{"totalMessageCount": 4}]}) == 4
    assert thread_message_count(None) is None
    # The live response is normalized, like the inbox feed.
    assert thread_message_count({"data": {"elements": []}, "included": []}) == 0
    assert thread_message_count({"data": {"elements": [{"totalMessageCount": 2}]}}) == 2


# -------------------------------------------------------------------- store


class TestStore:
    def test_pending_until_captured(self, store):
        h = query_hash(PEOPLE_URL)
        pending = store.pending_capture(h)
        assert [p["member_id"] for p in pending] == [MEMBER_ID]
        assert pending[0]["profile_id"] == PID
        store.record_capture(good_capture())
        assert store.pending_capture(h) == []

    def test_fast_fetch_alone_still_pending(self, store):
        store.upsert_enrichment(
            [
                {
                    "member_id": MEMBER_ID,
                    "http_status": 200,
                    "member_badges": {"openLink": True},
                }
            ]
        )
        assert len(store.pending_capture(query_hash(PEOPLE_URL))) == 1

    def test_map_carries_contacts_and_insights(self, store):
        store.record_capture(good_capture())
        block = store.enrichment_map(query_hash(PEOPLE_URL))[MEMBER_ID]
        assert block["openProfile"] is True
        assert {"kind": "email", "value": "jane@acme.io", "label": "WORK"} in block[
            "contactInfo"
        ]
        assert block["insights"][0]["type"] == "LEAD_SHARE_CONTENT_INSIGHT"
        stats = store.enrichment_stats(query_hash(PEOPLE_URL))
        assert stats["captured"] == 1
        assert stats["with_contact_info"] == 1

    def test_failed_capture_keeps_previous_data(self, store):
        store.record_capture(good_capture())
        store.record_capture(
            {
                "member_id": MEMBER_ID,
                "profile_id": PID,
                "http_status": None,
                "error": "message panel profile response not seen",
            }
        )
        block = store.enrichment_map(query_hash(PEOPLE_URL))[MEMBER_ID]
        assert block["openProfile"] is True
        assert len(block["contactInfo"]) == 6
        assert block["insights"]

    def test_failed_capture_leaves_fast_fetch_row_alone(self, store):
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
        store.record_capture({**TARGET, "http_status": 200, "error": "boom"})
        assert store.enrichment_stats(h)["succeeded"] == 1
        assert store.pending_enrichment(h) == []

    def test_recapture_replaces_children(self, store):
        store.record_capture(good_capture())
        store.record_capture(good_capture(contact_items=[], insights=[]))
        block = store.enrichment_map(query_hash(PEOPLE_URL))[MEMBER_ID]
        assert block["contactInfo"] == []
        assert block["insights"] == []

    def test_csv_columns(self, store, tmp_path, monkeypatch):
        store.record_capture(good_capture(thread_messages=2))
        app = AppConfig()
        app.storage.output_dir = tmp_path / "out"
        monkeypatch.setattr("sales_nav_mcp.export.get_config", lambda: app)
        query = store.get_query(query_hash(PEOPLE_URL))
        files = export_query(store, query, "csv")["files"]
        with open(files["csv"], encoding="utf-8") as f:
            row = next(csv.DictReader(f))
        assert row["emails"] == "jane@acme.io (WORK)"
        assert "https://jane.blog (BLOG)" in row["websites"]
        assert row["phones"] == "+1 555 0100 (MOBILE)"
        assert row["latest_post"].startswith("I am pleased")
        assert row["existing_thread_messages"] == "2"
        assert row["captured_at"]


# ------------------------------------------------------------ browser flow


class FakeResponse:
    def __init__(self, url, status, body):
        self.url, self.status, self._body = url, status, body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeButton:
    def __init__(self, page, label):
        self.page, self.label = page, label

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.page.panel_open else 0

    async def inner_text(self, timeout=None):
        # The close control's hidden text names the lead (observed live).
        return "Close conversation with Jordan Rivera"

    async def click(self, timeout=None):
        self.page.clicks.append(self.label)
        opener = self.label == "Message" or self.label.startswith("row-message:")
        if opener and self.page.dead_clicks:
            # A click that lands before the page is ready: nothing opens.
            self.page.dead_clicks -= 1
            return
        if opener:
            self.page.panel_open = True
            for resp in self.page.panel_responses:
                for cb in list(self.page.listeners):
                    await cb(resp)
        elif self.label.startswith("close"):
            self.page.panel_open = False


class FakePage:
    def __init__(self, panel_responses, *, has_message=True):
        self.panel_responses = panel_responses
        self.has_message = has_message
        self.listeners = []
        self.clicks = []
        self.urls = []
        self.panel_open = False
        self.dead_clicks = 0

    def on(self, event, cb):
        assert event == "response"
        self.listeners.append(cb)

    def remove_listener(self, event, cb):
        self.listeners.remove(cb)

    async def goto(self, url, wait_until=None):
        self.urls.append(url)

    def get_by_role(self, role, name=None):
        if not self.has_message:
            raise RuntimeError("no Message button")
        return FakeButton(self, name)

    def locator(self, selector):
        # The only selector this module may use is the close control.
        assert selector == 'button:has-text("Close conversation")'
        return FakeButton(self, "close:" + selector)

    async def evaluate(self, js, *args):
        return json.dumps({"free": True, "credits_remaining": None})


def panel(profile_body=None):
    profile = profile_body or {
        "memberBadges": {"openLink": True, "premium": True},
        "inmailRestriction": "NO_RESTRICTION",
        "contactInfo": CONTACT_INFO,
    }
    return [
        FakeResponse(
            "https://www.linkedin.com/sales-api/salesApiProfiles/(profileId:"
            f"{PID},authType:NAME_SEARCH,authToken:ab12)?decoration=%28contactInfo%2CmemberBadges%29",
            200,
            profile,
        ),
        FakeResponse(
            "https://www.linkedin.com/sales-api/salesApiInsights?q=findInsightsByMember"
            f"&page=INBOX&member={PID}&decoration=x",
            200,
            INSIGHTS_OBSERVED,
        ),
        FakeResponse(
            "https://www.linkedin.com/sales-api/salesApiMessagingThreads?recipients=List("
            f"urn%3Ali%3Afs_salesProfile%3A%28{PID}%2CNAME_SEARCH%2Cab12%29)&q=recipients",
            200,
            {"elements": []},
        ),
        # Someone else's profile call on the same page must be ignored.
        FakeResponse(
            "https://www.linkedin.com/sales-api/salesApiProfiles/(profileId:OTHER,"
            "authType:NAME_SEARCH,authToken:z)?decoration=%28contactInfo%29",
            200,
            {"memberBadges": {"openLink": False}},
        ),
    ]


TARGET = {
    "member_id": MEMBER_ID,
    "profile_id": PID,
    "auth_type": "NAME_SEARCH",
    "auth_token": "ab12",
}


class TestCaptureLead:
    def test_reads_panel_and_only_closes(self):
        page = FakePage(panel())
        result = asyncio.run(capture_lead(page, TARGET, timeout=1.0))
        assert result.get("error") is None
        assert result["member_badges"]["openLink"] is True
        assert {r["kind"] for r in result["contact_items"]} >= {
            "email",
            "website",
            "phone",
        }
        assert result["insights"][0]["insight_type"] == "LEAD_SHARE_CONTENT_INSIGHT"
        assert result["thread_messages"] == 0
        assert page.urls == [
            f"https://www.linkedin.com/sales/lead/{PID},NAME_SEARCH,ab12"
        ]
        # Message to open, the close control to close. Never Send.
        assert page.clicks[0] == "Message"
        assert page.clicks[1].startswith("close:")
        assert not any("send" in c.lower() for c in page.clicks)
        assert page.listeners == []

    def test_dead_first_click_is_retried_once(self, monkeypatch):
        monkeypatch.setattr("sales_nav_mcp.compose_capture._PANEL_APPEAR_S", 0.3)
        page = FakePage(panel())
        page.dead_clicks = 1
        result = asyncio.run(capture_lead(page, TARGET, timeout=1.0))
        assert result.get("error") is None
        assert page.clicks[:2] == ["Message", "Message"]
        assert result["panel_status"]["panel_retry"] == 1

    def test_panel_that_never_opens_fails_cleanly(self, monkeypatch):
        monkeypatch.setattr("sales_nav_mcp.compose_capture._PANEL_APPEAR_S", 0.3)
        page = FakePage(panel())
        page.dead_clicks = 5
        result = asyncio.run(capture_lead(page, TARGET, timeout=0.5))
        assert "did not appear" in result["error"]
        assert page.clicks.count("Message") == 2
        assert page.listeners == []

    def test_missing_profile_response_is_a_failure(self):
        page = FakePage(panel()[1:3])
        result = asyncio.run(capture_lead(page, TARGET, timeout=0.5))
        assert result["error"] == "message panel profile response not seen"

    def test_no_message_button_is_a_failure_not_a_crash(self):
        page = FakePage(panel(), has_message=False)
        result = asyncio.run(capture_lead(page, TARGET, timeout=0.5))
        assert result["error"].startswith("panel did not open")
        assert page.listeners == []

    def test_badge_without_openlink_is_not_accepted(self):
        page = FakePage(panel({"memberBadges": {}, "contactInfo": {}}))
        result = asyncio.run(capture_lead(page, TARGET, timeout=0.5))
        assert result["error"] == (
            "profile seen without memberBadges.openLink (panel call missing)"
        )

    def test_lead_page_profile_then_panel_profile(self):
        # Observed live: the lead page's own profile call carries contactInfo
        # but no memberBadges, and it lands first. The panel's call must win,
        # and contactInfo from the first must not be lost.
        lead_page = FakeResponse(
            f"https://www.linkedin.com/sales-api/salesApiProfiles/(profileId:{PID},"
            "authType:NAME_SEARCH,authToken:ab12)?decoration=%28contactInfo%2Csummary%29",
            200,
            {"contactInfo": CONTACT_INFO, "summary": "x"},
        )
        panel_profile = {"memberBadges": {"openLink": False}, "inmailRestriction": "X"}
        page = FakePage([lead_page, *panel(panel_profile)])
        result = asyncio.run(capture_lead(page, TARGET, timeout=1.0))
        assert result.get("error") is None
        assert result["member_badges"] == {"openLink": False}
        assert any(r["kind"] == "email" for r in result["contact_items"])


class FakeBrowser:
    page = None

    def __init__(self):
        self.lock = asyncio.Lock()

    async def get_page(self):
        return FakeBrowser.page or object()


class FakeAttr:
    def __init__(self, href):
        self.href = href

    @property
    def first(self):
        return self

    async def get_attribute(self, name, timeout=None):
        assert name == "href"
        return self.href


class FakeRow:
    def __init__(self, page, pid):
        self.page, self.pid = page, pid

    async def scroll_into_view_if_needed(self, timeout=None):
        self.page.scrolled.append(self.pid)

    def locator(self, selector):
        if selector == 'a[href*="/sales/lead/"]':
            return FakeAttr(f"/sales/lead/{self.pid},NAME_SEARCH,ab12?_ntb=x")
        assert selector == "button[data-anchor-send-message]"
        return FakeButton(self.page, f"row-message:{self.pid}")


class FakeRows:
    def __init__(self, rows):
        self.rows = rows

    @property
    def first(self):
        rows = self.rows

        class _First:
            async def wait_for(self, state=None, timeout=None):
                if not rows:
                    raise TimeoutError("no rows")

        return _First()

    async def count(self):
        return len(self.rows)

    def nth(self, i):
        return self.rows[i]


class FakeSearchPage(FakePage):
    """A results page: rows by profileId, each with its own Message button."""

    def __init__(self, panel_responses, row_pids):
        super().__init__(panel_responses)
        self.row_pids = row_pids
        self.scrolled = []

    def get_by_role(self, role, name=None):
        raise AssertionError("search capture must not use the lead page button")

    def locator(self, selector):
        if selector == "#search-results-container li.artdeco-list__item":
            return FakeRows([FakeRow(self, pid) for pid in self.row_pids])
        return super().locator(selector)


def dossier_response():
    return FakeResponse(
        "https://www.linkedin.com/sales-api/salesApiAccountDossier/14815603"
        "?accountIQUseCase=SALES_NAVIGATOR",
        200,
        {"strategicPriorities": ["AI-First CRM Strategy"]},
    )


class TestCaptureLeads:
    def test_search_row_is_used_not_the_lead_page(self, store, cfg, monkeypatch):
        monkeypatch.setattr("sales_nav_mcp.compose_capture.get_browser", FakeBrowser)
        page = FakeSearchPage([*panel(), dossier_response()], ["OTHER", PID])
        monkeypatch.setattr(FakeBrowser, "page", page)

        async def must_not_run(page, target):
            raise AssertionError("fallback used although the row was found")

        out = asyncio.run(
            capture_leads(
                query_hash(PEOPLE_URL),
                capture=must_not_run,
                pacer=Pacer(PacingConfig(enabled=False)),
            )
        )
        assert out["captured_this_call"] == 1
        assert out["opened_from_search_row"] == 1
        assert out["opened_from_lead_page"] == 0
        # Page 1 of the stored search, never a lead page.
        assert page.urls == [PEOPLE_URL]
        # Only the pending lead's row was opened; the other row was skipped.
        assert page.clicks[0] == f"row-message:{PID}"
        assert page.clicks[1].startswith("close:")
        assert not any("send" in c.lower() for c in page.clicks)
        assert store.company_dossier(14815603)["raw"] == {
            "strategicPriorities": ["AI-First CRM Strategy"]
        }

    def test_lead_off_its_page_falls_back(self, store, cfg, monkeypatch):
        monkeypatch.setattr("sales_nav_mcp.compose_capture.get_browser", FakeBrowser)
        monkeypatch.setattr(FakeBrowser, "page", FakeSearchPage(panel(), ["OTHER"]))
        used = []

        async def fallback(page, target):
            used.append(target["member_id"])
            return good_capture()

        out = asyncio.run(
            capture_leads(
                query_hash(PEOPLE_URL),
                capture=fallback,
                pacer=Pacer(PacingConfig(enabled=False)),
            )
        )
        assert used == [MEMBER_ID]
        assert out["opened_from_lead_page"] == 1
        assert out["captured_this_call"] == 1

    def test_runs_stores_and_counts(self, store, cfg, monkeypatch):
        monkeypatch.setattr("sales_nav_mcp.compose_capture.get_browser", FakeBrowser)

        async def fake_capture(page, target):
            return good_capture()

        out = asyncio.run(
            capture_leads(
                query_hash(PEOPLE_URL),
                via="lead_page",
                capture=fake_capture,
                pacer=Pacer(PacingConfig(enabled=False)),
            )
        )
        assert out["captured_this_call"] == 1
        assert out["with_contact_info_this_call"] == 1
        assert out["remaining"] == 0
        assert out["open_profiles"] == 1
        again = asyncio.run(capture_leads(query_hash(PEOPLE_URL), capture=fake_capture))
        assert again["nothing_pending"] is True

    def test_failure_stays_pending(self, store, cfg, monkeypatch):
        monkeypatch.setattr("sales_nav_mcp.compose_capture.get_browser", FakeBrowser)

        async def failing(page, target):
            return {**TARGET, "http_status": None, "error": "panel did not open: x"}

        out = asyncio.run(
            capture_leads(
                query_hash(PEOPLE_URL),
                via="lead_page",
                capture=failing,
                pacer=Pacer(PacingConfig(enabled=False)),
            )
        )
        assert out["failed_this_call"] == 1
        assert out["remaining"] == 1

    def test_gate(self, store, monkeypatch):
        app = AppConfig()
        app.outreach = OutreachConfig(enable_enrich=False)
        monkeypatch.setattr("sales_nav_mcp.compose_capture.get_config", lambda: app)
        with pytest.raises(EnrichDepthDisabled):
            asyncio.run(capture_leads(query_hash(PEOPLE_URL)))

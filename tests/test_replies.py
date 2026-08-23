"""Tests for reply capture.

Fixtures mirror the REAL `salesApiMessagingThreads` shape, captured from a live
inbox — which is not the shape the decoration implies. `participants` is a list
of profile URNs, `participantsResolutionResults` maps `*<urn>` to the same
`<urn>` rather than to a profile, and `included` comes back empty. So there is
no `objectUrn` and no member_id anywhere in the payload, and matching has to go
through the profileId embedded in the URN.

An earlier version of these tests invented resolved profiles carrying
`objectUrn`. They passed, and the code failed on the first live call.
"""

import pytest
from test_logic import REAL_LEAD

from sales_nav_mcp.normalize import normalize_person
from sales_nav_mcp.replies import parse_threads, profile_id_of
from sales_nav_mcp.store import Store, query_hash

PEOPLE_URL = "https://www.linkedin.com/sales/search/people?query=(filters:List())"
MEMBER_ID = 100000001

# REAL_LEAD's entityUrn is (ACwAAAA1B2C3,NAME_SEARCH,ab12)
LEAD_PID = "ACwAAAA1B2C3"
LEAD = f"urn:li:fs_salesProfile:({LEAD_PID},NAME_SEARCH,ab12)"
# same person seen via a different search: different token, same profileId
LEAD_OTHER_TOKEN = f"urn:li:fs_salesProfile:({LEAD_PID},NAME_SEARCH,zz99)"
VIEWER_PID = "ACwAABLOV80"
VIEWER = f"urn:li:fs_salesProfile:({VIEWER_PID},NAME_SEARCH,rIk9)"


def thread(messages, *, participants=None, thread_id="t1", unread=0):
    parts = participants if participants is not None else [VIEWER, LEAD]
    return {
        "id": thread_id,
        "archived": False,
        "unreadMessageCount": unread,
        "totalMessageCount": len(messages),
        "participants": parts,
        # exactly as LinkedIn returns it: a reference, not a resolved profile
        "participantsResolutionResults": {f"*{p}": p for p in parts},
        "messages": messages,
    }


def payload(*threads):
    return {"data": {"elements": list(threads)}}


def msg(author, at, body="hi"):
    return {"author": author, "deliveredAt": at, "body": body}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.upsert_query(PEOPLE_URL, "contacts")
    s.add_records(query_hash(PEOPLE_URL), "contacts", [normalize_person(REAL_LEAD)])
    yield s
    s.close()


class TestProfileIdOf:
    def test_extracts_first_component(self):
        assert profile_id_of(LEAD) == LEAD_PID

    def test_ignores_the_search_scoped_token(self):
        assert profile_id_of(LEAD) == profile_id_of(LEAD_OTHER_TOKEN)

    def test_none_and_garbage(self):
        assert profile_id_of(None) is None
        assert profile_id_of("not-a-urn") is None


class TestParseThreads:
    def test_our_own_message_is_not_a_reply(self):
        """The live thread had exactly this shape: one message authored by the
        viewer. It must not register as a reply."""
        assert parse_threads(payload(thread([msg(VIEWER, 1000)])), VIEWER) == {}

    def test_lead_message_is_a_reply(self):
        p = payload(thread([msg(VIEWER, 1000), msg(LEAD, 2000, "tell me more")]))
        found = parse_threads(p, VIEWER)
        assert set(found) == {LEAD_PID}
        assert found[LEAD_PID]["delivered_at"] == 2000
        assert found[LEAD_PID]["preview"] == "tell me more"

    def test_latest_reply_wins(self):
        p = payload(thread([msg(LEAD, 2000, "first"), msg(LEAD, 5000, "second")]))
        assert parse_threads(p, VIEWER)[LEAD_PID]["preview"] == "second"

    def test_matches_across_a_different_search_token(self):
        p = payload(
            thread(
                [msg(LEAD_OTHER_TOKEN, 3000, "yes")],
                participants=[VIEWER, LEAD_OTHER_TOKEN],
            )
        )
        assert set(parse_threads(p, VIEWER)) == {LEAD_PID}

    def test_unresolved_viewer_classifies_nothing(self):
        """Without a viewer the dangerous failure is reading our OWN outbound
        message as the lead's reply, so refuse rather than guess."""
        p = payload(thread([msg(VIEWER, 1000), msg(LEAD, 2000, "interested")]))
        assert parse_threads(p, None) == {}
        assert set(parse_threads(p, VIEWER)) == {LEAD_PID}

    def test_empty_body_reply_still_counts(self):
        """A deleted message renders with an empty body — observed live."""
        found = parse_threads(payload(thread([msg(LEAD, 3000, "")])), VIEWER)
        assert found[LEAD_PID]["preview"] is None
        assert found[LEAD_PID]["delivered_at"] == 3000

    def test_dict_body_is_unwrapped(self):
        p = payload(
            thread([{"author": LEAD, "deliveredAt": 1, "body": {"text": "yo"}}])
        )
        assert parse_threads(p, VIEWER)[LEAD_PID]["preview"] == "yo"

    def test_message_from_a_non_participant_is_ignored(self):
        stranger = "urn:li:fs_salesProfile:(ACwAASTRANGER,NAME_SEARCH,xx11)"
        assert parse_threads(payload(thread([msg(stranger, 1)])), VIEWER) == {}

    def test_empty_payloads(self):
        assert parse_threads({}, VIEWER) == {}
        assert parse_threads({"data": {"elements": []}}, VIEWER) == {}


class TestProfileIdResolution:
    def test_resolves_via_leads(self, store):
        assert store.member_id_for_profile_id(LEAD_PID) == MEMBER_ID

    def test_unknown_profile_id(self, store):
        assert store.member_id_for_profile_id("ACwAANOBODY") is None

    def test_empty_profile_id(self, store):
        assert store.member_id_for_profile_id("") is None

    def test_resolves_via_outreach_when_not_a_scraped_lead(self, store):
        store.record_outreach(
            777, "c1", "sent", entity_urn="urn:li:fs_salesProfile:(ACwAAOTHER,X,t)"
        )
        assert store.member_id_for_profile_id("ACwAAOTHER") == 777


class TestRepliedState:
    def test_replied_counts_as_contacted(self, store):
        store.record_outreach(MEMBER_ID, "c1", "sent")
        store.record_outreach(MEMBER_ID, "c1", "replied", replied_at=1234.0)
        assert store.already_contacted(MEMBER_ID) == "c1"

    def test_replied_lead_is_not_offered_again(self, store):
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
        store.record_outreach(MEMBER_ID, "c1", "replied")
        assert store.outreach_candidates(h, "c2") == []

    def test_replied_at_is_stored_and_preserved(self, store):
        store.record_outreach(MEMBER_ID, "c1", "sent", channel="open_profile")
        store.record_outreach(MEMBER_ID, "c1", "replied", replied_at=555.0)
        assert store.outreach_row(MEMBER_ID, "c1")["replied_at"] == 555.0
        store.record_outreach(MEMBER_ID, "c1", "replied")
        assert store.outreach_row(MEMBER_ID, "c1")["replied_at"] == 555.0

    def test_stats_report_replied(self, store):
        store.record_outreach(MEMBER_ID, "c1", "replied")
        assert store.outreach_stats("c1")["by_status"] == {"replied": 1}

    def test_unknown_status_still_rejected(self, store):
        with pytest.raises(ValueError):
            store.record_outreach(MEMBER_ID, "c1", "answered")


class TestLookup:
    def test_finds_row_without_knowing_the_campaign(self, store):
        store.record_outreach(MEMBER_ID, "some-campaign", "sent")
        row = store.outreach_row_any_campaign(MEMBER_ID)
        assert row["campaign"] == "some-campaign"

    def test_campaign_filter_narrows(self, store):
        store.record_outreach(MEMBER_ID, "a", "sent")
        assert store.outreach_row_any_campaign(MEMBER_ID, "b") is None

    def test_missing_member(self, store):
        assert store.outreach_row_any_campaign(424242) is None

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
from sales_nav_mcp.replies import answers_our_send, parse_threads, profile_id_of
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


class TestSendBoundary:
    """Only a delivered message can be replied to, and only afterwards.
    CodeRabbit caught that any outreach row -- queued, failed, skipped -- was
    being marked replied on the strength of an unrelated inbox message."""

    def test_sent_row_with_a_later_message(self):
        assert answers_our_send({"status": "sent", "sent_at": 1000.0}, 2000.0) is True

    def test_message_before_the_send_is_not_a_reply(self):
        """A pre-existing thread often has older inbound messages. Those are not
        answers to a send that happened afterwards."""
        assert answers_our_send({"status": "sent", "sent_at": 5000.0}, 1000.0) is False

    def test_message_at_exactly_the_send_time_is_not_a_reply(self):
        assert answers_our_send({"status": "sent", "sent_at": 1000.0}, 1000.0) is False

    def test_queued_row_never_counts(self):
        assert answers_our_send({"status": "queued"}, 9999.0) is False

    def test_failed_row_never_counts(self):
        assert (
            answers_our_send({"status": "failed", "updated_at": 1.0}, 9999.0) is False
        )

    def test_skipped_row_never_counts(self):
        assert answers_our_send({"status": "skipped"}, 9999.0) is False

    def test_already_replied_is_not_remarked(self):
        assert answers_our_send({"status": "replied", "sent_at": 1.0}, 9999.0) is False

    def test_sending_row_uses_the_attempt_time(self):
        """Delivery unconfirmed, but the attempt happened -- a later inbound
        message plausibly answers it."""
        row = {"status": "sending", "updated_at": 1000.0}
        assert answers_our_send(row, 2000.0) is True
        assert answers_our_send(row, 500.0) is False

    def test_missing_boundary_rejects(self):
        """Without a send timestamp there is nothing to prove the message
        answers our outreach, so it must not be recorded as a reply."""
        assert answers_our_send({"status": "sent", "sent_at": None}, 5.0) is False

    def test_missing_delivery_timestamp_rejects(self):
        """A message with no deliveredAt cannot be placed relative to the send."""
        assert answers_our_send({"status": "sent", "sent_at": 1.0}, 0) is False
        assert answers_our_send({"status": "sent", "sent_at": 1.0}, -5.0) is False

    def test_sending_row_without_updated_at_rejects(self):
        assert answers_our_send({"status": "sending"}, 500.0) is False

    def test_empty_row(self):
        assert answers_our_send({}, 1.0) is False


class TestProfileIdIsNotAWildcard:
    """profileIds are opaque and really do contain `-`; `_` is a
    single-character wildcard in SQL LIKE, so a wildcard match could resolve to
    a DIFFERENT member and mark the wrong person as replied."""

    def _lead_with_urn(self, urn, member_id):
        return {
            **REAL_LEAD,
            "entityUrn": urn,
            "objectUrn": f"urn:li:member:{member_id}",
        }

    def test_underscore_is_literal_not_a_wildcard(self, store):
        h = query_hash(PEOPLE_URL)
        store.add_records(
            h,
            "contacts",
            [
                normalize_person(
                    self._lead_with_urn(
                        "urn:li:fs_salesProfile:(ACwAA_BCD,NAME_SEARCH,t1)", 501
                    )
                ),
                normalize_person(
                    self._lead_with_urn(
                        "urn:li:fs_salesProfile:(ACwAAXBCD,NAME_SEARCH,t2)", 502
                    )
                ),
            ],
        )
        # Under LIKE, "ACwAA_BCD" would also match "ACwAAXBCD".
        assert store.member_id_for_profile_id("ACwAA_BCD") == 501
        assert store.member_id_for_profile_id("ACwAAXBCD") == 502

    def test_percent_is_literal(self, store):
        h = query_hash(PEOPLE_URL)
        store.add_records(
            h,
            "contacts",
            [
                normalize_person(
                    self._lead_with_urn(
                        "urn:li:fs_salesProfile:(ACwAA%ZZ,NAME_SEARCH,t3)", 503
                    )
                )
            ],
        )
        assert store.member_id_for_profile_id("ACwAA%ZZ") == 503
        # a bare % must not match everything
        assert store.member_id_for_profile_id("%") is None

    def test_prefix_does_not_match_a_longer_id(self, store):
        """The trailing comma delimiter pins the match to the whole id."""
        h = query_hash(PEOPLE_URL)
        store.add_records(
            h,
            "contacts",
            [
                normalize_person(
                    self._lead_with_urn(
                        "urn:li:fs_salesProfile:(ACwAALONGER,NAME_SEARCH,t4)", 504
                    )
                )
            ],
        )
        assert store.member_id_for_profile_id("ACwAALONG") is None
        assert store.member_id_for_profile_id("ACwAALONGER") == 504


class TestDailyCapSurvivesReplies:
    """A reply flips the row to `replied`. Counting the cap by status would
    drop that already-sent message, silently buying back headroom."""

    def test_reply_does_not_free_cap_space(self, store):
        import time as _t

        store.record_outreach(MEMBER_ID, "c1", "sent", channel="open_profile")
        assert store.sent_since(_t.time() - 60) == 1
        store.record_outreach(MEMBER_ID, "c1", "replied", replied_at=_t.time())
        assert store.sent_since(_t.time() - 60) == 1

    def test_never_sent_rows_do_not_count(self, store):
        import time as _t

        store.record_outreach(MEMBER_ID, "c1", "queued")
        store.record_outreach(555, "c1", "failed", last_error="x")
        assert store.sent_since(_t.time() - 60) == 0

    def test_window_still_applies(self, store):
        import time as _t

        store.record_outreach(MEMBER_ID, "c1", "sent")
        assert store.sent_since(_t.time() + 60) == 0


class TestMixedCampaignRows:
    """A member can have rows in several campaigns. Picking purely the most
    recent one surfaces a later queued/failed row, which fails the send
    boundary and silently discards a real reply to the campaign that did
    reach them."""

    def test_sent_row_wins_over_a_later_non_sending_row(self, store):
        import time as _t

        store.record_outreach(MEMBER_ID, "c1", "sent", channel="open_profile")
        store.record_outreach(MEMBER_ID, "c2", "queued")
        row = store.outreach_row_any_campaign(MEMBER_ID)
        assert row["campaign"] == "c1"
        assert answers_our_send(row, _t.time() + 10) is True

    def test_failed_later_row_does_not_mask_the_send(self, store):
        store.record_outreach(MEMBER_ID, "c1", "sent")
        store.record_outreach(MEMBER_ID, "c2", "failed", last_error="boom")
        assert store.outreach_row_any_campaign(MEMBER_ID)["campaign"] == "c1"

    def test_campaign_filter_still_wins_when_given(self, store):
        store.record_outreach(MEMBER_ID, "c1", "sent")
        store.record_outreach(MEMBER_ID, "c2", "queued")
        assert store.outreach_row_any_campaign(MEMBER_ID, "c2")["status"] == "queued"

    def test_most_recent_send_wins_between_two_sends(self, store):
        import time as _t

        store.record_outreach(MEMBER_ID, "old", "sent")
        store._conn.execute(
            "UPDATE lead_outreach SET sent_at = ? WHERE campaign = 'old'",
            (_t.time() - 9999,),
        )
        store.record_outreach(MEMBER_ID, "new", "sent")
        assert store.outreach_row_any_campaign(MEMBER_ID)["campaign"] == "new"

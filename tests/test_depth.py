"""Tests for pipeline depth and the depth-3 profile store.

Depth is a property of the query, not of the call that created it, so a run
resumed tomorrow knows what the search was collected for.
"""

import asyncio

import pytest
from test_logic import REAL_LEAD

from sales_nav_mcp.enrich import EnrichDepthDisabled, enrich_leads
from sales_nav_mcp.normalize import normalize_person
from sales_nav_mcp.store import Store, query_hash
from sales_nav_mcp.tools.contacts import _effective_depth

PEOPLE_URL = "https://www.linkedin.com/sales/search/people?query=(filters:List())"
MEMBER_ID = 100000001


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.upsert_query(PEOPLE_URL, "contacts")
    s.add_records(query_hash(PEOPLE_URL), "contacts", [normalize_person(REAL_LEAD)])
    yield s
    s.close()


class TestQueryDepth:
    def test_defaults_to_search(self, store):
        assert store.get_query(query_hash(PEOPLE_URL)).depth == "search"

    def test_persists_across_reads(self, store):
        h = query_hash(PEOPLE_URL)
        store.set_query_depth(h, "open_profile")
        assert store.get_query(h).depth == "open_profile"

    def test_full_depth(self, store):
        h = query_hash(PEOPLE_URL)
        store.set_query_depth(h, "full")
        assert store.get_query(h).depth == "full"

    def test_unknown_depth_rejected(self, store):
        with pytest.raises(ValueError):
            store.set_query_depth(query_hash(PEOPLE_URL), "everything")

    def test_survives_upsert_of_the_same_query(self, store):
        h = query_hash(PEOPLE_URL)
        store.set_query_depth(h, "full")
        store.upsert_query(PEOPLE_URL, "contacts")
        assert store.get_query(h).depth == "full"


class TestPendingProfiles:
    def test_lead_is_pending_until_fetched(self, store):
        h = query_hash(PEOPLE_URL)
        assert len(store.pending_profiles(h)) == 1
        store.upsert_profile(
            MEMBER_ID, profile_id="ACwAAAA1B2C3", http_status=200, raw={"fullName": "X"}
        )
        assert store.pending_profiles(h) == []

    def test_failed_fetch_stays_pending(self, store):
        h = query_hash(PEOPLE_URL)
        store.upsert_profile(MEMBER_ID, profile_id="X", http_status=500, raw=None)
        assert len(store.pending_profiles(h)) == 1

    def test_limit_zero(self, store):
        assert store.pending_profiles(query_hash(PEOPLE_URL), limit=0) == []

    def test_parsed_key_is_returned(self, store):
        row = store.pending_profiles(query_hash(PEOPLE_URL))[0]
        assert row["profile_id"] == "ACwAAAA1B2C3"
        assert row["auth_type"] == "NAME_SEARCH"


class TestProfileStore:
    def test_round_trip(self, store):
        store.upsert_profile(
            MEMBER_ID,
            profile_id="ACwAAAA1B2C3",
            http_status=200,
            raw={"fullName": "Jordan Rivera", "skills": ["SQL"]},
        )
        got = store.get_profile(MEMBER_ID)
        assert got["http_status"] == 200
        assert got["profile"]["fullName"] == "Jordan Rivera"
        assert got["profile"]["skills"] == ["SQL"]

    def test_missing_profile_is_none(self, store):
        assert store.get_profile(999999) is None

    def test_upsert_replaces(self, store):
        for name in ("old", "new"):
            store.upsert_profile(
                MEMBER_ID, profile_id="X", http_status=200, raw={"fullName": name}
            )
        assert store.get_profile(MEMBER_ID)["profile"]["fullName"] == "new"

    def test_profile_fetch_does_not_disturb_search_records(self, store):
        """Depth 3 writes its own table; the captured search element is
        untouched, same invariant as enrichment."""
        h = query_hash(PEOPLE_URL)
        store.upsert_profile(
            MEMBER_ID, profile_id="X", http_status=200, raw={"fullName": "Other"}
        )
        rec = next(iter(store.iter_records(h, include_raw=True)))
        assert rec["_raw"] == REAL_LEAD


class TestDepthIsNotResetOnResume:
    """CodeRabbit caught this: writing the parameter default on every call
    silently downgraded a `full` query back to `search` on the next resume,
    dropping the profile stage without telling anyone."""

    def test_omitted_depth_keeps_the_stored_value(self, store):
        h = query_hash(PEOPLE_URL)
        store.set_query_depth(h, "full")
        assert _effective_depth(None, store.get_query(h)) == "full"

    def test_explicit_depth_wins(self, store):
        h = query_hash(PEOPLE_URL)
        store.set_query_depth(h, "full")
        assert _effective_depth("search", store.get_query(h)) == "search"

    def test_unknown_query_defaults_to_search(self):
        assert _effective_depth(None, None) == "search"

    def test_explicit_depth_on_a_new_query(self):
        assert _effective_depth("open_profile", None) == "open_profile"


class TestEnrichGateHolds:
    """The depth check on search_contacts is early feedback only; enrich_leads
    is callable directly, so the gate has to live there too."""

    def test_disabled_enrichment_raises(self, store, monkeypatch):
        from sales_nav_mcp.config import AppConfig, OutreachConfig

        app = AppConfig()
        app.outreach = OutreachConfig(enable_enrich=False)
        monkeypatch.setattr("sales_nav_mcp.enrich.get_config", lambda: app)
        monkeypatch.setattr("sales_nav_mcp.enrich.get_store", lambda: store)
        with pytest.raises(EnrichDepthDisabled):
            asyncio.run(enrich_leads(query_hash(PEOPLE_URL), limit=1))


class TestProfileStats:
    def test_counts_only_successful_fetches(self, store):
        h = query_hash(PEOPLE_URL)
        assert store.profile_stats(h)["fetched"] == 0
        store.upsert_profile(MEMBER_ID, profile_id="X", http_status=500, raw=None)
        assert store.profile_stats(h)["fetched"] == 0
        store.upsert_profile(MEMBER_ID, profile_id="X", http_status=200, raw={"a": 1})
        assert store.profile_stats(h)["fetched"] == 1

    def test_does_not_count_profiles_from_other_queries(self, store):
        other = "https://www.linkedin.com/sales/search/people?query=(x)"
        store.upsert_query(other, "contacts")
        store.upsert_profile(999999, profile_id="Y", http_status=200, raw={"a": 1})
        assert store.profile_stats(query_hash(other))["fetched"] == 0


class TestReconcileMetadataPreserved:
    """record_outreach overwrites every column, so reconcile has to carry the
    original channel and evidence through or it erases the audit trail for a
    message that was actually delivered."""

    def test_ambiguous_sends_returns_channel_and_decoded_evidence(self, store):
        store.record_outreach(
            MEMBER_ID,
            "c1",
            "sending",
            channel="open_profile",
            subject="s",
            body="b",
            evidence_used=["title", "companyName"],
        )
        row = store.ambiguous_sends()[0]
        assert row["channel"] == "open_profile"
        assert row["evidence_used"] == ["title", "companyName"]

    def test_evidence_is_none_when_absent(self, store):
        store.record_outreach(MEMBER_ID, "c1", "sending")
        assert store.ambiguous_sends()[0]["evidence_used"] is None

    def test_round_trip_back_into_record_outreach(self, store):
        store.record_outreach(
            MEMBER_ID,
            "c1",
            "sending",
            channel="open_profile",
            evidence_used=["title"],
        )
        row = store.ambiguous_sends()[0]
        store.record_outreach(
            MEMBER_ID,
            "c1",
            "sent",
            channel=row["channel"],
            evidence_used=row["evidence_used"],
        )
        final = store.outreach_row(MEMBER_ID, "c1")
        assert final["channel"] == "open_profile"
        assert final["evidence_used"] == '["title"]'


class TestProfileParseFailures:
    """A 200 whose body would not parse stored nothing. Status alone must not
    mark it fetched, or the drafting step gets an empty record that looks real."""

    def test_parse_failure_is_not_fetched(self, store):
        h = query_hash(PEOPLE_URL)
        store.upsert_profile(
            MEMBER_ID, profile_id="X", http_status=200, raw=None, error="bad json"
        )
        assert store.profile_stats(h)["fetched"] == 0
        assert len(store.pending_profiles(h)) == 1

    def test_get_profile_reports_no_payload(self, store):
        store.upsert_profile(
            MEMBER_ID, profile_id="X", http_status=200, raw=None, error="bad json"
        )
        got = store.get_profile(MEMBER_ID)
        assert got is not None
        assert "profile" not in got
        assert got["error"] == "bad json"

    def test_failed_refresh_keeps_the_stored_profile(self, store):
        store.upsert_profile(
            MEMBER_ID, profile_id="X", http_status=200, raw={"fullName": "Jordan"}
        )
        store.upsert_profile(
            MEMBER_ID, profile_id="X", http_status=200, raw=None, error="bad json"
        )
        assert store.get_profile(MEMBER_ID)["profile"]["fullName"] == "Jordan"


class TestUnsuccessfulRefreshNeverErasesAProfile:
    """Round 3 guarded on `error IS NULL`, which a non-200 satisfies -- it sets
    no error string. So a 500 still wiped the stored profile. The guard is now
    on the incoming payload itself."""

    def _seed_good(self, store):
        store.upsert_profile(
            MEMBER_ID, profile_id="X", http_status=200, raw={"fullName": "Jordan"}
        )

    def test_500_refresh_keeps_the_profile(self, store):
        self._seed_good(store)
        store.upsert_profile(MEMBER_ID, profile_id="X", http_status=500, raw=None)
        assert store.get_profile(MEMBER_ID)["profile"]["fullName"] == "Jordan"

    def test_empty_200_payload_keeps_the_profile(self, store):
        self._seed_good(store)
        store.upsert_profile(MEMBER_ID, profile_id="X", http_status=200, raw=None)
        assert store.get_profile(MEMBER_ID)["profile"]["fullName"] == "Jordan"

    def test_failed_refresh_makes_it_pending_again(self, store):
        h = query_hash(PEOPLE_URL)
        self._seed_good(store)
        assert store.pending_profiles(h) == []
        store.upsert_profile(
            MEMBER_ID, profile_id="X", http_status=500, raw=None, error="HTTP 500"
        )
        assert len(store.pending_profiles(h)) == 1

    def test_a_good_refresh_still_replaces(self, store):
        self._seed_good(store)
        store.upsert_profile(
            MEMBER_ID, profile_id="X", http_status=200, raw={"fullName": "Updated"}
        )
        assert store.get_profile(MEMBER_ID)["profile"]["fullName"] == "Updated"

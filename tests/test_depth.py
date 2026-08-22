"""Tests for pipeline depth and the depth-3 profile store.

Depth is a property of the query, not of the call that created it, so a run
resumed tomorrow knows what the search was collected for.
"""

import pytest
from test_logic import REAL_LEAD

from sales_nav_mcp.normalize import normalize_person
from sales_nav_mcp.store import Store, query_hash

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

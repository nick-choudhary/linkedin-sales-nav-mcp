"""Tests for Open Profile enrichment: the side table and the read-path join.

The invariant under test is that enrichment never touches search data. It goes
into `lead_enrichment` keyed on the stable `member_id`, and is merged in only
when records are read, so a re-scrape cannot clobber it and it cannot corrupt
`raw_json`.
"""

import csv
import json
from pathlib import Path

import pytest
from test_logic import REAL_LEAD

from sales_nav_mcp.export import export_query
from sales_nav_mcp.normalize import normalize_person
from sales_nav_mcp.store import Store, _parse_profile_key, query_hash

PEOPLE_URL = "https://www.linkedin.com/sales/search/people?query=(filters:List())"
MEMBER_ID = 100000001  # REAL_LEAD's objectUrn


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def seeded(store):
    store.upsert_query(PEOPLE_URL, "contacts")
    h = query_hash(PEOPLE_URL)
    store.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
    return store, h


class TestParseProfileKey:
    def test_splits_the_three_parts(self):
        assert _parse_profile_key(
            "urn:li:fs_salesProfile:(ACwAAAA1B2C3,NAME_SEARCH,ab12)"
        ) == ("ACwAAAA1B2C3", "NAME_SEARCH", "ab12")

    def test_empty_auth_token_still_parses(self):
        parsed = _parse_profile_key(
            "urn:li:fs_salesProfile:(ACwAAAA1B2C3,NAME_SEARCH,)"
        )
        assert parsed == ("ACwAAAA1B2C3", "NAME_SEARCH", "")

    def test_garbage_returns_none(self):
        assert _parse_profile_key("not-a-urn") is None
        assert _parse_profile_key("") is None


class TestPendingEnrichment:
    def test_lists_lead_with_parsed_key(self, seeded):
        store, h = seeded
        pending = store.pending_enrichment(h)
        assert len(pending) == 1
        assert pending[0]["member_id"] == MEMBER_ID
        assert pending[0]["profile_id"] == "ACwAAAA1B2C3"
        assert pending[0]["auth_type"] == "NAME_SEARCH"
        assert pending[0]["auth_token"] == "ab12"

    def test_successful_fetch_is_skipped_next_time(self, seeded):
        store, h = seeded
        store.upsert_enrichment(
            [{"member_id": MEMBER_ID, "http_status": 200, "member_badges": {}}]
        )
        assert store.pending_enrichment(h) == []

    def test_failed_fetch_stays_pending(self, seeded):
        store, h = seeded
        store.upsert_enrichment([{"member_id": MEMBER_ID, "http_status": 400}])
        assert len(store.pending_enrichment(h)) == 1

    def test_only_missing_false_returns_everything(self, seeded):
        store, h = seeded
        store.upsert_enrichment(
            [{"member_id": MEMBER_ID, "http_status": 200, "member_badges": {}}]
        )
        assert len(store.pending_enrichment(h, only_missing=False)) == 1

    def test_limit_is_respected(self, seeded):
        store, h = seeded
        assert store.pending_enrichment(h, limit=0) == []


class TestUpsertAndMap:
    def test_round_trip(self, seeded):
        store, h = seeded
        store.upsert_enrichment(
            [
                {
                    "member_id": MEMBER_ID,
                    "profile_id": "ACwAAAA1B2C3",
                    "http_status": 200,
                    "member_badges": {
                        "openLink": True,
                        "premium": True,
                        "jobSeeker": False,
                    },
                    "inmail_restriction": "NO_RESTRICTION",
                }
            ]
        )
        enr = store.enrichment_map(h)[MEMBER_ID]
        assert enr["openProfile"] is True
        assert enr["premium"] is True
        assert enr["jobSeeker"] is False
        assert enr["inmailRestriction"] == "NO_RESTRICTION"

    def test_upsert_replaces_rather_than_duplicating(self, seeded):
        store, h = seeded
        for flag in (True, False):
            store.upsert_enrichment(
                [
                    {
                        "member_id": MEMBER_ID,
                        "http_status": 200,
                        "member_badges": {"openLink": flag},
                    }
                ]
            )
        assert store.enrichment_stats(h)["attempted"] == 1
        assert store.enrichment_map(h)[MEMBER_ID]["openProfile"] is False

    def test_unknown_badges_are_none_not_false(self, seeded):
        """Absent != False. A lead we never checked must not read as 'closed'."""
        store, h = seeded
        store.upsert_enrichment([{"member_id": MEMBER_ID, "http_status": 400}])
        assert store.enrichment_map(h)[MEMBER_ID]["openProfile"] is None

    def test_stats(self, seeded):
        store, h = seeded
        store.upsert_enrichment(
            [
                {
                    "member_id": MEMBER_ID,
                    "http_status": 200,
                    "member_badges": {"openLink": True},
                }
            ]
        )
        assert store.enrichment_stats(h) == {
            "attempted": 1,
            "succeeded": 1,
            "open_profiles": 1,
        }


class TestSearchDataIsUntouched:
    def test_raw_json_is_unchanged_by_enrichment(self, seeded):
        store, h = seeded
        store.upsert_enrichment(
            [
                {
                    "member_id": MEMBER_ID,
                    "http_status": 200,
                    "member_badges": {"openLink": True},
                    "raw": {"whatever": 1},
                }
            ]
        )
        rec = next(iter(store.iter_records(h, include_raw=True)))
        assert rec["_raw"] == REAL_LEAD

    def test_rescrape_does_not_drop_enrichment(self, seeded):
        store, h = seeded
        store.upsert_enrichment(
            [
                {
                    "member_id": MEMBER_ID,
                    "http_status": 200,
                    "member_badges": {"openLink": True},
                }
            ]
        )
        store.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
        assert store.enrichment_map(h)[MEMBER_ID]["openProfile"] is True


class TestExportJoin:
    def test_json_carries_enrichment_block(self, seeded):
        store, h = seeded
        store.upsert_enrichment(
            [
                {
                    "member_id": MEMBER_ID,
                    "http_status": 200,
                    "member_badges": {"openLink": True},
                    "inmail_restriction": "NO_RESTRICTION",
                }
            ]
        )
        result = export_query(store, store.get_query(h), "json")
        data = json.loads(Path(result["files"]["json"]).read_text(encoding="utf-8"))
        assert data[0]["enrichment"]["openProfile"] is True
        assert data[0]["enrichment"]["inmailRestriction"] == "NO_RESTRICTION"

    def test_json_has_no_enrichment_key_when_unenriched(self, seeded):
        store, h = seeded
        result = export_query(store, store.get_query(h), "json")
        data = json.loads(Path(result["files"]["json"]).read_text(encoding="utf-8"))
        assert "enrichment" not in data[0]
        assert "openLink" not in data[0]

    def test_csv_columns(self, seeded):
        store, h = seeded
        store.upsert_enrichment(
            [
                {
                    "member_id": MEMBER_ID,
                    "http_status": 200,
                    "member_badges": {"openLink": True},
                    "inmail_restriction": "NO_RESTRICTION",
                }
            ]
        )
        result = export_query(store, store.get_query(h), "csv")
        rows = list(
            csv.DictReader(
                Path(result["files"]["csv"]).read_text(encoding="utf-8").splitlines()
            )
        )
        assert rows[0]["open_profile"] == "True"
        assert rows[0]["inmail_restriction"] == "NO_RESTRICTION"
        assert "open_link" not in rows[0]

    def test_csv_blank_when_unenriched(self, seeded):
        """Blank, not False -- the CSV must not claim we checked when we didn't."""
        store, h = seeded
        result = export_query(store, store.get_query(h), "csv")
        rows = list(
            csv.DictReader(
                Path(result["files"]["csv"]).read_text(encoding="utf-8").splitlines()
            )
        )
        assert rows[0]["open_profile"] == ""

"""Tests for the decoration-id upgrade and the seniorityV2s it unlocks.

The search decoration the Sales Navigator client asks for (`LeadSearchResult-14`)
omits `seniorityV2s`; `-16` is a strict superset that includes it. capture.py
rewrites the request, normalize.py maps the field, and it lands in the
`seniorities` child table.
"""

import csv
from pathlib import Path

import pytest
from test_logic import REAL_LEAD

from sales_nav_mcp.capture import _upgrade_decoration_id
from sales_nav_mcp.export import export_query
from sales_nav_mcp.normalize import normalize_person
from sales_nav_mcp.store import Store, query_hash

PEOPLE_URL = "https://www.linkedin.com/sales/search/people?query=(filters:List())"

# Shape verified against a live response: multi-valued, unordered.
FOUNDER_LEVELS = [
    {"id": 120, "displayName": "Senior"},
    {"id": 320, "displayName": "Owner / Partner"},
    {"id": 310, "displayName": "CXO"},
]
LEAD_WITH_SENIORITY = {**REAL_LEAD, "seniorityV2s": FOUNDER_LEVELS}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


class TestDecorationUpgrade:
    def test_rewrites_14_to_16(self):
        url = (
            "https://www.linkedin.com/sales-api/salesApiLeadSearch?q=searchQuery"
            "&start=0&count=25&decorationId=com.linkedin.sales.deco.desktop"
            ".searchv2.LeadSearchResult-14"
        )
        assert _upgrade_decoration_id(url).endswith("LeadSearchResult-16")

    def test_rewrites_any_numeric_id(self):
        for n in (13, 15, 16):
            url = f"https://x/salesApiLeadSearch?decorationId=LeadSearchResult-{n}"
            assert _upgrade_decoration_id(url).endswith("LeadSearchResult-16")

    def test_leaves_unrelated_urls_untouched(self):
        """A no-op unless the URL actually carries a LeadSearchResult id."""
        for url in (
            "https://www.linkedin.com/sales-api/salesApiLeadSearch?q=searchQuery",
            "https://www.linkedin.com/sales-api/salesApiProfiles/(profileId:X)",
            "https://www.linkedin.com/sales/search/people?query=(x)",
        ):
            assert _upgrade_decoration_id(url) == url

    def test_does_not_touch_other_decoration_names(self):
        url = "https://x?decorationId=com.linkedin.sales.deco.AccountSearchResult-14"
        assert _upgrade_decoration_id(url) == url


class TestNormalize:
    def test_sorted_most_senior_first(self):
        rec = normalize_person(LEAD_WITH_SENIORITY)
        assert [s["id"] for s in rec["seniorities"]] == [320, 310, 120]
        assert rec["seniorities"][0]["displayName"] == "Owner / Partner"

    def test_top_is_the_highest_band(self):
        rec = normalize_person(LEAD_WITH_SENIORITY)
        assert rec["seniorityTopId"] == 320
        assert rec["seniorityTop"] == "Owner / Partner"

    def test_absent_field_yields_no_keys(self):
        """Searches captured before the upgrade simply have no seniority."""
        rec = normalize_person(REAL_LEAD)
        assert "seniorities" not in rec
        assert "seniorityTop" not in rec

    def test_malformed_entries_are_skipped(self):
        rec = normalize_person(
            {
                **REAL_LEAD,
                "seniorityV2s": ["junk", {}, {"id": 220, "displayName": "Director"}],
            }
        )
        assert rec["seniorities"] == [{"id": 220, "displayName": "Director"}]

    def test_non_list_is_ignored(self):
        rec = normalize_person({**REAL_LEAD, "seniorityV2s": "Director"})
        assert "seniorities" not in rec


class TestPersistence:
    def _seed(self, store, element):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [normalize_person(element)])
        return h

    def test_child_rows_written(self, store):
        self._seed(store, LEAD_WITH_SENIORITY)
        rows = store._conn.execute(
            "SELECT seniority_id, display_name FROM seniorities ORDER BY seniority_id DESC"
        ).fetchall()
        assert [r["seniority_id"] for r in rows] == [320, 310, 120]

    def test_iter_rows_derives_summary_and_top(self, store):
        h = self._seed(store, LEAD_WITH_SENIORITY)
        row = next(iter(store.iter_rows(h, "contacts")))
        assert row["seniority_top"] == "Owner / Partner"
        assert row["seniority_top_id"] == 320
        assert row["seniority_summary"] == "Owner / Partner; CXO; Senior"

    def test_blank_when_field_absent(self, store):
        h = self._seed(store, REAL_LEAD)
        row = next(iter(store.iter_rows(h, "contacts")))
        assert row["seniority_summary"] == ""
        assert row["seniority_top"] is None

    def test_raw_json_still_byte_faithful(self, store):
        """The rewrite changes what we ask for, never what we store."""
        h = self._seed(store, LEAD_WITH_SENIORITY)
        rec = next(iter(store.iter_records(h, include_raw=True)))
        assert rec["_raw"] == LEAD_WITH_SENIORITY


class TestExport:
    def test_csv_has_seniority_columns(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [normalize_person(LEAD_WITH_SENIORITY)])
        result = export_query(store, store.get_query(h), "csv")
        rows = list(
            csv.DictReader(
                Path(result["files"]["csv"]).read_text(encoding="utf-8").splitlines()
            )
        )
        assert rows[0]["seniority_top"] == "Owner / Partner"
        assert rows[0]["seniority_top_id"] == "320"
        assert rows[0]["seniority_summary"] == "Owner / Partner; CXO; Senior"

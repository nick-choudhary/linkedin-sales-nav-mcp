"""Unit tests for the SQLite store, URL hashing, resume tracking, and export."""

import json
import sqlite3
from pathlib import Path

import pytest

from sales_nav_mcp.export import export_query
from sales_nav_mcp.normalize import normalize_account, normalize_person
from sales_nav_mcp.store import (
    PAGE_SIZE,
    Store,
    normalize_url,
    query_hash,
    record_key,
)
from test_logic import REAL_ACCOUNT, REAL_LEAD

PEOPLE_URL = "https://www.linkedin.com/sales/search/people?query=(filters:List())"
ACCOUNTS_URL = "https://www.linkedin.com/sales/search/accounts?query=(filters:List())"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


class TestUrlHash:
    def test_page_param_ignored(self):
        a = "https://www.linkedin.com/sales/search/people?query=(x)&page=1"
        b = "https://www.linkedin.com/sales/search/people?query=(x)&page=5"
        assert query_hash(a) == query_hash(b)

    def test_trailing_slash_and_case(self):
        a = "https://WWW.linkedin.com/sales/search/people/?query=(x)"
        b = "https://www.linkedin.com/sales/search/people?query=(x)"
        assert normalize_url(a) == normalize_url(b)

    def test_different_search_differs(self):
        a = "https://www.linkedin.com/sales/search/people?query=(a)"
        b = "https://www.linkedin.com/sales/search/people?query=(b)"
        assert query_hash(a) != query_hash(b)

    def test_hash_is_16_hex(self):
        h = query_hash(PEOPLE_URL)
        assert len(h) == 16
        int(h, 16)  # parses as hex


class TestRecordKey:
    def test_prefers_entity_urn(self):
        assert record_key({"entityUrn": "urn:li:x"}) == "urn:li:x"

    def test_content_hash_fallback(self):
        k = record_key({"fullName": "A B"})
        assert k.startswith("sha:")


class TestStoreLifecycle:
    def test_upsert_is_idempotent(self, store):
        q1 = store.upsert_query(PEOPLE_URL, "contacts")
        q2 = store.upsert_query(PEOPLE_URL, "contacts")
        assert q1.url_hash == q2.url_hash
        assert len(store.list_queries()) == 1

    def test_add_records_dedupes(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        recs = [{"fullName": "A", "entityUrn": "urn:1"}, {"fullName": "B", "entityUrn": "urn:2"}]
        assert store.add_records(h, "contacts", recs) == 2
        # Re-adding the same URNs adds nothing.
        assert store.add_records(h, "contacts", recs) == 0
        assert store.count_records(h) == 2

    def test_progress_and_resume_fields(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [{"entityUrn": f"urn:{i}"} for i in range(25)])
        store.update_progress(h, last_page=1, total_available=200, status="paused")
        q = store.get_query(h)
        assert q.last_page == 1
        assert q.next_page == 2
        assert q.records_count == 25
        assert q.is_complete is False

    def test_is_complete_when_offset_exceeds_total(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.update_progress(h, last_page=2, total_available=40, status="in_progress")
        assert store.get_query(h).is_complete is True  # 2*25 >= 40

    def test_reset_clears_records_and_children(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
        store.reset_query(h)
        assert store.count_records(h) == 0
        assert store.get_query(h).last_page == 0
        for table in ("positions", "badges"):
            count = store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, table

    def test_resolve_by_url_or_hash(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        assert store.resolve_query(h).url_hash == h
        assert store.resolve_query(PEOPLE_URL).url_hash == h
        assert store.resolve_query("nope") is None


class TestFullSchemaPersistence:
    """The v2 promise: every captured field lands in a typed column, a child
    table, or (at minimum, and always) raw_json."""

    def test_lead_typed_columns(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
        row = store._conn.execute("SELECT * FROM leads").fetchone()
        assert row["full_name"] == "Jordan Rivera"
        assert row["member_id"] == 100000001
        assert row["open_link"] == 0
        assert row["premium"] == 1
        assert row["degree"] == 2
        assert row["title"] == "Chief Executive Officer"
        assert row["company_id"] == 900001
        assert row["company_industry"] == "Software Development"
        assert row["tenure_company_months"] == 17 * 12 + 8
        assert row["profile_picture_url"].endswith("800_800/a")

    def test_lead_raw_json_is_untouched_element(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
        raw = json.loads(
            store._conn.execute("SELECT raw_json FROM leads").fetchone()[0]
        )
        assert raw == REAL_LEAD  # byte-faithful, trackingId included

    def test_lead_children_inserted(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
        pos = store._conn.execute("SELECT * FROM positions").fetchone()
        assert pos["title"] == "Chief Executive Officer"
        assert pos["is_current"] == 1
        assert pos["start_year"] == 2009
        badge = store._conn.execute("SELECT * FROM badges").fetchone()
        assert badge["parent_type"] == "lead"
        assert badge["badge_id"] == "SECOND_DEGREE_CONNECTION"
        assert json.loads(badge["associated_urns"]) == [
            "urn:li:fs_salesProfile:(A, , )",
            "urn:li:fs_salesGroup:900003",
        ]

    def test_dedupe_does_not_duplicate_children(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        rec = normalize_person(REAL_LEAD)
        store.add_records(h, "contacts", [rec])
        store.add_records(h, "contacts", [rec])
        assert store._conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        assert store._conn.execute("SELECT COUNT(*) FROM badges").fetchone()[0] == 1

    def test_account_typed_columns_and_badges(self, store):
        store.upsert_query(ACCOUNTS_URL, "accounts")
        h = query_hash(ACCOUNTS_URL)
        store.add_records(h, "accounts", [normalize_account(REAL_ACCOUNT)])
        row = store._conn.execute("SELECT * FROM accounts").fetchone()
        assert row["company_name"] == "Contoso Telecom"
        assert row["company_id"] == 900002
        assert row["employee_count_range"] == "10,001+ employees"
        assert row["saved"] == 0
        assert row["logo_url"].endswith("200_200/contoso")
        badge = store._conn.execute("SELECT * FROM badges").fetchone()
        assert badge["parent_type"] == "account"
        assert badge["badge_id"] == "RECENT_FUNDING_EVENT"

    def test_iter_records_roundtrip(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
        [rec] = list(store.iter_records(h))
        assert rec["openLink"] is False
        assert rec["positions"][0]["title"] == "Chief Executive Officer"
        assert "_raw" not in rec  # stripped by default for token discipline
        [rec_raw] = list(store.iter_records(h, include_raw=True))
        assert rec_raw["_raw"] == REAL_LEAD

    def test_iter_rows_badge_summary(self, store):
        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
        [row] = list(store.iter_rows(h, "contacts"))
        assert row["badge_summary"] == "3 mutual connections"


class TestMigration:
    def test_v1_records_table_preserved(self, tmp_path):
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE records (id INTEGER PRIMARY KEY, url_hash TEXT, "
            "record_key TEXT, raw_json TEXT)"
        )
        conn.execute(
            "INSERT INTO records (url_hash, record_key, raw_json) "
            "VALUES ('h', 'k', '{}')"
        )
        conn.commit()
        conn.close()
        s = Store(db)
        try:
            legacy = s._conn.execute("SELECT COUNT(*) FROM records_v1").fetchone()[0]
            assert legacy == 1
            # New tables exist and are empty.
            assert s._conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
        finally:
            s.close()


class TestExport:
    def test_export_writes_files(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
        # export uses get_config().storage.resolved_output_dir()
        import sales_nav_mcp.config as config_mod

        config_mod.reset_config()

        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
        query = store.get_query(h)
        result = export_query(store, query, "both")

        assert result["record_count"] == 1
        assert "json" in result["files"] and "csv" in result["files"]
        data = json.loads(open(result["files"]["json"], encoding="utf-8").read())
        assert data[0]["fullName"] == "Jordan Rivera"
        assert data[0]["positions"][0]["title"] == "Chief Executive Officer"
        assert "_raw" not in data[0]
        csv_text = open(result["files"]["csv"], encoding="utf-8").read()
        assert "Jordan Rivera" in csv_text
        assert "open_link" in csv_text
        assert "badge_summary" in csv_text

        config_mod.reset_config()

    def test_export_include_raw(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
        import sales_nav_mcp.config as config_mod

        config_mod.reset_config()

        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
        result = export_query(store, store.get_query(h), "json", include_raw=True)
        data = json.loads(open(result["files"]["json"], encoding="utf-8").read())
        assert data[0]["_raw"] == REAL_LEAD

        config_mod.reset_config()


class TestStorageDirs:
    """The store and the exports live in two different places on purpose."""

    @pytest.fixture(autouse=True)
    def _clean_config(self, monkeypatch):
        import sales_nav_mcp.config as config_mod

        monkeypatch.delenv("STATE_DIR", raising=False)
        monkeypatch.delenv("OUTPUT_DIR", raising=False)
        config_mod.reset_config()
        yield
        config_mod.reset_config()

    def test_defaults_put_db_by_the_profile_and_exports_in_cwd(self):
        from sales_nav_mcp.config import StorageConfig

        storage = StorageConfig()
        assert storage.db_path() == (
            Path("~/.linkedin-sales-nav").expanduser().resolve() / "sales_nav.db"
        )
        assert storage.export_dir("abc") == Path("output").resolve() / "abc"

    def test_dirs_are_independent(self, tmp_path):
        from sales_nav_mcp.config import StorageConfig

        storage = StorageConfig(
            state_dir=str(tmp_path / "state"), output_dir=str(tmp_path / "out")
        )
        assert storage.db_path().parent == (tmp_path / "state").resolve()
        assert storage.raw_dir("abc") == (tmp_path / "state").resolve() / "abc" / "raw"
        assert storage.export_dir("abc") == (tmp_path / "out").resolve() / "abc"

    def test_export_writes_to_output_dir_not_state_dir(
        self, store, tmp_path, monkeypatch
    ):
        """Regression guard: exports must not follow the database."""
        import sales_nav_mcp.config as config_mod

        state = tmp_path / "state"
        out = tmp_path / "out"
        monkeypatch.setenv("STATE_DIR", str(state))
        monkeypatch.setenv("OUTPUT_DIR", str(out))
        config_mod.reset_config()

        store.upsert_query(PEOPLE_URL, "contacts")
        h = query_hash(PEOPLE_URL)
        store.add_records(h, "contacts", [normalize_person(REAL_LEAD)])
        result = export_query(store, store.get_query(h), "both")

        for path in result["files"].values():
            assert Path(path).is_relative_to(out)
        assert not state.exists()

    def test_empty_state_dir_rejected(self):
        from sales_nav_mcp.config import ConfigurationError, StorageConfig

        with pytest.raises(ConfigurationError, match="STATE_DIR"):
            StorageConfig(state_dir="").validate()


def test_page_size_constant():
    assert PAGE_SIZE == 25

"""SQLite persistence — the fail-proof spine of the scraper.

Design goals, straight from the requirements:

- **Fail-proof.** SQLite is transactional and runs in WAL mode, so a process
  killed mid-scrape never leaves a corrupt file. Results commit per page, so
  a crash at page 7 keeps pages 1-6 and records "resume at page 7".
- **Query identity by hash.** Each search URL is normalized (page/tracking
  params stripped) and hashed; the hash is the query's primary key, so the
  same search always maps to the same row.
- **Resume.** The `queries` row tracks `last_page` / `next_page` / status, so
  re-running a query continues instead of restarting.
- **Dedupe + provenance.** Result tables are unique on (url_hash, record_key),
  so a record can't be stored twice for one query, and every row carries its
  `url_hash` source — the "from source" column.
- **Lose nothing.** Every row stores `raw_json` — the complete untouched
  LinkedIn element — alongside a typed column for every scalar field the
  captured schema contains. Repeated groups land in child tables
  (`positions`, `badges`, `seniorities`) so they stay SQL-queryable.

Schema v9 (PRAGMA user_version=9): separate `leads` and `accounts` tables
replace the old single `records` table; a pre-existing `records` table is
renamed to `records_v1` untouched. `company_id` (parsed from URNs) is the
join key between leads and accounts. On top of that sit two additions:
`seniorities`, a child table of the multi-valued `seniorityV2s` LinkedIn
returns under search decoration id 16; and `lead_enrichment`, a side table of
Open Profile status keyed on the stable `member_id` (see enrich.py); and `lead_outreach`, the send-state machine,
also keyed on `member_id` so a person contacted once is never contacted
again from a different search (see outreach.py); and `outreach_events`, an
append-only log of every fetch and send attempt, which is the only place
trends live -- a single overwritten `last_error` column cannot show you
thirty failures clustered inside one minute.

Every schema addition is `CREATE TABLE IF NOT EXISTS` and no existing column
is ever altered, so opening an older database upgrades it in place without
touching a single stored row.

JSON/CSV are exported from this store (see export.py); the DB is the source of
truth, not the files. sqlite3 is stdlib, so this adds no dependency and works
identically on Windows/Mac/Linux.
"""

import contextlib
import hashlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sales_nav_mcp.config import get_config
from sales_nav_mcp.normalize import normalize_account, normalize_person

logger = logging.getLogger(__name__)

# Query-string params that don't change *which* search this is. Stripped before
# hashing so page 3 of a search maps to the same query as page 1, and so LinkedIn
# tracking noise doesn't fork one search into many.
_VOLATILE_PARAMS = {"page", "trk", "_ntb", "sessionid", "session_id", "sid"}

PAGE_SIZE = 25

SCHEMA_VERSION = 9


def normalize_url(url: str) -> str:
    """Canonical form of a search URL for identity hashing.

    Lowercases scheme+host, drops volatile params (page/tracking), and sorts
    the remaining query so trivially-reordered URLs hash the same.
    """
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _VOLATILE_PARAMS
    ]
    query.sort()
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urlencode(query),
            "",  # fragment dropped
        )
    )


def query_hash(url: str) -> str:
    """Stable 16-hex-char id for a search URL (SHA-256 of its normal form)."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def record_key(record: dict[str, Any]) -> str:
    """Dedupe key for one record: its LinkedIn URN, else a content hash."""
    urn = record.get("entityUrn") or record.get("objectUrn")
    if urn:
        return str(urn)
    payload = json.dumps(
        {k: v for k, v in record.items() if k != "_raw"}, sort_keys=True
    )
    return "sha:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _flag(value: Any) -> int | None:
    """Booleans as 0/1 so they survive SQLite and CSV round-trips."""
    return None if value is None else int(bool(value))


def _unflag(value: Any) -> bool | None:
    """0/1 back to a bool, preserving NULL as None ("not checked")."""
    return None if value is None else bool(value)


def _parse_profile_key(entity_urn: str) -> tuple[str, str, str] | None:
    """Pull (profileId, authType, authToken) out of a salesProfile URN.

    `urn:li:fs_salesProfile:(ACwAA...,NAME_SEARCH,rqQu)` -> the three parts.
    The authToken is scoped to the search that produced it, so it is read from
    the stored URN each time rather than cached separately.
    """
    m = re.search(r"\(([^,]+),([^,]+),([^)]*)\)", entity_urn or "")
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


# Normalized-record key -> leads column, for the typed scalar columns.
_LEAD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entityUrn", "entity_urn"),
    ("objectUrn", "object_urn"),
    ("memberId", "member_id"),
    ("fullName", "full_name"),
    ("firstName", "first_name"),
    ("lastName", "last_name"),
    ("geoRegion", "geo_region"),
    ("summary", "summary"),
    ("degree", "degree"),
    ("premium", "premium"),
    ("saved", "saved"),
    ("viewed", "viewed"),
    ("pendingInvitation", "pending_invitation"),
    ("memorialized", "memorialized"),
    ("blockThirdPartyDataSharing", "block_third_party_data_sharing"),
    ("listCount", "list_count"),
    ("profilePictureUrl", "profile_picture_url"),
    ("recipeType", "recipe_type"),
    ("title", "title"),
    ("companyName", "company_name"),
    ("companyUrn", "company_urn"),
    ("companyId", "company_id"),
    ("companyIndustry", "company_industry"),
    ("companyLocation", "company_location"),
    ("positionStartYear", "position_start_year"),
    ("positionStartMonth", "position_start_month"),
    ("tenureCompanyMonths", "tenure_company_months"),
    ("tenurePositionMonths", "tenure_position_months"),
)

_LEAD_FLAGS = {
    "premium",
    "saved",
    "viewed",
    "pendingInvitation",
    "memorialized",
    "blockThirdPartyDataSharing",
}

_ACCOUNT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entityUrn", "entity_urn"),
    ("companyId", "company_id"),
    ("companyName", "company_name"),
    ("industry", "industry"),
    ("employeeCountRange", "employee_count_range"),
    ("employeeDisplayCount", "employee_display_count"),
    ("description", "description"),
    ("logoUrl", "logo_url"),
    ("listCount", "list_count"),
    ("saved", "saved"),
    ("recipeType", "recipe_type"),
)

_ACCOUNT_FLAGS = {"saved"}

_POSITION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("posIndex", "pos_index"),
    ("posId", "pos_id"),
    ("title", "title"),
    ("companyName", "company_name"),
    ("companyUrn", "company_urn"),
    ("companyId", "company_id"),
    ("current", "is_current"),
    ("description", "description"),
    ("startYear", "start_year"),
    ("startMonth", "start_month"),
    ("tenureCompanyMonths", "tenure_company_months"),
    ("tenurePositionMonths", "tenure_position_months"),
    ("companyIndustry", "company_industry"),
    ("companyLocation", "company_location"),
    ("companyLogoUrl", "company_logo_url"),
)


@dataclass
class QueryRow:
    url_hash: str
    url: str
    scraper_type: str
    status: str
    last_page: int
    total_available: int | None
    records_count: int
    created_at: float
    updated_at: float
    # How deep this query is meant to go: "search" | "open_profile" | "full".
    # Stored on the query rather than passed per call, so a resumed run knows
    # what it was for without the caller having to remember.
    depth: str = "search"

    @property
    def next_page(self) -> int:
        return self.last_page + 1

    @property
    def is_complete(self) -> bool:
        if self.status == "complete":
            return True
        if self.total_available is not None:
            return self.last_page * PAGE_SIZE >= self.total_available
        return False


class Store:
    """Thin, synchronous SQLite wrapper. Operations are millisecond-scale and
    are already serialized by the browser lock, so no async layer is needed."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _migrate(self) -> None:
        # v1 -> v2: the old single `records` table is preserved untouched as
        # `records_v1` (its raw_json held only the curated normalized subset,
        # so it can't be faithfully backfilled into the typed columns).
        legacy = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='records'"
        ).fetchone()
        if legacy:
            logger.warning(
                "Migrating store to schema v2: renaming legacy 'records' table "
                "to 'records_v1' (kept as-is; re-scrape queries to fill the "
                "new tables)."
            )
            self._conn.execute("ALTER TABLE records RENAME TO records_v1")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS queries (
                url_hash        TEXT PRIMARY KEY,
                url             TEXT NOT NULL,
                scraper_type    TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'new',
                last_page       INTEGER NOT NULL DEFAULT 0,
                total_available INTEGER,
                records_count   INTEGER NOT NULL DEFAULT 0,
                created_at      REAL NOT NULL,
                updated_at      REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leads (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash      TEXT NOT NULL,
                record_key    TEXT NOT NULL,
                entity_urn    TEXT,
                object_urn    TEXT,
                member_id     INTEGER,
                full_name     TEXT,
                first_name    TEXT,
                last_name     TEXT,
                geo_region    TEXT,
                summary       TEXT,
                degree        INTEGER,
                premium       INTEGER,
                open_link     INTEGER,
                saved         INTEGER,
                viewed        INTEGER,
                pending_invitation INTEGER,
                memorialized  INTEGER,
                block_third_party_data_sharing INTEGER,
                list_count    INTEGER,
                profile_picture_url TEXT,
                recipe_type   TEXT,
                title         TEXT,
                company_name  TEXT,
                company_urn   TEXT,
                company_id    INTEGER,
                company_industry TEXT,
                company_location TEXT,
                position_start_year INTEGER,
                position_start_month INTEGER,
                tenure_company_months INTEGER,
                tenure_position_months INTEGER,
                raw_json      TEXT NOT NULL,
                first_seen_at REAL NOT NULL,
                UNIQUE(url_hash, record_key)
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash      TEXT NOT NULL,
                record_key    TEXT NOT NULL,
                entity_urn    TEXT,
                company_id    INTEGER,
                company_name  TEXT,
                industry      TEXT,
                employee_count_range TEXT,
                employee_display_count TEXT,
                description   TEXT,
                logo_url      TEXT,
                list_count    INTEGER,
                saved         INTEGER,
                recipe_type   TEXT,
                raw_json      TEXT NOT NULL,
                first_seen_at REAL NOT NULL,
                UNIQUE(url_hash, record_key)
            );
            CREATE TABLE IF NOT EXISTS positions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id       INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                pos_index     INTEGER NOT NULL,
                pos_id        INTEGER,
                title         TEXT,
                company_name  TEXT,
                company_urn   TEXT,
                company_id    INTEGER,
                is_current    INTEGER,
                description   TEXT,
                start_year    INTEGER,
                start_month   INTEGER,
                tenure_company_months INTEGER,
                tenure_position_months INTEGER,
                company_industry TEXT,
                company_location TEXT,
                company_logo_url TEXT
            );
            CREATE TABLE IF NOT EXISTS badges (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_type   TEXT NOT NULL,
                parent_id     INTEGER NOT NULL,
                badge_index   INTEGER NOT NULL,
                badge_id      TEXT,
                display_value TEXT,
                header_text   TEXT,
                message_text  TEXT,
                associated_urns TEXT
            );
            CREATE TABLE IF NOT EXISTS seniorities (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id       INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
                seniority_id  INTEGER NOT NULL,
                display_name  TEXT
            );
            CREATE TABLE IF NOT EXISTS lead_outreach (
                member_id     INTEGER NOT NULL,
                campaign      TEXT NOT NULL,
                status        TEXT NOT NULL,
                channel       TEXT,
                subject       TEXT,
                body          TEXT,
                evidence_used TEXT,
                attempts      INTEGER NOT NULL DEFAULT 0,
                last_error    TEXT,
                queued_at     REAL,
                sent_at       REAL,
                updated_at    REAL NOT NULL,
                PRIMARY KEY (member_id, campaign)
            );
            CREATE TABLE IF NOT EXISTS lead_enrichment (
                member_id          INTEGER PRIMARY KEY,
                profile_id         TEXT,
                open_link          INTEGER,
                premium            INTEGER,
                job_seeker         INTEGER,
                inmail_restriction TEXT,
                http_status        INTEGER,
                fetched_at         REAL NOT NULL,
                raw_json           TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_leads_hash ON leads(url_hash);
            CREATE INDEX IF NOT EXISTS idx_leads_urn ON leads(entity_urn);
            CREATE INDEX IF NOT EXISTS idx_leads_member ON leads(member_id);
            CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id);
            CREATE INDEX IF NOT EXISTS idx_accounts_hash ON accounts(url_hash);
            CREATE INDEX IF NOT EXISTS idx_accounts_company ON accounts(company_id);
            CREATE INDEX IF NOT EXISTS idx_positions_lead ON positions(lead_id);
            CREATE INDEX IF NOT EXISTS idx_badges_parent ON badges(parent_type, parent_id);
            CREATE INDEX IF NOT EXISTS idx_enrich_profile ON lead_enrichment(profile_id);
            CREATE TABLE IF NOT EXISTS lead_profiles (
                member_id   INTEGER PRIMARY KEY,
                profile_id  TEXT,
                fetched_at  REAL NOT NULL,
                http_status INTEGER,
                raw_json    TEXT
            );
            CREATE TABLE IF NOT EXISTS outreach_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL NOT NULL,
                kind        TEXT NOT NULL,
                member_id   INTEGER,
                campaign    TEXT,
                ok          INTEGER NOT NULL,
                http_status INTEGER,
                error       TEXT,
                duration_ms INTEGER,
                detail      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_seniorities_lead ON seniorities(lead_id);
            CREATE INDEX IF NOT EXISTS idx_events_ts ON outreach_events(ts);
            CREATE INDEX IF NOT EXISTS idx_events_kind ON outreach_events(kind, ok);
            CREATE INDEX IF NOT EXISTS idx_outreach_status ON lead_outreach(status);
            CREATE INDEX IF NOT EXISTS idx_outreach_sent ON lead_outreach(sent_at);
            """
        )
        # Additive column migration. CREATE TABLE IF NOT EXISTS cannot add
        # a column to a table that already exists, so depth is added here
        # for databases created before v7. Existing rows default to
        # 'search', which is exactly what they were.
        self._ensure_column("queries", "depth", "TEXT NOT NULL DEFAULT 'search'")
        # The lead reference used for THIS send. entity_urn embeds a
        # search-scoped authToken, and one person can appear in several
        # searches with different tokens -- so reconciliation must reuse
        # the exact reference the send used, not whichever row a join
        # happens to return.
        self._ensure_column("lead_outreach", "entity_urn", "TEXT")
        # Why an enrichment attempt failed. A 200 whose body would not
        # parse must stay retryable, and http_status alone cannot say so.
        self._ensure_column("lead_enrichment", "error", "TEXT")
        # Same reasoning for full profiles: a 200 whose body would not
        # parse has no payload, so status alone cannot mark it fetched.
        self._ensure_column("lead_profiles", "error", "TEXT")
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._conn.commit()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        """Add a column if it is missing. Never rewrites or drops anything."""
        existing = {
            r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            logger.info("Adding column %s.%s", table, column)
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def set_query_depth(self, url_hash: str, depth: str) -> None:
        """Record how deep this query should be taken."""
        if depth not in ("search", "open_profile", "full"):
            raise ValueError(f"unknown depth {depth!r}")
        self._conn.execute(
            "UPDATE queries SET depth = ?, updated_at = ? WHERE url_hash = ?",
            (depth, time.time(), url_hash),
        )
        self._conn.commit()

    # -- profiles --------------------------------------------------------

    def upsert_profile(
        self,
        member_id: int,
        *,
        profile_id: str | None,
        http_status: int | None,
        raw: dict[str, Any] | None,
        error: str | None = None,
    ) -> None:
        """Store one full profile fetch (depth 3), keyed on the stable id."""
        self._conn.execute(
            "INSERT INTO lead_profiles (member_id, profile_id, fetched_at, "
            "http_status, raw_json, error) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(member_id) DO UPDATE SET "
            "profile_id=COALESCE(excluded.profile_id, lead_profiles.profile_id), "
            "fetched_at=excluded.fetched_at, http_status=excluded.http_status, "
            "raw_json=CASE WHEN excluded.error IS NULL THEN excluded.raw_json "
            "ELSE lead_profiles.raw_json END, error=excluded.error",
            (
                int(member_id),
                profile_id,
                time.time(),
                http_status,
                json.dumps(raw, ensure_ascii=False) if raw else None,
                error,
            ),
        )
        self._conn.commit()

    def profile_stats(self, url_hash: str) -> dict[str, int]:
        """Successful full-profile fetches among this query's leads.

        Counted directly rather than derived by subtraction: `records_count`
        includes account rows and leads that pending_profiles skips (no
        member_id, unparseable URN), neither of which implies a successful
        fetch.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM lead_profiles p JOIN leads l "
            "ON l.member_id = p.member_id "
            "WHERE l.url_hash = ? AND p.http_status = 200 "
            "AND p.error IS NULL AND p.raw_json IS NOT NULL",
            (url_hash,),
        ).fetchone()
        return {"fetched": int(row["n"] or 0)}

    def get_profile(self, member_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM lead_profiles WHERE member_id = ?", (int(member_id),)
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        raw = out.pop("raw_json", None)
        # Only a parsed payload counts as a profile. A 200 that failed to
        # parse stored nothing, and reporting it as present would hand the
        # drafting step an empty record that looks fetched.
        if raw:
            out["profile"] = json.loads(raw)
        return out

    def pending_profiles(
        self, url_hash: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Leads of this query with no successful full-profile fetch yet."""
        if limit is not None and limit <= 0:
            return []
        rows: list[dict[str, Any]] = []
        for row in self._conn.execute(
            "SELECT l.member_id, l.entity_urn, l.full_name FROM leads l "
            "WHERE l.url_hash = ? AND l.member_id IS NOT NULL "
            "AND l.entity_urn IS NOT NULL AND l.member_id NOT IN "
            "(SELECT member_id FROM lead_profiles "
            "WHERE http_status = 200 AND error IS NULL AND raw_json IS NOT NULL) "
            "ORDER BY l.id",
            (url_hash,),
        ):
            parsed = _parse_profile_key(row["entity_urn"])
            if not parsed:
                continue
            rows.append(
                {
                    "member_id": row["member_id"],
                    "full_name": row["full_name"],
                    "profile_id": parsed[0],
                    "auth_type": parsed[1],
                    "auth_token": parsed[2],
                }
            )
            if limit is not None and len(rows) >= limit:
                break
        return rows

    # -- queries ---------------------------------------------------------

    def upsert_query(self, url: str, scraper_type: str) -> QueryRow:
        h = query_hash(url)
        now = time.time()
        existing = self.get_query(h)
        if existing is None:
            self._conn.execute(
                "INSERT INTO queries (url_hash, url, scraper_type, status, "
                "created_at, updated_at) VALUES (?, ?, ?, 'new', ?, ?)",
                (h, url, scraper_type, now, now),
            )
            self._conn.commit()
            return self.get_query(h)  # type: ignore[return-value]
        if existing.scraper_type != scraper_type:
            logger.warning(
                "Query %s previously scraped as %s, now %s; keeping the row.",
                h,
                existing.scraper_type,
                scraper_type,
            )
        return existing

    def get_query(self, url_hash: str) -> QueryRow | None:
        row = self._conn.execute(
            "SELECT * FROM queries WHERE url_hash = ?", (url_hash,)
        ).fetchone()
        return self._to_query_row(row) if row else None

    def resolve_query(self, url_or_hash: str) -> QueryRow | None:
        """Look up a query by its hash or by its URL."""
        row = self.get_query(url_or_hash)
        if row:
            return row
        return self.get_query(query_hash(url_or_hash))

    def list_queries(self) -> list[QueryRow]:
        rows = self._conn.execute(
            "SELECT * FROM queries ORDER BY updated_at DESC"
        ).fetchall()
        return [self._to_query_row(r) for r in rows]

    def update_progress(
        self,
        url_hash: str,
        *,
        last_page: int,
        total_available: int | None,
        status: str,
    ) -> None:
        count = self.count_records(url_hash)
        self._conn.execute(
            "UPDATE queries SET last_page = ?, total_available = COALESCE(?, "
            "total_available), records_count = ?, status = ?, updated_at = ? "
            "WHERE url_hash = ?",
            (last_page, total_available, count, status, time.time(), url_hash),
        )
        self._conn.commit()

    def reset_query(self, url_hash: str) -> None:
        """Forget progress and records for a query (a forced re-scrape)."""
        self._delete_records(url_hash)
        self._conn.execute(
            "UPDATE queries SET last_page = 0, records_count = 0, "
            "status = 'new', updated_at = ? WHERE url_hash = ?",
            (time.time(), url_hash),
        )
        self._conn.commit()

    def _delete_records(self, url_hash: str) -> None:
        # positions cascade off leads; badges are polymorphic so are cleared
        # explicitly before their parents go.
        for table, parent_type in (("leads", "lead"), ("accounts", "account")):
            self._conn.execute(
                f"DELETE FROM badges WHERE parent_type = ? AND parent_id IN "
                f"(SELECT id FROM {table} WHERE url_hash = ?)",
                (parent_type, url_hash),
            )
            self._conn.execute(f"DELETE FROM {table} WHERE url_hash = ?", (url_hash,))

    # -- records ---------------------------------------------------------

    def add_records(
        self, url_hash: str, scraper_type: str, records: list[dict[str, Any]]
    ) -> int:
        """Insert records, ignoring ones already stored for this query.

        Returns the count of genuinely new rows. Commits once, so a page is an
        all-or-nothing unit. Every row stores the complete raw element in
        raw_json; positions/badges land in their child tables.
        """
        now = time.time()
        new = 0
        insert = (
            self._insert_lead if scraper_type == "contacts" else self._insert_account
        )
        for rec in records:
            if insert(url_hash, rec, now):
                new += 1
        self._conn.commit()
        return new

    @staticmethod
    def _raw_payload(rec: dict[str, Any]) -> str:
        # The untouched LinkedIn element when we have it; otherwise the record
        # itself (minus the _raw slot) so raw_json is never empty.
        raw = rec.get("_raw")
        if not isinstance(raw, dict):
            raw = {k: v for k, v in rec.items() if k != "_raw"}
        return json.dumps(raw, ensure_ascii=False)

    def _insert_lead(self, url_hash: str, rec: dict[str, Any], now: float) -> bool:
        values = [
            _flag(rec.get(key)) if key in _LEAD_FLAGS else rec.get(key)
            for key, _col in _LEAD_COLUMNS
        ]
        columns = ", ".join(col for _key, col in _LEAD_COLUMNS)
        placeholders = ", ".join("?" for _ in _LEAD_COLUMNS)
        cur = self._conn.execute(
            f"INSERT OR IGNORE INTO leads (url_hash, record_key, {columns}, "
            f"raw_json, first_seen_at) VALUES (?, ?, {placeholders}, ?, ?)",
            (url_hash, record_key(rec), *values, self._raw_payload(rec), now),
        )
        if cur.rowcount != 1:
            return False
        lead_id = cur.lastrowid
        for pos in rec.get("positions") or []:
            pos_values = [
                _flag(pos.get(key)) if key == "current" else pos.get(key)
                for key, _col in _POSITION_COLUMNS
            ]
            pos_columns = ", ".join(col for _key, col in _POSITION_COLUMNS)
            pos_placeholders = ", ".join("?" for _ in _POSITION_COLUMNS)
            self._conn.execute(
                f"INSERT INTO positions (lead_id, {pos_columns}) "
                f"VALUES (?, {pos_placeholders})",
                (lead_id, *pos_values),
            )
        self._insert_badges("lead", lead_id, rec.get("badges") or [])
        self._insert_seniorities(lead_id, rec.get("seniorities") or [])
        return True

    def _insert_account(self, url_hash: str, rec: dict[str, Any], now: float) -> bool:
        values = [
            _flag(rec.get(key)) if key in _ACCOUNT_FLAGS else rec.get(key)
            for key, _col in _ACCOUNT_COLUMNS
        ]
        columns = ", ".join(col for _key, col in _ACCOUNT_COLUMNS)
        placeholders = ", ".join("?" for _ in _ACCOUNT_COLUMNS)
        cur = self._conn.execute(
            f"INSERT OR IGNORE INTO accounts (url_hash, record_key, {columns}, "
            f"raw_json, first_seen_at) VALUES (?, ?, {placeholders}, ?, ?)",
            (url_hash, record_key(rec), *values, self._raw_payload(rec), now),
        )
        if cur.rowcount != 1:
            return False
        self._insert_badges("account", cur.lastrowid, rec.get("badges") or [])
        return True

    def _insert_badges(
        self, parent_type: str, parent_id: int, badges: list[dict[str, Any]]
    ) -> None:
        for badge in badges:
            urns = badge.get("associatedUrns")
            self._conn.execute(
                "INSERT INTO badges (parent_type, parent_id, badge_index, "
                "badge_id, display_value, header_text, message_text, "
                "associated_urns) VALUES (?,?,?,?,?,?,?,?)",
                (
                    parent_type,
                    parent_id,
                    badge.get("badgeIndex", 0),
                    badge.get("id"),
                    badge.get("displayValue"),
                    badge.get("headerText"),
                    badge.get("messageText"),
                    json.dumps(urns, ensure_ascii=False) if urns else None,
                ),
            )

    def _insert_seniorities(
        self, lead_id: int, seniorities: list[dict[str, Any]]
    ) -> None:
        """Store the seniority bands LinkedIn assigns a lead.

        Multi-valued by nature — a founder comes back as Owner/Partner + CXO +
        Senior — hence a child table rather than a column. Empty for searches
        captured before decoration id 16, which simply did not return the field.
        """
        for entry in seniorities:
            self._conn.execute(
                "INSERT INTO seniorities (lead_id, seniority_id, display_name) "
                "VALUES (?,?,?)",
                (lead_id, entry.get("id"), entry.get("displayName")),
            )

    # -- outreach --------------------------------------------------------
    #
    # Keyed on (member_id, campaign), NOT on url_hash or entity_urn. Sending is
    # the one place where a duplicate is not a wasted request but a real-world
    # mistake -- the same human found by three searches must be messaged once.
    # `member_id` is the only identifier stable across searches.
    #
    # `absent` means never attempted; it never means "do not contact". A row is
    # only created when something is actually decided about the lead.

    # "sending" is the two-phase-commit marker: written BEFORE the Send
    # click, flipped to "sent" after. A crash in that window leaves a
    # "sending" row -- ambiguous but visible, which is recoverable. Without
    # it, a crash between click and commit leaves LinkedIn holding a
    # delivered message and this database holding nothing, and the person
    # becomes eligible again.
    OUTREACH_STATES = ("queued", "sending", "sent", "failed", "skipped")

    # Treated as "already contacted". `sending` is included deliberately:
    # when we cannot tell whether a message went out, assume it did. A
    # missed follow-up is recoverable; a duplicate cold message is not.
    CONTACTED_STATES = ("sent", "sending")

    def outreach_row(self, member_id: int, campaign: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM lead_outreach WHERE member_id = ? AND campaign = ?",
            (int(member_id), campaign),
        ).fetchone()
        return dict(row) if row else None

    def already_contacted(self, member_id: int) -> str | None:
        """Which campaign, if any, has ALREADY sent to this person.

        Deliberately campaign-agnostic: a second campaign must not re-message
        someone the first one already reached.
        """
        row = self._conn.execute(
            "SELECT campaign FROM lead_outreach WHERE member_id = ? "
            "AND status IN ('sent', 'sending') LIMIT 1",
            (int(member_id),),
        ).fetchone()
        return row["campaign"] if row else None

    def sent_since(self, since_epoch: float) -> int:
        """How many messages have gone out since `since_epoch`, all campaigns.

        Backs the daily cap. Counts across campaigns because LinkedIn sees one
        account, not your campaign labels.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM lead_outreach WHERE status = 'sent' "
            "AND sent_at >= ?",
            (since_epoch,),
        ).fetchone()
        return int(row["n"] or 0)

    def outreach_candidates(
        self,
        url_hash: str,
        campaign: str,
        *,
        limit: int = 10,
        open_profile_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Leads eligible for a first message, best channel first.

        Excludes anyone already sent to in ANY campaign, and anyone with a row
        in this campaign. With open_profile_only (the default) it returns only
        leads confirmed Open Profile by enrich_leads -- the free channel.
        """
        sql = (
            "SELECT l.member_id, l.full_name, l.title, l.company_name, "
            "l.entity_urn, e.open_link AS open_profile "
            "FROM leads l "
            "LEFT JOIN lead_enrichment e ON e.member_id = l.member_id "
            "WHERE l.url_hash = ? AND l.member_id IS NOT NULL "
            "AND l.member_id NOT IN (SELECT member_id FROM lead_outreach "
            "                        WHERE status IN ('sent','sending')) "
            "AND l.member_id NOT IN (SELECT member_id FROM lead_outreach "
            "                        WHERE campaign = ?) "
        )
        params: list[Any] = [url_hash, campaign]
        if open_profile_only:
            sql += "AND e.open_link = 1 AND e.http_status = 200 "
        sql += "ORDER BY e.open_link DESC, l.id LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params)]

    def record_outreach(
        self,
        member_id: int,
        campaign: str,
        status: str,
        *,
        channel: str | None = None,
        entity_urn: str | None = None,
        subject: str | None = None,
        body: str | None = None,
        evidence_used: list[str] | None = None,
        last_error: str | None = None,
        bump_attempts: bool = False,
    ) -> None:
        """Upsert one outreach row. Commits immediately.

        Commit-per-send, not per batch: a crash mid-run must never leave a
        message sent on LinkedIn but unrecorded here, because that is exactly
        how someone gets messaged twice.
        """
        if status not in self.OUTREACH_STATES:
            raise ValueError(f"unknown outreach status {status!r}")
        now = time.time()
        existing = self.outreach_row(member_id, campaign)
        attempts = (existing or {}).get("attempts", 0) or 0
        if bump_attempts:
            attempts += 1
        sent_at = now if status == "sent" else (existing or {}).get("sent_at")
        queued_at = (existing or {}).get("queued_at") or (
            now if status == "queued" else None
        )
        self._conn.execute(
            "INSERT INTO lead_outreach (member_id, campaign, status, channel, "
            "subject, body, evidence_used, attempts, last_error, queued_at, "
            "sent_at, updated_at, entity_urn) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(member_id, campaign) DO UPDATE SET "
            "status=excluded.status, channel=excluded.channel, "
            "subject=excluded.subject, body=excluded.body, "
            "evidence_used=excluded.evidence_used, attempts=excluded.attempts, "
            "last_error=excluded.last_error, queued_at=excluded.queued_at, "
            "sent_at=excluded.sent_at, updated_at=excluded.updated_at, "
            "entity_urn=COALESCE(excluded.entity_urn, lead_outreach.entity_urn)",
            (
                int(member_id),
                campaign,
                status,
                channel,
                subject,
                body,
                json.dumps(evidence_used, ensure_ascii=False)
                if evidence_used
                else None,
                attempts,
                last_error,
                queued_at,
                sent_at,
                now,
                entity_urn or (existing or {}).get("entity_urn"),
            ),
        )
        self._conn.commit()

    def log_event(
        self,
        kind: str,
        *,
        ok: bool,
        member_id: int | None = None,
        campaign: str | None = None,
        http_status: int | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Append one event. Never raises -- observability must not break work."""
        try:
            self._conn.execute(
                "INSERT INTO outreach_events (ts, kind, member_id, campaign, ok, "
                "http_status, error, duration_ms, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    kind,
                    int(member_id) if member_id is not None else None,
                    campaign,
                    int(bool(ok)),
                    http_status,
                    (error or None) and str(error)[:400],
                    duration_ms,
                    detail,
                ),
            )
            self._conn.commit()
        except Exception:  # pragma: no cover - logging must never be fatal
            logger.warning("failed to write outreach event", exc_info=True)

    def event_summary(self, since_epoch: float) -> dict[str, Any]:
        """Counts and error clusters since `since_epoch`.

        The clusters are the point: thirty identical errors inside a minute is
        what rate limiting looks like, and no per-row column can show it.
        """
        rows = self._conn.execute(
            "SELECT kind, ok, COUNT(*) AS n FROM outreach_events "
            "WHERE ts >= ? GROUP BY kind, ok",
            (since_epoch,),
        ).fetchall()
        by_kind: dict[str, dict[str, int]] = {}
        for r in rows:
            slot = by_kind.setdefault(r["kind"], {"ok": 0, "failed": 0})
            slot["ok" if r["ok"] else "failed"] += r["n"]
        errors = [
            {"error": r["error"], "count": r["n"], "last_seen": r["last_ts"]}
            for r in self._conn.execute(
                "SELECT error, COUNT(*) AS n, MAX(ts) AS last_ts FROM outreach_events "
                "WHERE ts >= ? AND ok = 0 AND error IS NOT NULL "
                "GROUP BY error ORDER BY n DESC LIMIT 5",
                (since_epoch,),
            )
        ]
        return {"by_kind": by_kind, "top_errors": errors}

    def ambiguous_sends(
        self, campaign: str | None = None, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Rows stuck in `sending` -- we clicked but never recorded the outcome."""
        sql = (
            # entity_urn comes from the outreach row, not from the leads
            # join: the join can return any of several search-scoped
            # references for the same person, and reconciliation must use
            # the one this send actually used.
            "SELECT o.member_id, o.campaign, o.subject, o.body, o.channel, "
            "o.evidence_used, o.updated_at, o.entity_urn, "
            "MIN(l.full_name) AS full_name FROM lead_outreach o "
            "LEFT JOIN leads l ON l.member_id = o.member_id "
            "WHERE o.status = 'sending'"
        )
        params: list[Any] = []
        if campaign:
            sql += " AND o.campaign = ?"
            params.append(campaign)
        sql += " GROUP BY o.member_id, o.campaign ORDER BY o.updated_at LIMIT ?"
        params.append(int(limit))
        rows = []
        for r in self._conn.execute(sql, params):
            row = dict(r)
            # Decoded so the caller can hand it straight back to
            # record_outreach, which expects a list.
            raw = row.get("evidence_used")
            row["evidence_used"] = json.loads(raw) if raw else None
            rows.append(row)
        return rows

    def outreach_stats_for_query(self, url_hash: str) -> dict[str, Any]:
        """Outreach counts for THIS query's leads only.

        `outreach_stats` is global on purpose (LinkedIn sees one account), but a
        per-query funnel must not report another query's sends as its own.
        """
        by_status: dict[str, int] = {}
        by_channel: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT o.status, o.channel, COUNT(*) AS n FROM lead_outreach o "
            "JOIN leads l ON l.member_id = o.member_id "
            "WHERE l.url_hash = ? GROUP BY o.status, o.channel",
            (url_hash,),
        ):
            by_status[row["status"]] = by_status.get(row["status"], 0) + row["n"]
            if row["channel"]:
                by_channel[row["channel"]] = (
                    by_channel.get(row["channel"], 0) + row["n"]
                )
        return {"by_status": by_status, "by_channel": by_channel}

    def count_ambiguous(self, url_hash: str | None = None) -> int:
        """SQL count of `sending` rows -- not len() of a limited page."""
        if url_hash is None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM lead_outreach WHERE status = 'sending'"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM lead_outreach o "
                "JOIN leads l ON l.member_id = o.member_id "
                "WHERE l.url_hash = ? AND o.status = 'sending'",
                (url_hash,),
            ).fetchone()
        return int(row["n"] or 0)

    def outreach_stats(self, campaign: str | None = None) -> dict[str, Any]:
        sql = "SELECT status, channel, COUNT(*) AS n FROM lead_outreach"
        params: tuple[Any, ...] = ()
        if campaign:
            sql += " WHERE campaign = ?"
            params = (campaign,)
        sql += " GROUP BY status, channel"
        by_status: dict[str, int] = {}
        by_channel: dict[str, int] = {}
        for row in self._conn.execute(sql, params):
            by_status[row["status"]] = by_status.get(row["status"], 0) + row["n"]
            if row["channel"]:
                by_channel[row["channel"]] = (
                    by_channel.get(row["channel"], 0) + row["n"]
                )
        return {"by_status": by_status, "by_channel": by_channel}

    def count_records(self, url_hash: str) -> int:
        total = 0
        for table in ("leads", "accounts"):
            total += int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE url_hash = ?", (url_hash,)
                ).fetchone()[0]
            )
        return total

    # -- enrichment ------------------------------------------------------
    #
    # Open Profile status is NOT in the search payload -- the search API's
    # `openLink` is a dead field that is false for everyone. The live flag is
    # `memberBadges.openLink` on the profile endpoint, one request per lead.
    #
    # It lives in its own table rather than in `leads` or in `raw_json`:
    #   * `iter_records` re-derives every record from `raw_json` on read, so
    #     anything written elsewhere would be silently dropped; and writing it
    #     INTO raw_json would break the "raw is exactly what LinkedIn sent"
    #     invariant that normalize.py depends on.
    #   * `member_id` is stable across searches, while `entity_urn` embeds a
    #     per-search authToken. Keying on member_id means a lead found by three
    #     searches is fetched once and shared by all three.

    def pending_enrichment(
        self, url_hash: str, *, limit: int | None = None, only_missing: bool = True
    ) -> list[dict[str, Any]]:
        """Leads of this query that still need an enrichment fetch.

        Returns what the profile endpoint needs: the stable member_id plus the
        profileId/authType/authToken triple parsed out of entity_urn.
        """
        if limit is not None and limit <= 0:
            return []
        sql = (
            "SELECT l.member_id, l.entity_urn, l.full_name FROM leads l "
            "WHERE l.url_hash = ? AND l.member_id IS NOT NULL "
            "AND l.entity_urn IS NOT NULL"
        )
        if only_missing:
            sql += (
                " AND l.member_id NOT IN ("
                "SELECT member_id FROM lead_enrichment "
                "WHERE http_status = 200 AND error IS NULL)"
            )
        sql += " ORDER BY l.id"
        rows: list[dict[str, Any]] = []
        for row in self._conn.execute(sql, (url_hash,)):
            parsed = _parse_profile_key(row["entity_urn"])
            if not parsed:
                continue
            profile_id, auth_type, auth_token = parsed
            rows.append(
                {
                    "member_id": row["member_id"],
                    "full_name": row["full_name"],
                    "profile_id": profile_id,
                    "auth_type": auth_type,
                    "auth_token": auth_token,
                }
            )
            if limit is not None and len(rows) >= limit:
                break
        return rows

    def upsert_enrichment(self, rows: Iterable[dict[str, Any]]) -> int:
        """Insert or replace enrichment rows. Returns how many were written."""
        now = time.time()
        n = 0
        for r in rows:
            member_id = r.get("member_id")
            if member_id is None:
                continue
            badges = r.get("member_badges") or {}
            self._conn.execute(
                "INSERT INTO lead_enrichment (member_id, profile_id, open_link, "
                "premium, job_seeker, inmail_restriction, http_status, "
                "fetched_at, raw_json, error) VALUES (?,?,?,?,?,?,?,?,?,?) "
                # A failed refresh must not destroy a good result. With
                # only_missing=False a transient parse error would
                # otherwise overwrite known badges with NULLs and lose a
                # confirmed Open Profile. Value columns are only taken
                # from the incoming row when that row actually parsed;
                # the attempt's status and error are always recorded.
                "ON CONFLICT(member_id) DO UPDATE SET "
                "profile_id=COALESCE(excluded.profile_id, lead_enrichment.profile_id), "
                "open_link=CASE WHEN excluded.error IS NULL THEN excluded.open_link "
                "ELSE lead_enrichment.open_link END, "
                "premium=CASE WHEN excluded.error IS NULL THEN excluded.premium "
                "ELSE lead_enrichment.premium END, "
                "job_seeker=CASE WHEN excluded.error IS NULL THEN excluded.job_seeker "
                "ELSE lead_enrichment.job_seeker END, "
                "inmail_restriction=CASE WHEN excluded.error IS NULL "
                "THEN excluded.inmail_restriction "
                "ELSE lead_enrichment.inmail_restriction END, "
                "raw_json=CASE WHEN excluded.error IS NULL THEN excluded.raw_json "
                "ELSE lead_enrichment.raw_json END, "
                "error=excluded.error, "
                "http_status=excluded.http_status, fetched_at=excluded.fetched_at",
                (
                    int(member_id),
                    r.get("profile_id"),
                    _flag(badges.get("openLink")),
                    _flag(badges.get("premium")),
                    _flag(badges.get("jobSeeker")),
                    r.get("inmail_restriction"),
                    r.get("http_status"),
                    now,
                    json.dumps(r.get("raw"), ensure_ascii=False)
                    if r.get("raw")
                    else None,
                    r.get("error"),
                ),
            )
            n += 1
        self._conn.commit()
        return n

    def enrichment_map(self, url_hash: str) -> dict[int, dict[str, Any]]:
        """member_id -> enrichment dict, for the leads of this query."""
        out: dict[int, dict[str, Any]] = {}
        for row in self._conn.execute(
            "SELECT e.* FROM lead_enrichment e JOIN leads l "
            "ON l.member_id = e.member_id WHERE l.url_hash = ?",
            (url_hash,),
        ):
            out[int(row["member_id"])] = {
                "openProfile": _unflag(row["open_link"]),
                "premium": _unflag(row["premium"]),
                "jobSeeker": _unflag(row["job_seeker"]),
                "inmailRestriction": row["inmail_restriction"],
                "httpStatus": row["http_status"],
                "fetchedAt": row["fetched_at"],
            }
        return out

    def enrichment_stats(self, url_hash: str) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN e.http_status = 200 AND e.error IS NULL "
            "THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN e.open_link = 1 THEN 1 ELSE 0 END) AS opened "
            "FROM lead_enrichment e JOIN leads l ON l.member_id = e.member_id "
            "WHERE l.url_hash = ?",
            (url_hash,),
        ).fetchone()
        return {
            "attempted": int(row["n"] or 0),
            "succeeded": int(row["ok"] or 0),
            "open_profiles": int(row["opened"] or 0),
        }

    def iter_records(
        self,
        url_hash: str,
        *,
        limit: int | None = None,
        offset: int = 0,
        include_raw: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Yield fully-normalized records, re-derived from the stored raw
        element so the mapping is always the current one."""
        remaining = limit
        skip = offset
        for table, fn in (("leads", normalize_person), ("accounts", normalize_account)):
            sql = f"SELECT raw_json FROM {table} WHERE url_hash = ? ORDER BY id"
            for row in self._conn.execute(sql, (url_hash,)):
                if skip > 0:
                    skip -= 1
                    continue
                if remaining is not None:
                    if remaining <= 0:
                        return
                    remaining -= 1
                yield fn(json.loads(row["raw_json"]), include_raw=include_raw)

    def iter_rows(self, url_hash: str, scraper_type: str) -> Iterator[dict[str, Any]]:
        """Yield typed column rows (plus badge_summary) for CSV export."""
        table = "leads" if scraper_type == "contacts" else "accounts"
        parent_type = "lead" if scraper_type == "contacts" else "account"
        for row in self._conn.execute(
            f"SELECT * FROM {table} WHERE url_hash = ? ORDER BY id", (url_hash,)
        ):
            # sqlite3.Row iterates over values, not keys, so .keys() must stay.
            record = {k: row[k] for k in row.keys() if k != "raw_json"}  # noqa: SIM118
            badges = self._conn.execute(
                "SELECT display_value FROM badges WHERE parent_type = ? AND "
                "parent_id = ? ORDER BY badge_index",
                (parent_type, row["id"]),
            ).fetchall()
            record["badge_summary"] = "; ".join(
                b["display_value"] for b in badges if b["display_value"]
            )
            if parent_type == "lead":
                # Derived here rather than stored on `leads`, exactly like
                # badge_summary — it keeps the leads table unchanged, so an
                # existing database needs no ALTER TABLE.
                levels = self._conn.execute(
                    "SELECT seniority_id, display_name FROM seniorities "
                    "WHERE lead_id = ? ORDER BY seniority_id DESC",
                    (row["id"],),
                ).fetchall()
                record["seniority_summary"] = "; ".join(
                    lvl["display_name"] for lvl in levels if lvl["display_name"]
                )
                record["seniority_top"] = levels[0]["display_name"] if levels else None
                record["seniority_top_id"] = (
                    levels[0]["seniority_id"] if levels else None
                )
            yield record

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()

    @staticmethod
    def _to_query_row(row: sqlite3.Row) -> QueryRow:
        return QueryRow(
            url_hash=row["url_hash"],
            url=row["url"],
            scraper_type=row["scraper_type"],
            status=row["status"],
            last_page=row["last_page"],
            total_available=row["total_available"],
            records_count=row["records_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            # sqlite3.Row iterates over values, not keys, so .keys() must stay
            # (same reason as iter_rows). Guarded because a database opened
            # before v7 has no depth column until the migration runs.
            depth=(
                row["depth"]  # noqa: SIM118
                if "depth" in row.keys() and row["depth"]  # noqa: SIM118
                else "search"
            ),
        )


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(get_config().storage.db_path())
    return _store


def close_store() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None

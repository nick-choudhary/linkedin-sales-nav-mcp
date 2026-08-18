"""Export a stored query's records to JSON and/or CSV.

These are *views* of the SQLite store — the DB is the source of truth. Exports
are regenerated on demand, so they can never be the thing that corrupts, and
re-exporting after a resume just reflects the larger set.

JSON carries the fully-normalized records (nested positions/badges included,
raw element on request); CSV carries every typed column of the leads/accounts
table plus a badge_summary, so nothing needs JSON parsing to reach a
spreadsheet.

Files land under output/<url_hash>/:
    query.json            the query metadata (url, hash, counts, status)
    <scraper_type>.json   the records as a JSON array
    <scraper_type>.csv    the records as CSV (typed columns)
"""

import csv
import json
import logging
from pathlib import Path
from typing import Any, Literal

from sales_nav_mcp.config import get_config
from sales_nav_mcp.store import QueryRow, Store

logger = logging.getLogger(__name__)

ExportFormat = Literal["json", "csv", "both"]

# Every typed column of the corresponding table (see store.py), in reading
# order, plus badge_summary. id/url_hash/record_key stay in for provenance.
_CONTACT_COLUMNS = (
    "id", "url_hash", "record_key",
    "full_name", "first_name", "last_name",
    "title", "company_name", "company_urn", "company_id",
    "company_industry", "company_location",
    "geo_region", "summary", "degree",
    "premium", "open_link", "saved", "viewed", "pending_invitation",
    "memorialized", "block_third_party_data_sharing", "list_count",
    "position_start_year", "position_start_month",
    "tenure_company_months", "tenure_position_months",
    "member_id", "entity_urn", "object_urn",
    "profile_picture_url", "recipe_type", "badge_summary",
    "first_seen_at",
)
_ACCOUNT_COLUMNS = (
    "id", "url_hash", "record_key",
    "company_name", "company_id", "industry",
    "employee_count_range", "employee_display_count", "description",
    "list_count", "saved", "logo_url", "entity_urn", "recipe_type",
    "badge_summary", "first_seen_at",
)


def _query_dir(query: QueryRow) -> Path:
    return get_config().storage.export_dir(query.url_hash)


def _write_metadata(query: QueryRow, out_dir: Path) -> Path:
    path = out_dir / "query.json"
    path.write_text(
        json.dumps(
            {
                "url_hash": query.url_hash,
                "url": query.url,
                "scraper_type": query.scraper_type,
                "status": query.status,
                "last_page": query.last_page,
                "total_available": query.total_available,
                "records_count": query.records_count,
                "created_at": query.created_at,
                "updated_at": query.updated_at,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def export_query(
    store: Store,
    query: QueryRow,
    fmt: ExportFormat = "both",
    *,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Write query.json plus the requested record files. Returns the paths."""
    out_dir = _query_dir(query)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {"metadata": str(_write_metadata(query, out_dir))}
    record_count = store.count_records(query.url_hash)

    if fmt in ("json", "both"):
        records = list(
            store.iter_records(query.url_hash, include_raw=include_raw)
        )
        json_path = out_dir / f"{query.scraper_type}.json"
        json_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        written["json"] = str(json_path)
        record_count = len(records)

    if fmt in ("csv", "both"):
        columns = (
            _CONTACT_COLUMNS
            if query.scraper_type == "contacts"
            else _ACCOUNT_COLUMNS
        )
        csv_path = out_dir / f"{query.scraper_type}.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
            writer.writeheader()
            for row in store.iter_rows(query.url_hash, query.scraper_type):
                writer.writerow(
                    {c: ("" if row.get(c) is None else row.get(c)) for c in columns}
                )
        written["csv"] = str(csv_path)

    return {
        "url_hash": query.url_hash,
        "record_count": record_count,
        "files": written,
    }

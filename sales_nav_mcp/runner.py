"""The search flow shared by the contacts and accounts tools.

Ties the pieces together: validate URL -> register/resume the query in the
store -> drive the browser, committing each page to the store as it arrives ->
return a LEAN summary (counts, progress, next step), never the raw records.

Keeping records out of the return value is the token discipline the design
calls for: hundreds of rows live in SQLite, and the model only ever sees a
small status object unless it explicitly asks to export or sample.
"""

import json
import logging
from typing import Any

from fastmcp import Context

from sales_nav_mcp.browser import get_browser
from sales_nav_mcp.capture import ScraperType, capture_search, validate_sales_nav_url
from sales_nav_mcp.config import get_config
from sales_nav_mcp.store import PAGE_SIZE, get_store, query_hash

logger = logging.getLogger(__name__)


async def run_search(
    scraper_type: ScraperType,
    search_url: str,
    ctx: Context,
    *,
    pages: int,
    resume: bool,
    refresh: bool,
    include_raw: bool,
) -> dict[str, Any]:
    """Run (or resume) a search and persist it. Returns a lean summary."""
    validate_sales_nav_url(search_url, scraper_type)
    h = query_hash(search_url)
    store = get_store()
    query = store.upsert_query(search_url, scraper_type)

    if refresh:
        store.reset_query(h)
        query = store.get_query(h)  # type: ignore[assignment]

    if query.is_complete and not refresh:
        return {
            "url_hash": h,
            "scraper_type": scraper_type,
            "status": "complete",
            "already_complete": True,
            "total_records": query.records_count,
            "total_available": query.total_available,
            "suggestion": (
                f"This search is already fully scraped ({query.records_count} "
                "records). Call export_results to save JSON/CSV, or pass "
                "refresh=true to re-scrape from the start."
            ),
        }

    start_page = query.next_page if resume else 1
    new_records = 0
    captured_this_call = 0

    async def on_page(records: list[dict[str, Any]], page_number: int, paging) -> None:
        nonlocal new_records, captured_this_call
        added = store.add_records(h, scraper_type, records)
        new_records += added
        captured_this_call += len(records)
        total = (paging or {}).get("total")
        # Commit progress after every page so a crash resumes from here.
        store.update_progress(
            h, last_page=page_number, total_available=total, status="in_progress"
        )

    browser = get_browser()
    async with browser.lock:
        await ctx.report_progress(
            progress=0, total=100, message="Opening Sales Navigator"
        )
        page = await browser.get_page()
        await ctx.report_progress(
            progress=20,
            total=100,
            message=f"Capturing results from page {start_page}",
        )
        summary = await capture_search(
            page,
            search_url,
            scraper_type,
            pages,
            start_page=start_page,
            on_page=on_page,
            include_raw=include_raw,
        )
        await ctx.report_progress(progress=100, total=100, message="Complete")

    # Schema-discovery aid: when raw was requested, dump the complete,
    # untouched API responses so a later session can enumerate every field
    # LinkedIn returns and design the DB schema from ground truth.
    raw_dir = None
    if include_raw and summary.get("raw_payloads"):
        raw_dir = get_config().storage.resolved_output_dir() / h / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        for i, payload in enumerate(summary["raw_payloads"]):
            (raw_dir / f"{scraper_type}-response-{i}.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    status = "complete" if summary["exhausted"] else "paused"
    store.update_progress(
        h,
        last_page=summary["last_page"],
        total_available=(summary["paging"] or {}).get("total"),
        status=status,
    )
    final = store.get_query(h)
    total_records = final.records_count if final else new_records
    total_available = final.total_available if final else None

    if status == "complete":
        suggestion = (
            f"Scrape complete: {total_records} unique records saved. Call "
            f"export_results(url_or_hash='{h}', format='csv') to save a file, "
            "or get_results to pull a sample for analysis."
        )
    else:
        remaining = (
            total_available - summary["last_page"] * PAGE_SIZE
            if total_available is not None
            else None
        )
        suggestion = (
            f"Saved {total_records} records so far (through page "
            f"{summary['last_page']}). "
            + (
                f"~{max(0, remaining)} of {total_available} still to fetch. "
                if remaining is not None
                else ""
            )
            + "Call this tool again with the same URL to resume from page "
            f"{summary['next_page']}, or export_results to save what you have."
        )

    return {
        "url_hash": h,
        "scraper_type": scraper_type,
        "status": status,
        "new_records_this_call": new_records,
        "captured_this_call": captured_this_call,
        "total_records": total_records,
        "total_available": total_available,
        "pages_fetched": summary["pages_fetched"],
        "last_page": summary["last_page"],
        "next_page": summary["next_page"],
        "raw_dir": str(raw_dir) if raw_dir else None,
        "suggestion": suggestion,
    }

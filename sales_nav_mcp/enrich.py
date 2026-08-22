"""Open Profile enrichment: the one signal search cannot give you.

Sales Navigator's lead-search payload carries an `openLink` field, but it is
dead -- it is `false` for every result, premium members included. The live flag
lives on the profile endpoint as `memberBadges.openLink`, which means one
request per lead. There is no batch form: `salesApiProfiles?ids=List(...)`
returns 400 in every shape tried.

Two details make this cheap enough to be practical:

* A trimmed projection works. The endpoint accepts a free-form `decoration`,
  so we ask for four fields instead of replaying the client's full profile
  decoration -- ~240 bytes per lead rather than ~15 KB.
* The `decoration` value must be encoded the way LinkedIn's own client encodes
  it: `(`, `)` and `,` percent-escaped, `*` and `~` and `:` left raw.
  `encodeURIComponent` does NOT do this, and the difference is a flat 400.

Requests are issued with `fetch` from inside the logged-in page, so they carry
the real session, cookies and CSRF token exactly like the SPA's own calls.
"""

import asyncio
import json
import logging
import time
from typing import Any

from sales_nav_mcp.browser import get_browser
from sales_nav_mcp.config import get_config
from sales_nav_mcp.exceptions import SalesNavMCPError
from sales_nav_mcp.store import get_store

logger = logging.getLogger(__name__)


class EnrichDepthDisabled(SalesNavMCPError):
    """Raised when depth-2 enrichment is not enabled in config."""


# Only what we actually store. Keeping this minimal is what makes per-lead
# enrichment affordable.
DECORATION = "(entityUrn,objectUrn,memberBadges,inmailRestriction)"

# A logged-in Sales Nav page to issue the fetches from. Any /sales page works;
# home is the cheapest to load.
_HOME = "https://www.linkedin.com/sales/home"

# Runs in the page. Takes the whole batch so one round-trip covers N leads,
# and paces itself between requests rather than hammering.
_FETCH_JS = r"""
async (args) => {
  const [targets, decoration, delayMs] = args;
  // LinkedIn's Rest.li encoding: ( ) , escaped; * ~ : left alone.
  const li = (s) => s.replace(/\(/g, '%28').replace(/\)/g, '%29').replace(/,/g, '%2C');
  const deco = li(decoration);
  const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)"?/) || [])[1];
  const headers = {
    'csrf-token': csrf,
    'x-restli-protocol-version': '2.0.0',
    'accept': '*/*',
  };
  const out = [];
  for (const t of targets) {
    const key = '(profileId:' + t.profile_id +
                ',authType:' + t.auth_type +
                ',authToken:' + t.auth_token + ')';
    const url = '/sales-api/salesApiProfiles/' + key + '?decoration=' + deco;
    let entry = { member_id: t.member_id, http_status: null };
    try {
      const r = await fetch(url, { headers, credentials: 'include' });
      entry.http_status = r.status;
      const text = await r.text();
      if (r.status === 200) {
        try {
          const j = JSON.parse(text);
          entry.member_badges = j.memberBadges || null;
          entry.inmail_restriction = j.inmailRestriction || null;
          entry.raw = j;
        } catch (e) { entry.parse_error = String(e); }
      }
    } catch (e) {
      entry.error = String(e);
    }
    out.push(entry);
    if (delayMs > 0) await new Promise((s) => setTimeout(s, delayMs));
  }
  return JSON.stringify(out);
}
"""


async def enrich_leads(
    url_hash: str,
    *,
    limit: int | None = None,
    only_missing: bool = True,
    chunk_size: int = 25,
) -> dict[str, Any]:
    """Fetch Open Profile status for a query's leads and store it.

    Writes to the `lead_enrichment` side table only -- `leads.raw_json` and the
    normalized records are never touched, so a re-scrape cannot clobber
    enrichment and enrichment cannot corrupt a re-scrape.
    """
    # Gate here, not only in search_contacts. The depth check on the search
    # tool is early feedback; this is the one that actually holds, because
    # enrich_leads can be called directly and it costs a request per lead.
    if not get_config().outreach.enable_enrich:
        raise EnrichDepthDisabled(
            "Open Profile enrichment (depth 2) is disabled. Set "
            "ENABLE_ENRICH=true to allow it. It costs one request per lead."
        )

    store = get_store()
    targets = store.pending_enrichment(url_hash, limit=limit, only_missing=only_missing)
    if not targets:
        stats = store.enrichment_stats(url_hash)
        return {
            "url_hash": url_hash,
            "enriched_this_call": 0,
            "nothing_pending": True,
            "events_last_24h": store.event_summary(time.time() - 24 * 3600),
            **stats,
            "suggestion": (
                "Every lead in this query already has Open Profile status. "
                "Call get_results or export_results to read it."
            ),
        }

    config = get_config()
    delay_ms = (
        int(max(0.0, config.pacing.page_delay_min) * 1000 / 4)
        if config.pacing.enabled
        else 0
    )

    browser = get_browser()
    written = 0
    failures = 0
    async with browser.lock:
        page = await browser.get_page()
        if not (page.url or "").startswith(_HOME):
            await page.goto(_HOME, wait_until="domcontentloaded")
            await asyncio.sleep(2)

        for start in range(0, len(targets), chunk_size):
            chunk = targets[start : start + chunk_size]
            payload = await page.evaluate(_FETCH_JS, [chunk, DECORATION, delay_ms])
            results = json.loads(payload)
            by_id = {t["member_id"]: t for t in chunk}
            rows = []
            for r in results:
                target = by_id.get(r.get("member_id")) or {}
                # A 200 whose body would not parse is a failed enrichment, not
                # a completed one. Counting it as written would report it as
                # enriched while the badges are actually missing.
                if (
                    r.get("http_status") != 200
                    or r.get("error")
                    or r.get("parse_error")
                ):
                    failures += 1
                rows.append(
                    {
                        "member_id": r.get("member_id"),
                        "profile_id": target.get("profile_id"),
                        "member_badges": r.get("member_badges"),
                        "inmail_restriction": r.get("inmail_restriction"),
                        "http_status": r.get("http_status"),
                        # Stored so pending_enrichment keeps this row
                        # retryable: a 200 that would not parse has no
                        # badges, so it is not enriched.
                        "error": (
                            r.get("error")
                            or r.get("parse_error")
                            or (
                                None
                                if r.get("http_status") == 200
                                else f"HTTP {r.get('http_status')}"
                            )
                        ),
                        "raw": r.get("raw"),
                    }
                )
            # upsert_enrichment persists every attempt (including failures, so
            # they are visible and retryable), but only clean fetches count as
            # enriched.
            store.upsert_enrichment(rows)
            written += sum(
                1
                for r in results
                if r.get("http_status") == 200
                and not (r.get("error") or r.get("parse_error"))
            )
            # One event per lead, so a run of 400s shows up as a cluster in
            # event_summary rather than as a single overwritten error column.
            for r in results:
                # A 200 that failed to parse is not a success. Counting it as
                # one would hide it from top_errors, which filters on ok = 0 --
                # so the exact failures worth noticing would be invisible.
                problem = r.get("error") or r.get("parse_error")
                if not problem and r.get("http_status") != 200:
                    # Name the failure, so the stored row is guarded and
                    # the error clusters in event_summary.
                    problem = f"HTTP {r.get('http_status')}"
                store.log_event(
                    "enrich",
                    ok=r.get("http_status") == 200 and not problem,
                    member_id=r.get("member_id"),
                    http_status=r.get("http_status"),
                    error=problem,
                )
            logger.info("enrich_leads %s: %d/%d done", url_hash, written, len(targets))

    stats = store.enrichment_stats(url_hash)
    remaining = len(store.pending_enrichment(url_hash, only_missing=True))
    return {
        "url_hash": url_hash,
        "events_last_24h": store.event_summary(time.time() - 24 * 3600),
        "enriched_this_call": written,
        "failed_this_call": failures,
        "remaining": remaining,
        **stats,
        "suggestion": _suggest(stats, failures, remaining),
    }


def _suggest(stats: dict[str, int], failures: int, remaining: int) -> str:
    parts = [
        f"{stats['succeeded']} leads have Open Profile status "
        f"({stats['open_profiles']} are Open Profile)."
    ]
    if failures:
        parts.append(
            f"{failures} fetches failed this call -- they stay pending and are "
            "retried on the next call."
        )
    if remaining:
        parts.append(f"{remaining} still pending; call again to continue.")
    else:
        parts.append("Nothing pending. Call export_results to write it out.")
    return " ".join(parts)

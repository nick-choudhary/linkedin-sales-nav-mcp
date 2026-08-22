"""Depth 3: the full profile fetch, for drafting material.

Same endpoint as `enrich.py` -- `salesApiProfiles` -- but a much wider
projection. They are kept as two calls rather than one merged fetch on purpose:

* `enrich_leads` is the cheap screen (~240 bytes) run across a whole list to
  find who is free to message.
* `fetch_lead_profiles` is the expensive read (~15 KB per lead, one browser
  request each) run only for the leads you are actually going to write to.
  `get_lead_profile` just reads what that stored -- it never calls LinkedIn.

Merging them would mean pulling heavy payloads for every lead in a 2,000-row
list to serve the couple of hundred you message, and -- more importantly --
the activity data would be as stale as the screen. Fetching at drafting time
means "recent" actually means recent.

The Rest.li encoding rule from enrich.py applies identically here: `(`, `)`
and `,` percent-escaped, `*` and `~` and `:` left raw.
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

# Everything the drafting model can legitimately draw on. Kept explicit rather
# than "give me everything", so what lands in the store is a deliberate choice.
DECORATION = (
    "(entityUrn,objectUrn,fullName,firstName,lastName,headline,summary,"
    "location,defaultPosition,positions*(title,companyName,description,current,"
    "startedOn,endedOn),educations*(schoolName,degree,fieldsOfStudy*),"
    "skills*,languages*,volunteeringExperiences*(role,companyName,cause),"
    "numOfConnections,numOfSharedConnections,memberBadges,inmailRestriction)"
)

_HOME = "https://www.linkedin.com/sales/home"


class ProfileDepthDisabled(SalesNavMCPError):
    """Raised when depth-3 profile fetching is not enabled in config."""


_FETCH_JS = r"""
async (args) => {
  const [targets, decoration, delayMs] = args;
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
    let entry = { member_id: t.member_id, profile_id: t.profile_id, http_status: null };
    try {
      const r = await fetch(url, { headers, credentials: 'include' });
      entry.http_status = r.status;
      const text = await r.text();
      if (r.status === 200) {
        try { entry.raw = JSON.parse(text); }
        catch (e) { entry.parse_error = String(e); }
      }
    } catch (e) { entry.error = String(e); }
    out.push(entry);
    if (delayMs > 0) await new Promise((s) => setTimeout(s, delayMs));
  }
  return JSON.stringify(out);
}
"""


async def fetch_profiles(
    url_hash: str, *, limit: int | None = 10, chunk_size: int = 10
) -> dict[str, Any]:
    """Fetch and store full profiles for a query's leads (depth 3)."""
    config = get_config()
    if not config.outreach.enable_profile:
        raise ProfileDepthDisabled(
            "Full profile fetching (depth 3) is disabled. Set ENABLE_PROFILE=true "
            "to allow it. It costs one heavy request per lead, so it is opt-in."
        )

    store = get_store()
    targets = store.pending_profiles(url_hash, limit=limit)
    if not targets:
        return {
            "url_hash": url_hash,
            "fetched_this_call": 0,
            "nothing_pending": True,
            "events_last_24h": store.event_summary(time.time() - 24 * 3600),
            "suggestion": "Every lead in this query already has a full profile.",
        }

    delay_ms = (
        int(max(0.0, config.pacing.page_delay_min) * 1000 / 3)
        if config.pacing.enabled
        else 0
    )
    written = 0
    failures = 0

    browser = get_browser()
    async with browser.lock:
        page = await browser.get_page()
        if not (page.url or "").startswith(_HOME):
            await page.goto(_HOME, wait_until="domcontentloaded")
            await asyncio.sleep(2)

        for start in range(0, len(targets), chunk_size):
            chunk = targets[start : start + chunk_size]
            results = json.loads(
                await page.evaluate(_FETCH_JS, [chunk, DECORATION, delay_ms])
            )
            for r in results:
                problem = r.get("error") or r.get("parse_error")
                ok = r.get("http_status") == 200 and not problem
                store.upsert_profile(
                    r.get("member_id"),
                    profile_id=r.get("profile_id"),
                    http_status=r.get("http_status"),
                    raw=r.get("raw"),
                    error=problem,
                )
                store.log_event(
                    "profile",
                    ok=ok,
                    member_id=r.get("member_id"),
                    http_status=r.get("http_status"),
                    error=problem,
                )
                if ok:
                    written += 1
                else:
                    failures += 1
            logger.info("fetch_profiles %s: %d/%d", url_hash, written, len(targets))

    remaining = len(store.pending_profiles(url_hash))
    return {
        "url_hash": url_hash,
        "fetched_this_call": written,
        "failed_this_call": failures,
        "remaining": remaining,
        "events_last_24h": store.event_summary(time.time() - 24 * 3600),
        "suggestion": (
            f"{written} full profiles stored, {remaining} still pending."
            if remaining
            else f"{written} stored; nothing pending."
        ),
    }

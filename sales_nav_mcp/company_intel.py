"""Company context for a pitch, compact and deterministic.

Two sources, cheapest first:

1. LinkedIn's Account IQ dossier, which the message panel loads on its own
   (`salesApiAccountDossier/<companyId>`, stored by compose capture). It
   carries strategic priorities, challenges, revenue model, workforce
   planning and news -- about 100 KB raw, so it is boiled down here to a few
   lines. For some companies it comes back as an empty shell.
2. TinyFish Search (news), only when the dossier is empty and
   TINYFISH_API_KEY is set. Search requests are free; results are cached per
   company for CACHE_DAYS.

No model is involved: the drafter receives the summary, never the raw data.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

TINYFISH_SEARCH_URL = "https://api.search.tinyfish.ai/"
CACHE_DAYS = 14
NEWS_RECENCY_DAYS = 365

_ITEMS = 3
_TEXT = 180


def _clip(text: Any, n: int = _TEXT) -> str | None:
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def summarize_dossier(raw: Any) -> dict[str, Any] | None:
    """The few dossier facts worth citing in a first message, or None if empty."""
    if not isinstance(raw, dict):
        return None

    def items(key: str) -> list[str]:
        out = []
        for it in (raw.get(key) or [])[:_ITEMS]:
            if not isinstance(it, dict):
                continue
            head, desc = _clip(it.get("text"), 80), _clip(it.get("description"))
            line = ": ".join(x for x in (head, desc) if x)
            if line:
                out.append(line)
        return out

    workforce = [
        _clip(w.get("text"))
        for w in (raw.get("workforcePlanningTextViewModel") or [])[:2]
        if isinstance(w, dict) and w.get("text")
    ]
    news = [
        _clip(n.get("newsTitle"), 120)
        for n in ((raw.get("bingCompanyNews") or {}).get("newsArticle") or [])[:2]
        if isinstance(n, dict) and n.get("newsTitle")
    ]
    summary = {
        "priorities": items("strategicPriorities"),
        "challenges": items("challenges"),
        "revenueModel": _clip((raw.get("revenueDetails") or {}).get("text")),
        "workforce": [w for w in workforce if w],
        "news": [n for n in news if n],
    }
    summary = {k: v for k, v in summary.items() if v}
    return {"source": "linkedin_account_iq", **summary} if summary else None


Fetcher = Callable[[str, dict[str, str]], dict[str, Any]]


def _http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https host
        return json.loads(r.read().decode("utf-8"))


def tinyfish_news(
    company_name: str, api_key: str, *, fetch: Fetcher | None = None
) -> dict[str, Any] | None:
    """Recent news about the company from TinyFish Search, or None."""
    if not (company_name and api_key):
        return None
    params = urllib.parse.urlencode(
        {
            "query": f'"{company_name}"',
            "domain_type": "news",
            "recency_minutes": NEWS_RECENCY_DAYS * 24 * 60,
        }
    )
    try:
        body = (fetch or _http_get_json)(
            f"{TINYFISH_SEARCH_URL}?{params}",
            {"X-API-Key": api_key, "Accept": "application/json"},
        )
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning("TinyFish search failed for %r: %s", company_name, e)
        return None
    news = []
    for res in (body or {}).get("results") or []:
        title = _clip(res.get("title"), 120)
        # Search matches loosely; keep only items that name the company.
        blob = f"{res.get('title', '')} {res.get('snippet', '')}".lower()
        if not title or company_name.lower() not in blob:
            continue
        news.append(
            {
                k: v
                for k, v in {
                    "title": title,
                    "date": res.get("date"),
                    "publisher": res.get("publisher"),
                    "snippet": _clip(res.get("snippet"), 160),
                    "url": res.get("url"),
                }.items()
                if v
            }
        )
        if len(news) >= _ITEMS:
            break
    return {"source": "tinyfish_news", "news": news} if news else None


def company_intel(
    store: Any,
    *,
    company_id: int | None,
    company_name: str | None,
    api_key: str = "",
    fetch: Fetcher | None = None,
) -> dict[str, Any] | None:
    """Dossier summary if LinkedIn had one, else cached/fresh TinyFish news."""
    if company_id is not None:
        dossier = store.company_dossier(company_id)
        summary = summarize_dossier(dossier["raw"]) if dossier else None
        if summary:
            return summary
    if not (company_name and api_key):
        return None
    key = f"name:{company_name.strip().lower()}"
    cached = store.company_intel_cached(key, max_age=CACHE_DAYS * 86400)
    if cached is not None:
        return cached or None  # {} caches "nothing found"
    found = tinyfish_news(company_name, api_key, fetch=fetch)
    store.cache_company_intel(key, found or {}, fetched_at=time.time())
    return found

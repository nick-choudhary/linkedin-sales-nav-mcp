"""Deterministic ICP screen, applied before a lead's message panel is opened.

A lead that fails costs nothing: no panel is opened, no LinkedIn request is
made, no model tokens are spent. What is left for judgement in the chat is
the genuinely borderline, not the obvious agency or recruiter.

Rules live in a JSON file (FIT_RULES_FILE, see fit_rules.example.json):

    {
      "exclude_keywords": ["agency", "consultancy", "dev shop"],
      "exclude_title_keywords": ["intern", "assistant", "recruiter"],
      "exclude_industries": ["Staffing and Recruiting"],
      "exclude_companies": ["Acme Corp"]
    }

Matching is case-insensitive and on whole words or phrases.
`exclude_keywords` is checked against the title, company name, the current
position's description and the About summary; the other lists against their
own field only.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FitRules:
    exclude_keywords: list[str] = field(default_factory=list)
    exclude_title_keywords: list[str] = field(default_factory=list)
    exclude_industries: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (
            self.exclude_keywords
            or self.exclude_title_keywords
            or self.exclude_industries
            or self.exclude_companies
        )


def load_rules(path: Path | None) -> FitRules:
    """Rules from `path`; empty rules when it is unset, missing or unreadable."""
    if path is None or not path.is_file():
        return FitRules()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("fit rules at %s unreadable; screening disabled", path)
        return FitRules()
    known = FitRules.__dataclass_fields__
    return FitRules(
        **{
            k: [str(x) for x in v]
            for k, v in data.items()
            if k in known and isinstance(v, list)
        }
    )


def _hit(terms: list[str], text: str) -> str | None:
    for term in terms:
        if term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.I):
            return term
    return None


def screen(record: dict[str, Any], rules: FitRules) -> str | None:
    """None when the lead passes; otherwise a short reason naming the rule."""
    if rules.empty:
        return None
    title = record.get("title") or ""
    company = record.get("companyName") or ""
    positions = record.get("positions") or [{}]
    current = positions[0] if positions and isinstance(positions[0], dict) else {}
    body = " \n ".join(
        str(x)
        for x in (title, company, current.get("description"), record.get("summary"))
        if x
    )
    if term := _hit(rules.exclude_companies, company):
        return f"fit: company {term!r}"
    if term := _hit(rules.exclude_industries, record.get("companyIndustry") or ""):
        return f"fit: industry {term!r}"
    if term := _hit(rules.exclude_title_keywords, title):
        return f"fit: title {term!r}"
    if term := _hit(rules.exclude_keywords, body):
        return f"fit: keyword {term!r}"
    return None

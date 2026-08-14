"""Parse LinkedIn Sales Navigator search API payloads into flat records.

The mappings below are grounded in captured live payloads (2026-08, 50 lead
elements / 49 account elements): every field LinkedIn actually returned is
mapped, and the complete untouched element is kept under ``_raw`` on every
record, so a payload change can never silently lose data — worst case a new
field waits inside ``_raw`` until it gets a mapping.

Shape notes from the captures:

- Lead elements ($recipeType ...LeadSearchResult) carry their position data
  under ``currentPositions[]`` (employer decorated under
  ``companyUrnResolutionResult``) and sales signals under
  ``spotlightBadges[]``. There is no top-level ``headline`` — what the UI
  shows as the headline is the ``summary`` field.
- Account elements (...AccountSearchResult) are flat scalars plus the same
  badge shape; they contain no website or headquarters fields.
- ``trackingId`` is binary noise and intentionally stays raw-only.
"""

from typing import Any


def find_search_elements(payload: Any) -> list[dict[str, Any]]:
    """Return the first non-empty list of result dicts found in *payload*.

    Handles the shapes Sales Navigator has used: a top-level ``elements``
    array, a ``data`` wrapper, and GraphQL-style nesting. Recurses breadth-ish
    but stops at the first plausible hit.
    """
    if isinstance(payload, dict):
        elements = payload.get("elements")
        if isinstance(elements, list) and _looks_like_records(elements):
            return [e for e in elements if isinstance(e, dict)]
        # Common wrappers first, then any remaining values.
        ordered_values = []
        for key in ("data", "included", "results", "leadSearchResults"):
            if key in payload:
                ordered_values.append(payload[key])
        ordered_values.extend(
            v for k, v in payload.items()
            if k not in ("data", "included", "results", "leadSearchResults")
        )
        for value in ordered_values:
            found = find_search_elements(value)
            if found:
                return found
    elif isinstance(payload, list):
        if _looks_like_records(payload):
            return [e for e in payload if isinstance(e, dict)]
        for item in payload:
            found = find_search_elements(item)
            if found:
                return found
    return []


def _looks_like_records(items: list[Any]) -> bool:
    """A list of dicts that carry name/company/urn-ish keys, not metadata."""
    dicts = [i for i in items if isinstance(i, dict)]
    if not dicts:
        return False
    markers = {
        "firstName", "lastName", "fullName", "companyName", "name",
        "entityUrn", "objectUrn", "currentPositions", "degree", "industry",
    }
    hits = sum(1 for d in dicts if markers & set(d.keys()))
    return hits >= max(1, len(dicts) // 2)


def find_paging(payload: Any) -> dict[str, Any] | None:
    """Return the first ``paging``-like dict found (total/start/count)."""
    if isinstance(payload, dict):
        paging = payload.get("paging")
        if isinstance(paging, dict) and (
            "total" in paging or "count" in paging
        ):
            return {
                "total": paging.get("total"),
                "start": paging.get("start"),
                "count": paging.get("count"),
            }
        for value in payload.values():
            found = find_paging(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_paging(item)
            if found:
                return found
    return None


def _text(value: Any) -> str | None:
    """Coerce LinkedIn's occasionally-wrapped text values to a plain string."""
    if value is None or isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("text", "value", "localized", "name"):
            if key in value:
                return _text(value[key])
    return None


def _urn_id(urn: Any) -> int | None:
    """Numeric tail of URNs like urn:li:member:123 / urn:li:fs_salesCompany:9."""
    if not isinstance(urn, str):
        return None
    tail = urn.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _image_url(image: Any) -> str | None:
    """Largest-artifact URL from a *PictureDisplayImage dict.

    All artifact sizes remain available in ``_raw``; this composes the one
    most callers want.
    """
    if not isinstance(image, dict):
        return None
    root = image.get("rootUrl")
    artifacts = image.get("artifacts")
    if not (isinstance(root, str) and isinstance(artifacts, list) and artifacts):
        return None
    best = max(
        (a for a in artifacts if isinstance(a, dict)),
        key=lambda a: a.get("width") or 0,
        default=None,
    )
    segment = best.get("fileIdentifyingUrlPathSegment") if best else None
    return root + segment if isinstance(segment, str) else None


def _tenure_months(tenure: Any) -> int | None:
    """Collapse {numYears, numMonths} into one sortable month count."""
    if not isinstance(tenure, dict):
        return None
    years, months = tenure.get("numYears"), tenure.get("numMonths")
    if years is None and months is None:
        return None
    return (years or 0) * 12 + (months or 0)


def _normalize_position(pos: dict[str, Any], index: int) -> dict[str, Any]:
    """Map one currentPositions[] entry, including the decorated company."""
    company = pos.get("companyUrnResolutionResult")
    company = company if isinstance(company, dict) else {}
    started = pos.get("startedOn")
    started = started if isinstance(started, dict) else {}
    record = {
        "posIndex": index,
        "posId": pos.get("posId"),
        "title": _text(pos.get("title")),
        "companyName": _text(pos.get("companyName")) or _text(company.get("name")),
        "companyUrn": pos.get("companyUrn") or company.get("entityUrn"),
        "companyId": _urn_id(pos.get("companyUrn") or company.get("entityUrn")),
        "current": pos.get("current"),
        "description": _text(pos.get("description")),
        "startYear": started.get("year"),
        "startMonth": started.get("month"),
        "tenureCompanyMonths": _tenure_months(pos.get("tenureAtCompany")),
        "tenurePositionMonths": _tenure_months(pos.get("tenureAtPosition")),
        "companyIndustry": _text(company.get("industry")),
        "companyLocation": _text(company.get("location")),
        "companyLogoUrl": _image_url(company.get("companyPictureDisplayImage")),
        "recipeType": pos.get("$recipeType"),
    }
    return {k: v for k, v in record.items() if v is not None}


def _normalize_badge(badge: dict[str, Any], index: int) -> dict[str, Any]:
    """Map one spotlightBadges[] entry (connection/hiring/funding signals)."""
    popup = badge.get("popup")
    popup = popup if isinstance(popup, dict) else {}
    config = popup.get("config")
    config = config if isinstance(config, dict) else {}
    unions = badge.get("associatedEntityUrnsUnions")
    urns: list[str] = []
    if isinstance(unions, list):
        for entry in unions:
            if isinstance(entry, dict):
                urns.extend(v for v in entry.values() if isinstance(v, str))
    record = {
        "badgeIndex": index,
        "id": badge.get("id"),
        "displayValue": _text(badge.get("displayValue")),
        "headerText": _text((popup.get("header") or {}).get("text")),
        "messageText": _text((popup.get("message") or {}).get("text")),
        "associatedUrns": urns or None,
        "popupTypes": config.get("popupTypes") or None,
        "supportsDataFetch": config.get("supportsDataFetch"),
    }
    return {k: v for k, v in record.items() if v is not None}


def _positions(element: dict[str, Any]) -> list[dict[str, Any]]:
    positions = element.get("currentPositions") or element.get("currentPosition")
    if isinstance(positions, dict):
        positions = [positions]
    if not isinstance(positions, list):
        return []
    return [
        _normalize_position(p, i)
        for i, p in enumerate(positions)
        if isinstance(p, dict)
    ]


def _badges(element: dict[str, Any]) -> list[dict[str, Any]]:
    badges = element.get("spotlightBadges")
    if not isinstance(badges, list):
        return []
    return [
        _normalize_badge(b, i) for i, b in enumerate(badges) if isinstance(b, dict)
    ]


def normalize_person(
    element: dict[str, Any], *, include_raw: bool = True
) -> dict[str, Any]:
    """Map one lead element completely: scalars, positions, badges, raw."""
    positions = _positions(element)
    primary = next((p for p in positions if p.get("current")), None) or (
        positions[0] if positions else {}
    )
    record: dict[str, Any] = {
        "fullName": _text(element.get("fullName"))
        or " ".join(
            p for p in (_text(element.get("firstName")), _text(element.get("lastName")))
            if p
        )
        or None,
        "firstName": _text(element.get("firstName")),
        "lastName": _text(element.get("lastName")),
        "geoRegion": _text(element.get("geoRegion")) or _text(element.get("location")),
        "summary": _text(element.get("summary")) or _text(element.get("headline")),
        "degree": element.get("degree"),
        "entityUrn": element.get("entityUrn"),
        "objectUrn": element.get("objectUrn"),
        "memberId": _urn_id(element.get("objectUrn")),
        "premium": element.get("premium"),
        "openLink": element.get("openLink"),
        "saved": element.get("saved"),
        "viewed": element.get("viewed"),
        "pendingInvitation": element.get("pendingInvitation"),
        "memorialized": element.get("memorialized"),
        "blockThirdPartyDataSharing": element.get("blockThirdPartyDataSharing"),
        "listCount": element.get("listCount"),
        "profilePictureUrl": _image_url(element.get("profilePictureDisplayImage")),
        "recipeType": element.get("$recipeType"),
        # Primary current position, denormalized for flat access. The element-
        # level fallbacks cover older/other payload shapes that put these at
        # the top level.
        "title": primary.get("title") or _text(element.get("title")),
        "companyName": primary.get("companyName") or _text(element.get("companyName")),
        "companyUrn": primary.get("companyUrn") or element.get("companyUrn"),
        "companyId": primary.get("companyId") or _urn_id(element.get("companyUrn")),
        "companyIndustry": primary.get("companyIndustry")
        or _text(element.get("industry")),
        "companyLocation": primary.get("companyLocation"),
        "positionStartYear": primary.get("startYear"),
        "positionStartMonth": primary.get("startMonth"),
        "tenureCompanyMonths": primary.get("tenureCompanyMonths"),
        "tenurePositionMonths": primary.get("tenurePositionMonths"),
    }
    record = {k: v for k, v in record.items() if v is not None}
    if positions:
        record["positions"] = positions
    badges = _badges(element)
    if badges:
        record["badges"] = badges
    if include_raw:
        record["_raw"] = element
    return record


def _employee_range(element: dict[str, Any]) -> str | None:
    value = element.get("employeeCountRange")
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        start, end = value.get("start"), value.get("end")
        if start is not None and end is not None:
            return f"{start}-{end} employees"
        if start is not None:
            return f"{start}+ employees"
    count = element.get("employeeCount")
    if isinstance(count, (int, str)) and str(count):
        return str(count)
    return None


def normalize_account(
    element: dict[str, Any], *, include_raw: bool = True
) -> dict[str, Any]:
    """Map one account element completely: scalars, badges, raw."""
    record: dict[str, Any] = {
        "companyName": _text(element.get("companyName")) or _text(element.get("name")),
        "industry": _text(element.get("industry")),
        "employeeCountRange": _employee_range(element),
        "employeeDisplayCount": _text(element.get("employeeDisplayCount")),
        "description": _text(element.get("description")),
        "entityUrn": element.get("entityUrn"),
        "companyId": _urn_id(element.get("entityUrn")),
        "logoUrl": _image_url(element.get("companyPictureDisplayImage")),
        "listCount": element.get("listCount"),
        "saved": element.get("saved"),
        "recipeType": element.get("$recipeType"),
    }
    record = {k: v for k, v in record.items() if v is not None}
    badges = _badges(element)
    if badges:
        record["badges"] = badges
    if include_raw:
        record["_raw"] = element
    return record


def normalize(
    elements: list[dict[str, Any]],
    scraper_type: str,
    *,
    include_raw: bool = True,
) -> list[dict[str, Any]]:
    """Normalize a page of elements. ``_raw`` is kept by default so the store
    always persists the complete element; strip it at the presentation layer,
    not here."""
    fn = normalize_person if scraper_type == "contacts" else normalize_account
    return [fn(e, include_raw=include_raw) for e in elements]

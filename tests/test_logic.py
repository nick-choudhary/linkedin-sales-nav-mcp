"""Unit tests for the pure logic: URL validation + JSON normalization.

No browser and no network — these exercise the parts that don't need a live
Sales Navigator session. REAL_LEAD / REAL_ACCOUNT mirror the exact shape of
captured LeadSearchResult / AccountSearchResult elements, with every
identifying value replaced by synthetic data — so the full-mapping tests are
grounded in LinkedIn's real structure without embedding anyone's information.
"""

import pytest

from sales_nav_mcp.capture import validate_sales_nav_url
from sales_nav_mcp.exceptions import UrlValidationError
from sales_nav_mcp.normalize import (
    find_paging,
    find_search_elements,
    normalize,
    normalize_account,
    normalize_person,
)

PEOPLE_URL = "https://www.linkedin.com/sales/search/people?query=(filters:List())"
ACCOUNTS_URL = "https://www.linkedin.com/sales/search/accounts?query=(filters:List())"

# Mirrors a captured LeadSearchResult element; all values synthetic.
REAL_LEAD = {
    "$recipeType": "com.linkedin.sales.deco.desktop.searchv2.LeadSearchResult",
    "fullName": "Jordan Rivera",
    "firstName": "Jordan",
    "lastName": "Rivera",
    "geoRegion": "New York, New York, United States",
    "summary": "WHAT WE DO...",
    "degree": 2,
    "entityUrn": "urn:li:fs_salesProfile:(ACwAAAA1B2C3,NAME_SEARCH,ab12)",
    "objectUrn": "urn:li:member:100000001",
    "premium": True,
    "openLink": False,
    "saved": False,
    "viewed": False,
    "pendingInvitation": False,
    "memorialized": False,
    "blockThirdPartyDataSharing": False,
    "listCount": 0,
    "trackingId": "jÏ...",
    "profilePictureDisplayImage": {
        "rootUrl": "https://media.licdn.com/dms/image/v2/X/profile-",
        "artifacts": [
            {"width": 100, "height": 100, "fileIdentifyingUrlPathSegment": "100_100/a"},
            {"width": 800, "height": 800, "fileIdentifyingUrlPathSegment": "800_800/a"},
        ],
    },
    "currentPositions": [
        {
            "$recipeType": "com.linkedin.sales.deco.common.profile.DecoratedPosition",
            "title": "Chief Executive Officer",
            "companyName": "Northwind Studio",
            "companyUrn": "urn:li:fs_salesCompany:900001",
            "current": True,
            "posId": 1,
            "description": "Jordan is CEO...",
            "startedOn": {"year": 2009, "month": 1},
            "tenureAtCompany": {"numYears": 17, "numMonths": 8},
            "tenureAtPosition": {"numYears": 17, "numMonths": 8},
            "companyUrnResolutionResult": {
                "entityUrn": "urn:li:fs_salesCompany:900001",
                "name": "Northwind Studio",
                "industry": "Software Development",
                "location": "New York, New York, United States",
                "companyPictureDisplayImage": {
                    "rootUrl": "https://media.licdn.com/dms/image/v2/Y/company-logo_",
                    "artifacts": [
                        {
                            "width": 400,
                            "height": 400,
                            "fileIdentifyingUrlPathSegment": "400_400/logo",
                        },
                    ],
                },
            },
        }
    ],
    "spotlightBadges": [
        {
            "id": "SECOND_DEGREE_CONNECTION",
            "displayValue": "3 mutual connections",
            "associatedEntityUrnsUnions": [
                {"profileUrn": "urn:li:fs_salesProfile:(A, , )"},
                {"groupUrn": "urn:li:fs_salesGroup:900003"},
            ],
            "popup": {
                "header": {"text": "Mutual connections"},
                "message": {"text": "Ask Priya for a warm introduction."},
                "config": {"supportsDataFetch": True, "popupTypes": []},
            },
        }
    ],
}

# Mirrors a captured AccountSearchResult element; all values synthetic.
REAL_ACCOUNT = {
    "$recipeType": "com.linkedin.sales.deco.desktop.searchv2.AccountSearchResult",
    "companyName": "Contoso Telecom",
    "industry": "IT Services and IT Consulting",
    "employeeCountRange": "10,001+ employees",
    "employeeDisplayCount": "100K+",
    "description": "Connecting people...",
    "entityUrn": "urn:li:fs_salesCompany:900002",
    "listCount": 0,
    "saved": False,
    "trackingId": "6...",
    "companyPictureDisplayImage": {
        "rootUrl": "https://media.licdn.com/dms/image/v2/Z/company-logo_",
        "artifacts": [
            {
                "width": 200,
                "height": 200,
                "fileIdentifyingUrlPathSegment": "200_200/contoso",
            },
        ],
    },
    "spotlightBadges": [
        {
            "id": "RECENT_FUNDING_EVENT",
            "displayValue": "Series B",
            "popup": {
                "header": {"text": "Recent funding"},
                "config": {"supportsDataFetch": True},
            },
        }
    ],
}


class TestUrlValidation:
    def test_people_ok(self):
        validate_sales_nav_url(PEOPLE_URL, "contacts")

    def test_accounts_ok(self):
        validate_sales_nav_url(ACCOUNTS_URL, "accounts")

    def test_leads_path_ok(self):
        validate_sales_nav_url(
            "https://www.linkedin.com/sales/lists/people/12345", "contacts"
        )

    def test_wrong_tool_points_to_other(self):
        with pytest.raises(UrlValidationError, match="search_accounts"):
            validate_sales_nav_url(ACCOUNTS_URL, "contacts")

    def test_non_linkedin_host(self):
        with pytest.raises(UrlValidationError, match="not a LinkedIn domain"):
            validate_sales_nav_url("https://evil.com/sales/search/people", "contacts")

    def test_regular_search_rejected(self):
        with pytest.raises(UrlValidationError, match="Sales Navigator"):
            validate_sales_nav_url(
                "https://www.linkedin.com/search/results/people/", "contacts"
            )

    def test_bare_query_rejected(self):
        with pytest.raises(UrlValidationError):
            validate_sales_nav_url("sales/search/people", "contacts")


class TestFindElements:
    def test_top_level_elements(self):
        payload = {"elements": [{"firstName": "A", "entityUrn": "urn:1"}]}
        assert len(find_search_elements(payload)) == 1

    def test_nested_under_data(self):
        payload = {"data": {"results": {"elements": [{"companyName": "X"}]}}}
        assert find_search_elements(payload)[0]["companyName"] == "X"

    def test_ignores_metadata_lists(self):
        payload = {"metadata": {"filters": [{"a": 1}, {"b": 2}]}, "elements": []}
        assert find_search_elements(payload) == []

    def test_paging_found_nested(self):
        payload = {"data": {"paging": {"total": 500, "start": 0, "count": 25}}}
        assert find_paging(payload)["total"] == 500


class TestNormalizePersonFullMapping:
    def test_real_element_scalars(self):
        rec = normalize_person(REAL_LEAD)
        assert rec["fullName"] == "Jordan Rivera"
        assert rec["geoRegion"] == "New York, New York, United States"
        assert rec["summary"] == "WHAT WE DO..."
        assert rec["degree"] == 2
        assert rec["memberId"] == 100000001
        assert rec["premium"] is True
        # `openLink` is dead in the search payload (false for everyone),
        # so normalize drops it rather than publishing a misleading flag.
        assert "openLink" not in rec
        assert rec["saved"] is False
        assert rec["viewed"] is False
        assert rec["pendingInvitation"] is False
        assert rec["memorialized"] is False
        assert rec["blockThirdPartyDataSharing"] is False
        assert rec["listCount"] == 0
        assert rec["recipeType"].endswith("LeadSearchResult")
        # trackingId is raw-only by design.
        assert "trackingId" not in rec

    def test_primary_position_denormalized(self):
        rec = normalize_person(REAL_LEAD)
        assert rec["title"] == "Chief Executive Officer"
        assert rec["companyName"] == "Northwind Studio"
        assert rec["companyId"] == 900001
        assert rec["companyIndustry"] == "Software Development"
        assert rec["companyLocation"] == "New York, New York, United States"
        assert rec["positionStartYear"] == 2009
        assert rec["tenureCompanyMonths"] == 17 * 12 + 8

    def test_positions_list_complete(self):
        rec = normalize_person(REAL_LEAD)
        assert len(rec["positions"]) == 1
        pos = rec["positions"][0]
        assert pos["posId"] == 1
        assert pos["current"] is True
        assert pos["description"] == "Jordan is CEO..."
        assert pos["companyLogoUrl"].endswith("400_400/logo")

    def test_badges_mapped(self):
        rec = normalize_person(REAL_LEAD)
        badge = rec["badges"][0]
        assert badge["id"] == "SECOND_DEGREE_CONNECTION"
        assert badge["displayValue"] == "3 mutual connections"
        assert badge["headerText"] == "Mutual connections"
        assert badge["messageText"] == "Ask Priya for a warm introduction."
        assert badge["associatedUrns"] == [
            "urn:li:fs_salesProfile:(A, , )",
            "urn:li:fs_salesGroup:900003",
        ]

    def test_profile_picture_uses_largest_artifact(self):
        rec = normalize_person(REAL_LEAD)
        assert rec["profilePictureUrl"].endswith("800_800/a")

    def test_raw_kept_by_default(self):
        rec = normalize_person(REAL_LEAD)
        assert rec["_raw"] is REAL_LEAD

    def test_raw_stripped_on_request(self):
        rec = normalize_person(REAL_LEAD, include_raw=False)
        assert "_raw" not in rec

    def test_fallbacks_for_flat_shapes(self):
        el = {
            "firstName": "Jane",
            "lastName": "Doe",
            "title": "VP Engineering",
            "companyName": "Acme",
            "geoRegion": "Berlin, Germany",
            "degree": 2,
        }
        rec = normalize_person(el)
        assert rec["fullName"] == "Jane Doe"
        assert rec["title"] == "VP Engineering"
        assert rec["companyName"] == "Acme"
        assert rec["geoRegion"] == "Berlin, Germany"

    def test_wrapped_text_value(self):
        el = {"firstName": {"text": "Kai"}, "lastName": {"text": "Lin"}}
        rec = normalize_person(el)
        assert rec["fullName"] == "Kai Lin"


class TestNormalizeAccountFullMapping:
    def test_real_element(self):
        rec = normalize_account(REAL_ACCOUNT)
        assert rec["companyName"] == "Contoso Telecom"
        assert rec["companyId"] == 900002
        assert rec["industry"] == "IT Services and IT Consulting"
        assert rec["employeeCountRange"] == "10,001+ employees"
        assert rec["employeeDisplayCount"] == "100K+"
        assert rec["description"] == "Connecting people..."
        assert rec["listCount"] == 0
        assert rec["saved"] is False
        assert rec["logoUrl"].endswith("200_200/contoso")
        assert rec["badges"][0]["id"] == "RECENT_FUNDING_EVENT"
        assert rec["_raw"] is REAL_ACCOUNT
        # Phantom fields (never present in real payloads) must not appear.
        assert "websiteUrl" not in rec
        assert "location" not in rec

    def test_employee_range_dict(self):
        el = {
            "companyName": "Globex",
            "employeeCountRange": {"start": 51, "end": 200},
            "entityUrn": "urn:li:fs_salesCompany:99",
        }
        rec = normalize_account(el)
        assert rec["employeeCountRange"] == "51-200 employees"

    def test_employee_range_string(self):
        el = {"name": "Initech", "employeeCountRange": "1001-5000 employees"}
        rec = normalize_account(el)
        assert rec["companyName"] == "Initech"
        assert rec["employeeCountRange"] == "1001-5000 employees"


class TestNormalizeBatch:
    def test_dispatch_by_type(self):
        people = normalize([{"fullName": "A"}], "contacts")
        accounts = normalize([{"companyName": "C"}], "accounts")
        assert people[0]["fullName"] == "A"
        assert accounts[0]["companyName"] == "C"

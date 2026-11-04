"""
Unit tests for the EU Funding & Tenders Portal plugin.

The portal's search API (api.tech.ec.europa.eu/search-api) requires a
specific multipart shape: both the `query` and `languages` form parts must
carry `Content-Type: application/json`, and `languages` must be present or
the API silently returns zero results despite a 200 status. These tests
lock down exactly that shape so a future refactor can't reintroduce the bug.
"""

import copy
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import yaml

from external_sources.plugins import ft_portal_transforms as ft
from external_sources.plugins.ft_portal import _DEFAULT_SPEC_PATH as _SPEC_PATH
from external_sources.plugins.ft_portal import FTPortalPlugin

FIXED_NOW = datetime(2026, 9, 1, tzinfo=UTC)

PORTAL_PAGE = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
    "opportunities/topic-details/{id}"
)
API_PAGE = "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/{id}"

# Shapes copied from a live extraction (JSON-encoded strings inside 1-item lists).
REAL_ACTIONS = json.dumps(
    [
        {
            "types": [
                {
                    "typeOfAction": "HORIZON-TMA-MSCA-Cofund-D HORIZON TMA MSCA Cofund Doctoral programme",
                    "typeOfMGA": [
                        {"id": 43251971, "abbreviation": "HORIZON-AG-UN", "description": "HORIZON Unit Grant"}
                    ],
                }
            ],
            "status": {"id": 31094501, "abbreviation": "Forthcoming", "description": "Forthcoming"},
            "plannedOpeningDate": "2026-12-08",
            "submissionProcedure": {
                "id": 31094504, "abbreviation": "single-stage", "description": "single-stage",
            },
            "deadlineDates": ["2027-04-06"],
        }
    ]
)
REAL_LINKS = json.dumps(
    [
        {
            "callIdentifier": "HORIZON-WIDERA-2026-03",
            "criterionCode": "HORIZON-RIA",
            "criterionDescription": "HORIZON  Research and Innovation Actions",
            "url": "https://ec.europa.eu/research/participants/submission/manage/screen/submission/create-draft/43196?topic=X",
            "startDate": "1768262400000",
            "endDate": "1790208000000",
            "mgaDescription": "HORIZON Action Grant Budget-Based",
            "mgaCode": "HORIZON-AG",
        }
    ]
)


class TestFTPortalQueryBuilding:
    """Test the pure query-construction logic (no network)"""

    def test_query_includes_grant_type_filter(self):
        """Every query must filter to type=1 (grants) — this plugin never wants tenders"""
        plugin = FTPortalPlugin()
        query = plugin._build_query("43108390", ["31094502"])

        must_clauses = query["bool"]["must"]
        type_clause = next(c for c in must_clauses if "terms" in c and "type" in c["terms"])
        assert type_clause["terms"]["type"] == ["1"]

    def test_query_includes_status_filter(self):
        """The status codes passed in must appear verbatim in the query"""
        plugin = FTPortalPlugin()
        query = plugin._build_query("43108390", ["31094501", "31094502"])

        must_clauses = query["bool"]["must"]
        status_clause = next(c for c in must_clauses if "terms" in c and "status" in c["terms"])
        assert status_clause["terms"]["status"] == ["31094501", "31094502"]

    def test_query_includes_framework_programme_filter(self):
        """The programme id passed in must appear verbatim in the query"""
        plugin = FTPortalPlugin()
        query = plugin._build_query("43108390", ["31094502"])

        must_clauses = query["bool"]["must"]
        fw_clause = next(
            c for c in must_clauses if "terms" in c and "frameworkProgramme" in c["terms"]
        )
        assert fw_clause["terms"]["frameworkProgramme"] == ["43108390"]

    def test_query_is_json_serializable(self):
        """The query dict must round-trip through json.dumps without error"""
        plugin = FTPortalPlugin()
        query = plugin._build_query("43108390", ["31094502"])

        # Should not raise
        serialized = json.dumps(query)
        assert "43108390" in serialized


class TestFTPortalDefaults:
    """Test plugin construction defaults"""

    def test_default_statuses_are_open_and_forthcoming(self):
        """Per the agreed scope: Open and Forthcoming, not Closed"""
        plugin = FTPortalPlugin()
        assert set(plugin.statuses) == {"open", "forthcoming"}

    def test_custom_statuses_are_respected(self):
        """A caller can narrow to just one status if they want"""
        plugin = FTPortalPlugin(statuses=["open"])
        assert plugin.statuses == ["open"]

    def test_loads_programmes_from_config(self):
        """The plugin should load the real programme list from the YAML config"""
        plugin = FTPortalPlugin()
        assert "horizon" in plugin.programmes
        assert plugin.programmes["horizon"]["id"] == "43108390"
        assert plugin.programmes["horizon"]["label"] == "Horizon Europe"


FAKE_API_RESPONSE = {
    "apiVersion": "2.155",
    "totalResults": 2,
    "pageNumber": 1,
    "pageSize": 50,
    "results": [
        {
            "metadata": {
                "identifier": ["HORIZON-CL5-2026-09-D4-03"],
                "title": ["Test call about batteries"],
                "status": ["31094502"],
                "deadlineDate": ["2026-09-24T00:00:00.000+0000"],
                "frameworkProgramme": ["43108390"],
                "url": ["https://example.eu/topic/1"],
                "callIdentifier": ["HORIZON-CL5-2026-09"],
                "startDate": ["2026-12-08T00:00:00.000+0000"],
                "descriptionByte": ["<p>Some description</p>"],
            }
        },
        {
            "metadata": {
                "identifier": ["HORIZON-MSCA-2027-COFUND-01-01"],
                "title": ["MSCA COFUND 2027"],
                "status": ["31094501"],
                "deadlineDate": ["2027-04-06T00:00:00.000+0000"],
                "frameworkProgramme": ["43108390"],
                "url": ["https://example.eu/topic/2"],
                "callIdentifier": ["HORIZON-MSCA-2027"],
            }
        },
    ],
}


class TestFTPortalFetchPage:
    """Test the single-page HTTP call, with the network mocked out"""

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_fetch_page_sends_correct_multipart_parts(self, mock_post):
        """Both query and languages parts must be present with application/json content type"""
        mock_response = MagicMock()
        mock_response.json.return_value = FAKE_API_RESPONSE
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        plugin = FTPortalPlugin()
        plugin._fetch_page("43108390", ["31094502"], page_number=1)

        _, kwargs = mock_post.call_args
        files = kwargs["files"]
        assert "query" in files
        assert "languages" in files
        assert files["query"][2] == "application/json"
        assert files["languages"][2] == "application/json"
        assert json.loads(files["languages"][1]) == ["en"]

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_fetch_page_passes_pagination_params(self, mock_post):
        """pageSize and pageNumber must reach the request as query params"""
        mock_response = MagicMock()
        mock_response.json.return_value = FAKE_API_RESPONSE
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        plugin = FTPortalPlugin()
        plugin._fetch_page("43108390", ["31094502"], page_number=2)

        _, kwargs = mock_post.call_args
        assert kwargs["params"]["pageNumber"] == 2
        assert kwargs["params"]["pageSize"] == FTPortalPlugin.PAGE_SIZE
        assert kwargs["params"]["apiKey"] == "SEDIA"

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_fetch_page_returns_parsed_json(self, mock_post):
        """The method should return the response's parsed JSON body"""
        mock_response = MagicMock()
        mock_response.json.return_value = FAKE_API_RESPONSE
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        plugin = FTPortalPlugin()
        result = plugin._fetch_page("43108390", ["31094502"], page_number=1)

        assert result["totalResults"] == 2


class TestFTPortalRecordMapping:
    """Test mapping one raw API result into a NormalizedRecord"""

    def test_maps_open_status_correctly(self):
        """status code 31094502 must map to the human-readable 'open'"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        raw_result = FAKE_API_RESPONSE["results"][0]

        record = plugin._record_from_result(raw_result, "Horizon Europe")

        assert record.status == "open"
        assert record.natural_key == "HORIZON-CL5-2026-09-D4-03"
        assert record.title == "Test call about batteries"
        assert record.programme == "Horizon Europe"
        assert record.deadline_date == "2026-09-24T00:00:00+00:00"
        assert record.source_url == "https://example.eu/topic/1"
        assert record.record_type == "call"
        assert record.domain == "projects"

    def test_maps_forthcoming_status_correctly(self):
        """status code 31094501 must map to the human-readable 'forthcoming'"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        raw_result = FAKE_API_RESPONSE["results"][1]

        record = plugin._record_from_result(raw_result, "Horizon Europe")

        assert record.status == "forthcoming"

    def test_raw_field_preserves_original_metadata(self):
        """raw must carry the untouched metadata dict, for fields this model doesn't cover"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        raw_result = FAKE_API_RESPONSE["results"][0]

        record = plugin._record_from_result(raw_result, "Horizon Europe")

        assert record.raw["callIdentifier"] == ["HORIZON-CL5-2026-09"]

    def test_maps_start_date_and_description(self):
        """startDate and descriptionByte must be unwrapped like every other metadata field"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        raw_result = FAKE_API_RESPONSE["results"][0]

        record = plugin._record_from_result(raw_result, "Horizon Europe")

        assert record.start_date == "2026-12-08T00:00:00+00:00"
        assert record.description == "Some description"

    def test_missing_description_maps_to_none(self):
        """A result with no descriptionByte key must produce description=None"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        raw_result = FAKE_API_RESPONSE["results"][1]

        record = plugin._record_from_result(raw_result, "Horizon Europe")

        assert record.description is None


class TestFTPortalFullFetch:
    """Test the full fetch() loop across programmes and pages"""

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_fetch_returns_records_for_every_configured_programme(self, mock_post):
        """fetch() calls the API once per programme and de-duplicates identical results"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "totalResults": 2,
            "results": FAKE_API_RESPONSE["results"],
        }
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        plugin = FTPortalPlugin(now=FIXED_NOW)
        records = plugin.fetch()

        assert mock_post.call_count == len(plugin.programmes)
        assert len(records) == 2
        assert all(r.domain == "projects" for r in records)

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_fetch_paginates_when_more_results_exist(self, mock_post):
        """If totalResults exceeds one page, fetch() must request the next page too"""
        page_1 = {
            "totalResults": FTPortalPlugin.PAGE_SIZE + 1,
            "results": FAKE_API_RESPONSE["results"],
        }
        distinct = copy.deepcopy(FAKE_API_RESPONSE["results"][0])
        distinct["metadata"]["identifier"] = ["HORIZON-CL5-2026-09-D4-99"]
        page_2 = {
            "totalResults": FTPortalPlugin.PAGE_SIZE + 1,
            "results": [distinct],
        }
        mock_response_1 = MagicMock()
        mock_response_1.json.return_value = page_1
        mock_response_1.raise_for_status.return_value = None
        mock_response_2 = MagicMock()
        mock_response_2.json.return_value = page_2
        mock_response_2.raise_for_status.return_value = None

        plugin = FTPortalPlugin(now=FIXED_NOW)
        # Restrict to a single programme to keep this test's math simple.
        plugin.programmes = {"horizon": plugin.programmes["horizon"]}
        mock_post.side_effect = [mock_response_1, mock_response_2]

        records = plugin.fetch()

        assert mock_post.call_count == 2
        assert len(records) == 3  # 2 from page 1 + 1 from page 2

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_fetch_stops_at_max_pages_safety_cap(self, mock_post):
        """Even if the API claims huge totalResults, fetch() must not loop forever"""
        huge_total_page = {
            "totalResults": 1_000_000,
            "results": FAKE_API_RESPONSE["results"],
        }
        mock_response = MagicMock()
        mock_response.json.return_value = huge_total_page
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        plugin = FTPortalPlugin(now=FIXED_NOW)
        plugin.programmes = {"horizon": plugin.programmes["horizon"]}

        plugin.fetch()

        assert mock_post.call_count == FTPortalPlugin.MAX_PAGES

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_fetch_prints_warning_when_max_pages_cap_hit(self, mock_post, capsys):
        """Hitting the MAX_PAGES cap without collecting all results must be signaled"""
        huge_total_page = {
            "totalResults": 1_000_000,
            "results": FAKE_API_RESPONSE["results"],
        }
        mock_response = MagicMock()
        mock_response.json.return_value = huge_total_page
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        plugin = FTPortalPlugin(now=FIXED_NOW)
        plugin.programmes = {"horizon": plugin.programmes["horizon"]}

        plugin.fetch()

        captured = capsys.readouterr()
        assert "[WARN]" in captured.out
        assert "Horizon Europe" in captured.out
        assert f"MAX_PAGES cap ({FTPortalPlugin.MAX_PAGES})" in captured.out


def rich_metadata(**overrides):
    """A minimal 'rich family' metadata dict (as the F&T API returns it)."""
    budget = json.dumps(
        {
            "budgetYearsColumns": ["2027"],
            "budgetTopicActionMap": {
                "1": [
                    {
                        "action": "HORIZON-A-01 - Type X",
                        "expectedGrants": 2,
                        "minContribution": 1000000,
                        "maxContribution": 3000000,
                        "budgetYearMap": {"2027": "5000000"},
                        "plannedOpeningDate": "2026-12-08",
                        "deadlineDates": ["2027-04-06"],
                    }
                ],
                "2": [
                    {
                        "action": "HORIZON-B-02 - Type Y",
                        "expectedGrants": 9,
                        "budgetYearMap": {"2027": "99999999"},
                    }
                ],
            },
        }
    )
    metadata = {
        "identifier": ["HORIZON-A-01"],
        "title": ["  A   title "],
        "status": ["31094502"],
        "deadlineDate": ["2027-04-06T00:00:00.000+0000"],
        "startDate": ["2026-12-08T00:00:00.000+0000"],
        "url": ["https://ec.europa.eu/topic-details/HORIZON-A-01.json"],
        "callIdentifier": ["HORIZON-A-2027"],
        "callTitle": ["A call"],
        "typesOfAction": ["HORIZON RIA"],
        "keywords": ['["ai", "robotics"]'],
        "allowPartnerSearch": ["true"],
        "budgetOverview": [budget],
        "supportInfo": [
            '<p><a href="https://ec.europa.eu/manual" rel="x">Online Manual</a> is your guide.</p>'
        ],
        "topicConditions": ['<div><h4>General conditions</h4><p>Page limit &amp; layout</p></div>'],
        "latestInfos": [
            json.dumps(
                [{"lastChangeDate": "2026-01-20T14:20:17.608", "content": "<p>Updated&nbsp;text</p>"}]
            )
        ],
        "links": ["[]"],
    }
    metadata.update(overrides)
    return metadata


class TestFTPortalFieldSpec:
    """Test that records are prepared through the declarative field spec"""

    KNOWN_RAW_KEYS = {
        "es_SortDate", "keywords", "language", "type", "focusArea", "typeOfMGAs",
        "callIdentifier", "callccm2Id", "DATASOURCE", "frameworkProgramme", "identifier",
        "es_ContentType", "programmePeriod", "deadlineDate", "es_Combine", "esST_URL",
        "esDA_QueueDate", "startDate", "deadlineModel", "status", "sortStatus", "title",
        "esST_FileName", "ccm2Id", "programmeDivision", "crossCuttingPriorities",
        "esDA_IngestDate", "typesOfAction", "url", "esST_checksum", "destinationGroup",
        "destination", "ccmTags2", "cftId", "corporate-search-version", "datasource",
        "esIN_ccmTags2", "esIN_ccmTags", "ccmTags", "latestInfos", "links", "callTitle",
        "descriptionByte", "tags", "actions", "supportInfo", "sepTemplate",
        "topicConditions", "REFERENCE", "esDA_FirstIngestDate", "allowPartnerSearch",
        "budgetOverview", "specificObjective", "destinationDetails",
        "destinationDescription", "cenTagsA", "mission", "missionGroup",
    }

    def test_spec_declares_every_known_raw_key(self):
        """All 58 raw keys seen in the live API are mapped or explicitly ignored"""
        plugin = FTPortalPlugin(now=FIXED_NOW)

        missing = self.KNOWN_RAW_KEYS - plugin.field_spec.declared_keys()

        assert missing == set()

    def test_text_fields_are_cleaned_and_links_preserved(self):
        """HTML fields become plain text; their links are kept as data"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        result = {"metadata": rich_metadata()}

        record = plugin._record_from_result(result, "Horizon Europe")

        assert record.details["support"] == "Online Manual is your guide."
        assert record.details["support_links"] == [
            {"text": "Online Manual", "url": "https://ec.europa.eu/manual"}
        ]
        assert record.details["conditions"] == "General conditions\nPage limit & layout"
        assert record.details["latest_updates"] == [
            {"date": "2026-01-20T14:20:17+00:00", "text": "Updated text"}
        ]

    def test_source_url_loses_json_suffix_and_title_is_trimmed(self):
        """A trailing .json is stripped from the URL; the title is trimmed"""
        plugin = FTPortalPlugin(now=FIXED_NOW)

        record = plugin._record_from_result({"metadata": rich_metadata()}, "Horizon Europe")

        assert record.source_url == "https://ec.europa.eu/topic-details/HORIZON-A-01"
        assert record.title == "A title"

    def test_budget_uses_only_the_records_own_action(self):
        """Sibling topics in the budget map must not inflate the total"""
        plugin = FTPortalPlugin(now=FIXED_NOW)

        record = plugin._record_from_result({"metadata": rich_metadata()}, "Horizon Europe")

        assert record.funding_amount == 5000000.0
        assert record.details["budget"] == {
            "currency": "EUR",
            "per_year": {"2027": 5000000.0},
            "total_eur": 5000000.0,
            "expected_grants": 2,
            "min_contribution": 1000000.0,
            "max_contribution": 3000000.0,
            "planned_opening_date": "2026-12-08T00:00:00+00:00",
        }
        assert record.details["deadline_dates"] == ["2027-04-06T00:00:00+00:00"]

    def test_lists_booleans_and_json_encoded_lists(self):
        """Lists, JSON-encoded lists and booleans are decoded"""
        plugin = FTPortalPlugin(now=FIXED_NOW)

        record = plugin._record_from_result({"metadata": rich_metadata()}, "Horizon Europe")

        assert record.details["types_of_action"] == ["HORIZON RIA"]
        assert record.details["keywords"] == ["ai", "robotics"]
        assert record.details["allow_partner_search"] is True

    def test_empty_json_lists_are_not_written(self):
        """A source value of '[]' produces no details entry (source absent)"""
        plugin = FTPortalPlugin(now=FIXED_NOW)

        record = plugin._record_from_result({"metadata": rich_metadata()}, "Horizon Europe")

        assert "links" not in record.details

    def test_actions_and_links_strings_are_deep_cleaned(self):
        """Double spaces inside JSON-encoded actions/links come out clean"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        metadata = rich_metadata(
            actions=[
                json.dumps(
                    [{"types": [{"typeOfAction": "HORIZON  Research  and Innovation "}], "x": "  "}]
                )
            ],
            links=[json.dumps([{"callIdentifier": " Some   link ", "url": "https://x.eu"}])],
        )

        record = plugin._record_from_result({"metadata": metadata}, "Horizon Europe")

        assert record.details["actions"] == [
            {"types": [{"action": "HORIZON Research and Innovation"}]}
        ]
        assert record.details["links"] == [{"call_identifier": "Some link", "url": "https://x.eu"}]

    def test_actions_and_links_are_structured_from_real_shapes(self):
        """Real JSON-in-list payloads become readable structures without opaque ids"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        metadata = rich_metadata(actions=[REAL_ACTIONS], links=[REAL_LINKS])

        record = plugin._record_from_result({"metadata": metadata}, "Horizon Europe")

        assert record.details["actions"] == [
            {
                "types": [
                    {
                        "action": "HORIZON-TMA-MSCA-Cofund-D HORIZON TMA MSCA Cofund Doctoral programme",
                        "grant_type": [{"code": "HORIZON-AG-UN", "description": "HORIZON Unit Grant"}],
                    }
                ],
                "status": "Forthcoming",
                "submission_procedure": "single-stage",
                "planned_opening_date": "2026-12-08T00:00:00+00:00",
                "deadline_dates": ["2027-04-06T00:00:00+00:00"],
            }
        ]
        assert record.details["links"][0]["start_date"] == "2026-01-13T00:00:00+00:00"
        assert record.details["links"][0]["end_date"] == "2026-09-24T00:00:00+00:00"

    def test_description_links_are_kept(self):
        """Links inside descriptionByte are preserved as data next to the text"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        metadata = rich_metadata(
            descriptionByte=['<p>See <a href="https://ec.europa.eu/guide">the guide</a>.</p>']
        )

        record = plugin._record_from_result({"metadata": metadata}, "Horizon Europe")

        assert record.description == "See the guide."
        assert record.details["description_links"] == [
            {"text": "the guide", "url": "https://ec.europa.eu/guide"}
        ]

    def test_description_links_absent_without_anchors(self):
        """No <a> in the description -> no description_links entry"""
        plugin = FTPortalPlugin(now=FIXED_NOW)

        record = plugin._record_from_result(
            {"metadata": rich_metadata(descriptionByte=["<p>plain</p>"])}, "Horizon Europe"
        )

        assert "description_links" not in record.details

    def test_api_path_url_becomes_the_portal_page(self):
        """The API path url is replaced by the browsable topic-details page"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        metadata = rich_metadata(
            url=[
                "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/"
                "HORIZON-A-01.json"
            ]
        )

        record = plugin._record_from_result({"metadata": metadata}, "Horizon Europe")

        assert record.source_url == PORTAL_PAGE.format(id="HORIZON-A-01")

    def test_top_level_targets_beyond_the_old_set_reach_the_record(self, tmp_path):
        """A spec that maps start_date (not in the original hand-wired set) is not dropped"""
        spec_path = tmp_path / "custom_fields.yaml"
        spec_path.write_text(
            "source: custom\n"
            "fields:\n"
            "  identifier: [{to: natural_key, parse: [first, strip]}]\n"
            "  title: [{to: title, parse: [first, strip]}]\n"
            "  url: [{to: source_url, parse: [first, strip]}]\n"
            "  startDate: [{to: start_date, parse: [first, iso_datetime]}]\n"
            "  budget: [{to: funding_amount, parse: [first]}]\n"
        )
        plugin = FTPortalPlugin(spec_path=spec_path, now=FIXED_NOW)
        metadata = {
            "identifier": ["X-1"],
            "title": ["T"],
            "url": ["https://x.eu/1"],
            "startDate": ["2028-01-31T00:00:00.000+0000"],
            "budget": ["1000"],
        }

        record = plugin._record_from_result({"metadata": metadata}, "Horizon Europe")

        assert record.start_date == "2028-01-31T00:00:00+00:00"
        assert record.funding_amount == 1000.0
        assert record.programme == "Horizon Europe"
        assert record.record_type == "call"

    def test_context_natural_key_uses_the_spec_normalization(self):
        """Budget matching works for identifiers with irregular whitespace, because the
        ctx key is normalised exactly like the spec's natural_key rule"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        budget = json.dumps(
            {
                "budgetTopicActionMap": {
                    "1": [{"action": "TOPIC 7 - Type", "budgetYearMap": {"2027": "1000"}}]
                }
            }
        )
        metadata = rich_metadata(identifier=["  TOPIC \t 7  "], budgetOverview=[budget])

        record = plugin._record_from_result({"metadata": metadata}, "Horizon Europe")

        assert record.natural_key == "TOPIC 7"
        assert record.funding_amount == 1000.0

    def test_missing_identifier_gives_empty_natural_key(self):
        """No identifier -> empty ctx/record key, no crash"""
        plugin = FTPortalPlugin(now=FIXED_NOW)

        record = plugin._record_from_result({"metadata": {"title": ["T"], "url": ["https://x.eu"]}}, "X")

        assert record.natural_key == ""

    def test_shipped_spec_declares_its_transform_module(self):
        """The shipped spec brings its own ft_* transforms via transform_modules"""
        text = _SPEC_PATH.read_text()

        assert yaml.safe_load(text)["transform_modules"] == [
            "external_sources.plugins.ft_portal_transforms"
        ]

    def test_stale_open_call_becomes_closed(self):
        """Source 'open' with a past deadline is closed; source label kept"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        metadata = rich_metadata(deadlineDate=["2022-01-19T00:00:00.000+0000"])

        record = plugin._record_from_result({"metadata": metadata}, "Horizon Europe")

        assert record.status == "closed"
        assert record.details["source_status"] == "open"

    def test_call_family_record_without_budget_or_description(self):
        """A record lacking budget/description keys leaves those fields empty"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        metadata = {
            "identifier": ["CALL-1"],
            "title": ["Call one"],
            "status": ["31094501"],
            "deadlineDate": ["2027-01-01T00:00:00.000+0000"],
            "startDate": ["2026-12-01T00:00:00.000+0000"],
            "url": ["https://ec.europa.eu/call/CALL-1"],
        }

        record = plugin._record_from_result({"metadata": metadata}, "LIFE")

        assert record.description is None
        assert record.funding_amount is None
        assert record.status == "forthcoming"
        assert "budget" not in record.details

    def test_multi_cutoff_call_with_future_date_stays_open(self):
        """Several cut-offs: deadline_date is the next upcoming one, not the earliest"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        metadata = rich_metadata(
            deadlineDate=[
                "2022-05-03T00:00:00.000+0000",
                "2026-10-05T00:00:00.000+0000",
                "2027-01-26T00:00:00.000+0000",
            ]
        )

        record = plugin._record_from_result({"metadata": metadata}, "Erasmus+")

        assert record.status == "open"
        assert record.deadline_date == "2026-10-05T00:00:00+00:00"
        assert record.details["deadline_dates"] == [
            "2022-05-03T00:00:00+00:00",
            "2026-10-05T00:00:00+00:00",
            "2027-01-26T00:00:00+00:00",
        ]

    def test_fetch_drops_duplicate_natural_keys(self):
        """Identical records returned twice appear once in the output"""
        with patch("external_sources.plugins.ft_portal.requests.post") as mock_post:
            response = MagicMock()
            response.json.return_value = {
                "totalResults": 2,
                "results": [{"metadata": rich_metadata()}, {"metadata": rich_metadata()}],
            }
            response.raise_for_status.return_value = None
            mock_post.return_value = response
            plugin = FTPortalPlugin(statuses=["open"], now=FIXED_NOW)
            plugin.programmes = {"horizon": plugin.programmes["horizon"]}

            records = plugin.fetch()

        assert len(records) == 1


def _mock_search(mock_post, *results):
    """Make the patched requests.post return one page holding `results`."""
    response = MagicMock()
    response.json.return_value = {
        "totalResults": len(results),
        "results": [{"metadata": m} for m in results],
    }
    response.raise_for_status.return_value = None
    mock_post.return_value = response


def _single_programme_plugin(**kwargs):
    plugin = FTPortalPlugin(now=FIXED_NOW, **kwargs)
    plugin.programmes = {"horizon": plugin.programmes["horizon"]}
    return plugin


class TestFTPortalScopeFilter:
    """fetch() must not let out-of-scope (expired / unknown-status) records leave the plugin"""

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_expired_open_record_is_dropped(self, mock_post):
        live = rich_metadata(identifier=["LIVE-1"])
        dead = rich_metadata(
            identifier=["DEAD-1"], deadlineDate=["2024-03-01T00:00:00.000+0000"]
        )
        _mock_search(mock_post, live, dead)

        records = _single_programme_plugin().fetch()

        assert [r.natural_key for r in records] == ["LIVE-1"]
        assert records[0].status == "open"

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_forthcoming_with_past_deadline_is_dropped(self, mock_post):
        stale = rich_metadata(
            identifier=["STALE-FORTHCOMING"],
            status=["31094501"],
            deadlineDate=["2025-01-15T00:00:00.000+0000"],
        )
        live = rich_metadata(identifier=["LIVE-FORTHCOMING"], status=["31094501"])
        _mock_search(mock_post, stale, live)

        records = _single_programme_plugin().fetch()

        assert [r.natural_key for r in records] == ["LIVE-FORTHCOMING"]
        assert records[0].status == "forthcoming"

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_multi_cutoff_call_with_future_date_is_kept(self, mock_post):
        multi = rich_metadata(
            identifier=["MULTI-1"],
            deadlineDate=[
                "2022-05-03T00:00:00.000+0000",
                "2026-10-05T00:00:00.000+0000",
            ],
        )
        _mock_search(mock_post, multi)

        records = _single_programme_plugin().fetch()

        assert [r.natural_key for r in records] == ["MULTI-1"]
        assert records[0].status == "open"

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_info_line_reports_dropped_count(self, mock_post, capsys):
        dead_1 = rich_metadata(
            identifier=["DEAD-1"], deadlineDate=["2024-03-01T00:00:00.000+0000"]
        )
        dead_2 = rich_metadata(
            identifier=["DEAD-2"], deadlineDate=["2023-03-01T00:00:00.000+0000"]
        )
        _mock_search(mock_post, rich_metadata(identifier=["LIVE-1"]), dead_1, dead_2)

        _single_programme_plugin().fetch()

        out = capsys.readouterr().out
        assert (
            "[INFO] ft_portal: dropped 2 out-of-scope record(s) "
            "(status=closed after deadline check)"
        ) in out

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_no_info_line_when_nothing_is_dropped(self, mock_post, capsys):
        _mock_search(mock_post, rich_metadata(identifier=["LIVE-1"]))

        _single_programme_plugin().fetch()

        assert "out-of-scope" not in capsys.readouterr().out

    @patch("external_sources.plugins.ft_portal.requests.post")
    def test_open_only_scope_also_drops_forthcoming(self, mock_post, capsys):
        open_rec = rich_metadata(identifier=["OPEN-1"])
        forthcoming = rich_metadata(identifier=["FORTH-1"], status=["31094501"])
        _mock_search(mock_post, open_rec, forthcoming)

        records = _single_programme_plugin(statuses=["open"]).fetch()

        assert [r.natural_key for r in records] == ["OPEN-1"]
        assert "dropped 1 out-of-scope record(s) (status=forthcoming" in capsys.readouterr().out

    def test_filter_in_scope_splits_by_derived_status(self):
        plugin = FTPortalPlugin(now=FIXED_NOW)

        def rec(key, status):
            record = plugin._record_from_result(
                {"metadata": rich_metadata(identifier=[key])}, "Horizon Europe"
            )
            record.status = status
            return record

        records = [
            rec("A", "open"),
            rec("B", "closed"),
            rec("C", "forthcoming"),
            rec("D", None),
            rec("E", "closed"),
        ]

        kept, dropped = plugin._filter_in_scope(records)

        assert [r.natural_key for r in kept] == ["A", "C"]
        assert dropped == {"closed": 2, None: 1}

    def test_filter_in_scope_respects_requested_statuses(self):
        plugin = FTPortalPlugin(statuses=["open"], now=FIXED_NOW)
        record = plugin._record_from_result(
            {"metadata": rich_metadata(status=["31094501"])}, "Horizon Europe"
        )

        kept, dropped = plugin._filter_in_scope([record])

        assert kept == []
        assert dropped == {"forthcoming": 1}


CTX = {"record_type": "call", "natural_key": "TOPIC-1", "now": FIXED_NOW}


def budget_of(*actions, key="TOPIC-1"):
    """A budgetOverview dict whose actions all sit under one topic entry."""
    return {"budgetTopicActionMap": {"1": [dict(a, action=f"{key} - Type") for a in actions]}}


class TestFTBudgetTransforms:
    """Direct tests for ft_budget_total / ft_budget_summary"""

    def test_identical_year_maps_are_counted_once(self):
        """Actions repeating the same topic budget must not be summed repeatedly"""
        budget = budget_of(*[{"budgetYearMap": {"2027": "574616274"}}] * 3)

        assert ft.ft_budget_total(budget, CTX) == 574616274.0
        summary = ft.ft_budget_summary(budget, CTX)
        assert summary["total_eur"] == 574616274.0
        assert summary["per_year"] == {"2027": 574616274.0}

    def test_different_year_maps_are_summed(self):
        """Genuinely different budgets of the same record add up"""
        budget = budget_of(
            {"budgetYearMap": {"2027": "330000000"}},
            {"budgetYearMap": {"2027": "58000000"}},
        )

        assert ft.ft_budget_total(budget, CTX) == 388000000.0
        assert ft.ft_budget_summary(budget, CTX)["total_eur"] == 388000000.0

    def test_total_and_summary_agree(self):
        """funding_amount source and details.budget.total_eur are always equal"""
        budget = budget_of(
            {"budgetYearMap": {"2026": "1", "2027": "2"}},
            {"budgetYearMap": {"2026": "1", "2027": "2"}},
            {"budgetYearMap": {"2027": "5"}},
        )

        assert ft.ft_budget_total(budget, CTX) == ft.ft_budget_summary(budget, CTX)["total_eur"] == 8.0

    def test_zero_budget_is_unstated(self):
        """A zero budget yields no total and no summary when nothing else is known"""
        budget = budget_of({"budgetYearMap": {"2027": "0"}})

        assert ft.ft_budget_total(budget, CTX) is None
        assert ft.ft_budget_summary(budget, CTX) is None

    def test_empty_year_map_keeps_other_content(self):
        """Without amounts the summary still carries grants/contributions/opening"""
        budget = budget_of(
            {
                "budgetYearMap": {},
                "expectedGrants": 4,
                "minContribution": 10,
                "maxContribution": 20,
                "plannedOpeningDate": "2026-12-08",
            }
        )

        assert ft.ft_budget_total(budget, CTX) is None
        assert ft.ft_budget_summary(budget, CTX) == {
            "expected_grants": 4,
            "min_contribution": 10.0,
            "max_contribution": 20.0,
            "planned_opening_date": "2026-12-08T00:00:00+00:00",
        }

    def test_sibling_topics_ignored(self):
        """Actions of other topics never count"""
        budget = budget_of({"budgetYearMap": {"2027": "9"}}, key="OTHER-2")

        assert ft.ft_budget_total(budget, CTX) is None
        assert ft.ft_budget_summary(budget, CTX) is None

    def test_malformed_shapes_return_none(self):
        """Non-dict maps/actions never raise"""
        for bad in (
            "text",
            ["x"],
            {"budgetTopicActionMap": "x"},
            {"budgetTopicActionMap": {"1": "x"}},
            {"budgetTopicActionMap": {"1": ["x", 3, None]}},
            {"budgetTopicActionMap": {"1": [{"action": "TOPIC-1 - T", "budgetYearMap": "x"}]}},
            {"budgetTopicActionMap": {"1": [{"action": "TOPIC-1 - T", "budgetYearMap": ["x"]}]}},
        ):
            assert ft.ft_budget_total(bad, CTX) is None
            assert ft.ft_budget_summary(bad, CTX) is None

    def test_non_finite_numbers_are_rejected(self):
        """inf/nan amounts and grants are treated as unstated"""
        budget = budget_of(
            {
                "budgetYearMap": {"2027": "inf", "2028": "nan"},
                "expectedGrants": "inf",
                "minContribution": "nan",
                "maxContribution": "1e999",
            }
        )

        assert ft.ft_budget_total(budget, CTX) is None
        assert ft.ft_budget_summary(budget, CTX) is None


class TestFTLatestInfos:
    """Direct tests for ft_latest_infos"""

    def test_approval_date_only_entry(self):
        """approvalDate is used when lastChangeDate is missing"""
        value = [{"approvalDate": "2025-03-01T00:00:00.000+0000"}]

        assert ft.ft_latest_infos(value, CTX) == [{"date": "2025-03-01T00:00:00+00:00"}]

    def test_html_content_becomes_text(self):
        """HTML content is converted to text"""
        value = [{"lastChangeDate": "2025-03-01", "content": "<p>Hello&nbsp;there</p>"}]

        assert ft.ft_latest_infos(value, CTX) == [
            {"date": "2025-03-01T00:00:00+00:00", "text": "Hello there"}
        ]

    def test_malformed_input_never_raises(self):
        """Non-list input, non-dict items and non-string content are tolerated"""
        assert ft.ft_latest_infos("x", CTX) is None
        assert ft.ft_latest_infos(["x", 1, None, {"content": 5}, {"content": ["a"]}], CTX) is None
        assert ft.ft_latest_infos([{"content": 5, "lastChangeDate": 7}], CTX) is None


class TestFTTopicUrl:
    """Direct tests for ft_topic_url"""

    def test_api_path_becomes_portal_page(self):
        """Both API-path shapes (with and without .json) map to the browsable page"""
        assert ft.ft_topic_url(API_PAGE.format(id="HORIZON-A-01"), CTX) == (
            PORTAL_PAGE.format(id="HORIZON-A-01")
        )
        assert ft.ft_topic_url(API_PAGE.format(id="HORIZON-A-01") + ".json", CTX) == (
            PORTAL_PAGE.format(id="HORIZON-A-01")
        )

    def test_mixed_case_ids_are_preserved(self):
        """The id is copied verbatim (e.g. InnovFund ids)"""
        assert ft.ft_topic_url(API_PAGE.format(id="InnovFund-2026-NZT"), CTX) == (
            PORTAL_PAGE.format(id="InnovFund-2026-NZT")
        )

    def test_portal_page_passes_through(self):
        """A url that is already the portal page is unchanged"""
        url = PORTAL_PAGE.format(id="HORIZON-A-01")

        assert ft.ft_topic_url(url, CTX) == url

    def test_other_urls_lose_only_a_trailing_json(self):
        """Any other url passes through, minus a trailing .json"""
        assert ft.ft_topic_url("https://x.eu/a/b.json", CTX) == "https://x.eu/a/b"
        assert ft.ft_topic_url("https://x.eu/a/b", CTX) == "https://x.eu/a/b"

    def test_non_string_returns_none(self):
        """Non-strings yield None"""
        assert ft.ft_topic_url(None, CTX) is None
        assert ft.ft_topic_url(5, CTX) is None


class TestFTActions:
    """Direct tests for ft_actions"""

    def test_real_shape(self):
        """A live-shaped action keeps readable fields and drops opaque ids"""
        out = ft.ft_actions(json.loads(REAL_ACTIONS), CTX)

        assert out == [
            {
                "types": [
                    {
                        "action": "HORIZON-TMA-MSCA-Cofund-D HORIZON TMA MSCA Cofund Doctoral programme",
                        "grant_type": [{"code": "HORIZON-AG-UN", "description": "HORIZON Unit Grant"}],
                    }
                ],
                "status": "Forthcoming",
                "submission_procedure": "single-stage",
                "planned_opening_date": "2026-12-08T00:00:00+00:00",
                "deadline_dates": ["2027-04-06T00:00:00+00:00"],
            }
        ]
        assert "id" not in json.dumps(out)

    def test_deadlines_sorted_and_deduplicated(self):
        """Several cut-offs come out as a sorted unique ISO list"""
        action = {"deadlineDates": ["2027-04-06", "2026-10-01", "2027-04-06", "bad"]}

        assert ft.ft_actions([action], CTX) == [
            {"deadline_dates": ["2026-10-01T00:00:00+00:00", "2027-04-06T00:00:00+00:00"]}
        ]

    def test_status_falls_back_to_description_and_strings(self):
        """abbreviation wins, then description; plain strings are accepted"""
        action = {"status": {"description": "Open"}, "submissionProcedure": "two-stage"}

        assert ft.ft_actions([action], CTX) == [
            {"status": "Open", "submission_procedure": "two-stage"}
        ]

    def test_empty_keys_omitted_and_nothing_remaining_is_none(self):
        """Empty pieces are dropped; an action with nothing left disappears"""
        assert ft.ft_actions([{"types": [], "status": {}, "deadlineDates": []}], CTX) is None
        assert ft.ft_actions(
            [{"types": [{"typeOfAction": "X", "typeOfMGA": [{"id": 1}]}]}], CTX
        ) == [{"types": [{"action": "X"}]}]

    def test_malformed_shapes_never_raise(self):
        """Wrong types anywhere are tolerated"""
        assert ft.ft_actions("x", CTX) is None
        assert ft.ft_actions({"types": []}, CTX) is None
        assert ft.ft_actions([None, 1, "s", []], CTX) is None
        bad = {
            "types": "x", "status": 5, "submissionProcedure": [1],
            "plannedOpeningDate": {"a": 1}, "deadlineDates": 7,
        }
        assert ft.ft_actions([bad, {"types": [None, {"typeOfMGA": "x"}]}], CTX) is None


class TestFTLinks:
    """Direct tests for ft_links"""

    def test_real_shape(self):
        """A live-shaped link is renamed, epoch millis become ISO UTC"""
        out = ft.ft_links(json.loads(REAL_LINKS), CTX)

        assert out == [
            {
                "call_identifier": "HORIZON-WIDERA-2026-03",
                "criterion_code": "HORIZON-RIA",
                "criterion_description": "HORIZON Research and Innovation Actions",
                "url": json.loads(REAL_LINKS)[0]["url"],
                "mga_code": "HORIZON-AG",
                "mga_description": "HORIZON Action Grant Budget-Based",
                "start_date": "2026-01-13T00:00:00+00:00",
                "end_date": "2026-09-24T00:00:00+00:00",
            }
        ]

    def test_epoch_int_and_date_string_inputs(self):
        """Epoch ints and parseable date strings are both accepted"""
        link = {"url": "https://x.eu", "startDate": 1768262400000, "endDate": "2026-09-24"}

        assert ft.ft_links([link], CTX) == [
            {
                "url": "https://x.eu",
                "start_date": "2026-01-13T00:00:00+00:00",
                "end_date": "2026-09-24T00:00:00+00:00",
            }
        ]

    def test_unparsable_dates_are_omitted(self):
        """Garbage, booleans and absurd epochs drop the date, not the link"""
        link = {"url": "https://x.eu", "startDate": "soon", "endDate": "9" * 40}

        assert ft.ft_links([link, dict(link, startDate=True)], CTX) == [
            {"url": "https://x.eu"},
            {"url": "https://x.eu"},
        ]

    def test_entries_without_a_http_url_are_dropped(self):
        """No url, or a non-http(s) one, drops the entry; nothing left -> None"""
        links = [{"callIdentifier": "A"}, {"url": "javascript:x()"}, {"url": "ftp://x.eu"}]

        assert ft.ft_links(links, CTX) is None

    def test_malformed_shapes_never_raise(self):
        """Wrong types are tolerated"""
        assert ft.ft_links("x", CTX) is None
        assert ft.ft_links([None, 1, "s", {"url": 5}], CTX) is None


class TestFTDeadlineTransforms:
    """Direct tests for ft_pick_deadline / ft_all_dates"""

    DATES = [
        "2027-01-26T00:00:00.000+0000",
        "2022-05-03T00:00:00.000+0000",
        "2026-10-05T00:00:00.000+0000",
    ]

    def test_picks_next_upcoming_as_raw_string(self):
        """The earliest date not in the past wins; the raw string is returned"""
        assert ft.ft_pick_deadline(self.DATES, CTX) == "2026-10-05T00:00:00.000+0000"

    def test_all_past_picks_latest(self):
        """When every cut-off has passed, the latest one is returned"""
        past = ["2020-01-01T00:00:00.000+0000", "2021-06-01T00:00:00.000+0000"]

        assert ft.ft_pick_deadline(past, CTX) == "2021-06-01T00:00:00.000+0000"

    def test_naive_now_is_treated_as_utc(self):
        """A naive ctx['now'] does not break comparisons"""
        ctx = dict(CTX, now=datetime(2026, 9, 1))

        assert ft.ft_pick_deadline(self.DATES, ctx) == "2026-10-05T00:00:00.000+0000"

    def test_unparseable_returns_none(self):
        """No parseable date -> None"""
        assert ft.ft_pick_deadline(["soon", None, 5], CTX) is None
        assert ft.ft_pick_deadline(None, CTX) is None

    def test_scalar_input_accepted(self):
        """A single string works like a one-element list"""
        assert ft.ft_pick_deadline("2026-10-05", CTX) == "2026-10-05"

    def test_all_dates_sorted_and_deduplicated(self):
        """Every cut-off as sorted unique ISO strings"""
        value = self.DATES + ["2026-10-05T00:00:00.000+0000", "bad"]

        assert ft.ft_all_dates(value, CTX) == [
            "2022-05-03T00:00:00+00:00",
            "2026-10-05T00:00:00+00:00",
            "2027-01-26T00:00:00+00:00",
        ]
        assert ft.ft_all_dates(["bad"], CTX) is None


class TestSourceUpdatedAndDetailRules:
    """Test source_updated_at and the detail-injected fields"""

    def test_source_updated_at_comes_from_ingest_date(self):
        """esDA_IngestDate maps to source_updated_at in ISO UTC"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        metadata = rich_metadata(esDA_IngestDate=["2026-03-02T10:39:47.852+0100"])

        record = plugin._record_from_result({"metadata": metadata}, "Horizon Europe")

        assert record.source_updated_at == "2026-03-02T09:39:47+00:00"

    def test_detail_payload_populates_detail_only_fields(self):
        """A `detail` dict in the metadata feeds publication_date, sme, grant_types"""
        plugin = FTPortalPlugin(now=FIXED_NOW)
        metadata = rich_metadata()
        metadata["detail"] = {
            "publicationDateLong": 1765497600000,
            "sme": False,
            "topicMGAs": [
                {"id": 1, "abbreviation": "HORIZON-AG-LS", "description": "HORIZON Lump Sum Grant"}
            ],
        }

        record = plugin._record_from_result({"metadata": metadata}, "Horizon Europe")

        assert record.details["publication_date"] == "2025-12-12T00:00:00+00:00"
        assert record.details["sme"] is False
        assert record.details["grant_types"] == [
            {"code": "HORIZON-AG-LS", "description": "HORIZON Lump Sum Grant"}
        ]

    def test_missing_detail_leaves_detail_fields_absent(self):
        """Without a detail dict those fields are simply not written"""
        plugin = FTPortalPlugin(now=FIXED_NOW)

        record = plugin._record_from_result({"metadata": rich_metadata()}, "Horizon Europe")

        assert "publication_date" not in record.details
        assert "grant_types" not in record.details


class TestDetailEnrichmentInFetch:
    """Test that fetch() enriches only in-scope records and records gaps"""

    class StubDetail:
        """DetailEnricher double keyed by identifier."""

        def __init__(self, results):
            self.results = results
            self.asked: list[str] = []

        def fetch(self, identifier):
            self.asked.append(identifier)
            return self.results[identifier]

    def _plugin(self, detail):
        plugin = FTPortalPlugin(statuses=["open"], now=FIXED_NOW)
        plugin.programmes = {"horizon": plugin.programmes["horizon"]}
        plugin.detail_enricher = detail
        return plugin

    def _patched_post(self, results):
        response = MagicMock()
        response.json.return_value = {"totalResults": len(results), "results": results}
        response.raise_for_status.return_value = None
        return patch("external_sources.plugins.ft_portal.requests.post", return_value=response)

    def test_detail_is_fetched_only_for_in_scope_records(self):
        """Expired records never trigger a detail request"""
        from external_sources.enrichment.detail import DetailResult

        live = rich_metadata()
        expired = rich_metadata(
            identifier=["OLD-1"], deadlineDate=["2022-01-19T00:00:00.000+0000"]
        )
        detail = self.StubDetail(
            {"HORIZON-A-01": DetailResult("fresh", {"sme": False, "publicationDateLong": 1765497600000})}
        )
        with self._patched_post([{"metadata": live}, {"metadata": expired}]):
            records = self._plugin(detail).fetch()

        assert detail.asked == ["HORIZON-A-01"]
        assert [r.natural_key for r in records] == ["HORIZON-A-01"]
        assert records[0].details["sme"] is False
        assert records[0].field_sources["details.sme"] == "detail"
        assert records[0].field_sources["title"] == "api"

    def test_unavailable_detail_records_a_gap_and_keeps_the_record(self):
        """A failed detail fetch adds a data_gaps entry; the record is not dropped"""
        from external_sources.enrichment.detail import DetailResult

        detail = self.StubDetail(
            {"HORIZON-A-01": DetailResult("error", None, "HTTP 503")}
        )
        with self._patched_post([{"metadata": rich_metadata()}]):
            records = self._plugin(detail).fetch()

        assert len(records) == 1
        gap = records[0].data_gaps[0]
        assert gap["field"] == "detail" and gap["kind"] == "detail_unavailable"
        assert "503" in gap["error"]

    def test_stale_detail_is_used_and_flagged(self):
        """A stale cached detail payload is applied and flagged as stale"""
        from external_sources.enrichment.detail import DetailResult

        detail = self.StubDetail(
            {"HORIZON-A-01": DetailResult("error", {"sme": True}, "down")}
        )
        with self._patched_post([{"metadata": rich_metadata()}]):
            records = self._plugin(detail).fetch()

        assert records[0].details["sme"] is True
        assert records[0].data_gaps[0]["kind"] == "detail_stale"

    def test_no_enricher_means_no_detail_and_api_provenance(self):
        """Without an enricher nothing changes except provenance labels"""
        plugin = FTPortalPlugin(now=FIXED_NOW)

        record = plugin._record_from_result({"metadata": rich_metadata()}, "Horizon Europe")

        assert plugin.detail_enricher is None
        assert record.field_sources["title"] == "api"
        assert record.field_sources["details.types_of_action"] == "api"

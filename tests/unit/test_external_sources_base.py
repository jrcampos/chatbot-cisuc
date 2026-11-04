"""
Unit tests for the external_sources shared plugin base — NormalizedRecord,
SourcePlugin, and MissingAPIKeyError.
"""

import pytest
from pydantic import ValidationError

from external_sources.plugins.base import (
    MissingAPIKeyError,
    NormalizedRecord,
    SourcePlugin,
)


class TestNormalizedRecord:
    """Test NormalizedRecord validation and defaults"""

    def test_minimal_valid_record(self):
        """A record with only the required fields should validate"""
        record = NormalizedRecord(
            natural_key="HORIZON-CL5-2026-09-D4-03",
            record_type="call",
            domain="projects",
            title="Test call",
            source_url="https://example.eu/call/1",
            raw={"identifier": ["HORIZON-CL5-2026-09-D4-03"]},
        )

        assert record.natural_key == "HORIZON-CL5-2026-09-D4-03"
        assert record.record_type == "call"
        assert record.programme is None
        assert record.funding_amount is None

    def test_invalid_record_type_rejected(self):
        """record_type must be one of the allowed literal values"""
        with pytest.raises(ValidationError):
            NormalizedRecord(
                natural_key="X",
                record_type="not_a_real_type",
                domain="projects",
                title="Test",
                source_url="https://example.eu",
                raw={},
            )

    def test_missing_required_field_rejected(self):
        """source_url is required — omitting it should fail validation"""
        with pytest.raises(ValidationError):
            NormalizedRecord(
                natural_key="X",
                record_type="call",
                domain="projects",
                title="Test",
                raw={},
            )

    def test_full_record_with_optional_fields(self):
        """All optional fields should be settable"""
        record = NormalizedRecord(
            natural_key="HORIZON-CL5-2026-09-D4-03",
            record_type="call",
            domain="projects",
            programme="Horizon Europe",
            country_scope="EU",
            title="Test call",
            description="A call about testing",
            status="open",
            funding_amount=1_000_000.0,
            start_date=None,
            deadline_date="2026-09-24",
            source_url="https://example.eu/call/1",
            raw={"identifier": ["HORIZON-CL5-2026-09-D4-03"]},
        )

        assert record.programme == "Horizon Europe"
        assert record.deadline_date == "2026-09-24"
        assert record.funding_amount == 1_000_000.0


class TestSourcePlugin:
    """Test the SourcePlugin abstract contract"""

    def test_cannot_instantiate_without_implementing_fetch(self):
        """SourcePlugin is abstract — subclasses must implement fetch()"""

        class IncompletePlugin(SourcePlugin):
            name = "incomplete"

        with pytest.raises(TypeError):
            IncompletePlugin()

    def test_concrete_plugin_can_be_instantiated(self):
        """A subclass implementing fetch() should instantiate cleanly"""

        class ConcretePlugin(SourcePlugin):
            name = "concrete"

            def fetch(self) -> list[NormalizedRecord]:
                return []

        plugin = ConcretePlugin()
        assert plugin.name == "concrete"
        assert plugin.fetch() == []


class TestMissingAPIKeyError:
    """Test the MissingAPIKeyError exception type"""

    def test_is_a_runtime_error(self):
        """MissingAPIKeyError should be catchable as a RuntimeError"""
        with pytest.raises(RuntimeError):
            raise MissingAPIKeyError("KEEP_EU_API_KEY not set")

    def test_carries_message(self):
        """The exception message should be preserved"""
        try:
            raise MissingAPIKeyError("KEEP_EU_API_KEY not set")
        except MissingAPIKeyError as e:
            assert "KEEP_EU_API_KEY" in str(e)


class TestDetailsAndFieldSpec:
    """Test the details field and the optional field_spec on plugins"""

    def test_details_defaults_to_empty_dict(self):
        """details is optional and defaults to an empty dict"""
        record = NormalizedRecord(
            natural_key="K",
            record_type="call",
            domain="projects",
            title="T",
            source_url="https://x.eu",
            raw={},
        )

        assert record.details == {}

    def test_details_are_not_shared_between_records(self):
        """Each record gets its own details dict"""
        kwargs = dict(
            natural_key="K",
            record_type="call",
            domain="projects",
            title="T",
            source_url="https://x.eu",
            raw={},
        )
        a, b = NormalizedRecord(**kwargs), NormalizedRecord(**kwargs)
        a.details["x"] = 1

        assert b.details == {}

    def test_plugins_have_no_field_spec_by_default(self):
        """A plugin without a spec exposes field_spec = None"""

        class Bare(SourcePlugin):
            name = "bare"

            def fetch(self) -> list[NormalizedRecord]:
                return []

        assert Bare().field_spec is None


class TestProvenanceFields:
    """Test the provenance/gap fields added to NormalizedRecord"""

    def _kwargs(self):
        return dict(
            natural_key="K", record_type="call", domain="projects",
            title="T", source_url="https://x.eu", raw={},
        )

    def test_new_fields_default_to_empty(self):
        """source_updated_at is None; field_sources/data_gaps start empty"""
        record = NormalizedRecord(**self._kwargs())

        assert record.source_updated_at is None
        assert record.field_sources == {}
        assert record.data_gaps == []

    def test_new_fields_are_not_shared_between_records(self):
        """Mutating one record's field_sources/data_gaps never leaks to another"""
        a, b = NormalizedRecord(**self._kwargs()), NormalizedRecord(**self._kwargs())
        a.field_sources["description"] = "api"
        a.data_gaps.append({"field": "x"})

        assert b.field_sources == {} and b.data_gaps == []

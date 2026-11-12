"""
Unit tests for external_sources.normalization.spec and .dedupe — the engine
that applies a declarative field spec to a raw source payload.
"""

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from external_sources.normalization import (
    SpecError,
    apply_spec,
    dedupe_by_natural_key,
    load_spec,
)

NOW = datetime(2026, 9, 18, tzinfo=UTC)


def write_spec(tmp_path, text: str):
    """Write a spec YAML into tmp_path and return its path."""
    path = tmp_path / "demo_fields.yaml"
    path.write_text(text)
    return path


VALID = """
source: demo
anchors:
  status_map: &status_map
    mapping: {"1": forthcoming, "2": open}
not_applicable:
  funding_amount: [call]
finalizers: [derive_status]
fields:
  ident: [{to: natural_key, parse: [first, strip]}]
  name:  [{to: title, parse: [first, strip]}]
  state:
    - {to: status, parse: [first, {map: *status_map}]}
    - {to: details.source_status, parse: [first, {map: *status_map}]}
  due:   [{to: deadline_date, parse: [first, iso_datetime]}]
  body:
    - {to: description, parse: [first, html_to_text]}
    - {to: details.body_links, parse: [first, html_links]}
  only_project: [{to: details.proj, parse: [first], applies_to: [project]}]
  junk:  {ignore: "search-engine internal"}
"""


class TestLoadSpec:
    """Test loading and validating a spec"""

    def test_loads_valid_spec(self, tmp_path):
        """A valid spec loads with rules, ignores and finalizers"""
        spec = load_spec(write_spec(tmp_path, VALID))

        assert spec.source == "demo"
        assert set(spec.rules) == {"ident", "name", "state", "due", "body", "only_project"}
        assert spec.ignored == {"junk": "search-engine internal"}
        assert spec.not_applicable == {"funding_amount": ("call",)}
        assert spec.finalizers == ("derive_status",)
        assert spec.declared_keys() == {
            "ident", "name", "state", "due", "body", "only_project", "junk",
        }

    def test_may_be_empty_flag_is_loaded(self, tmp_path):
        """may_be_empty defaults to False and loads True when set"""
        text = VALID.replace(
            "{to: description, parse: [first, html_to_text]}",
            "{to: description, parse: [first, html_to_text], may_be_empty: true}",
        )
        spec = load_spec(write_spec(tmp_path, text))
        by_target = spec.rules_by_target()

        assert by_target["description"][0].may_be_empty is True
        assert by_target["title"][0].may_be_empty is False

    @pytest.mark.parametrize("bad", ['"yes"', "1", "[true]"])
    def test_may_be_empty_must_be_boolean(self, tmp_path, bad):
        """A non-boolean may_be_empty is rejected"""
        text = VALID.replace(
            "{to: description, parse: [first, html_to_text]}",
            "{to: description, parse: [first, html_to_text], may_be_empty: " + bad + "}",
        )
        with pytest.raises(SpecError, match="may_be_empty"):
            load_spec(write_spec(tmp_path, text))

    def test_rules_by_target(self, tmp_path):
        """rules_by_target indexes rules by their target"""
        spec = load_spec(write_spec(tmp_path, VALID))

        assert spec.rules_by_target()["details.source_status"][0].raw_key == "state"

    def test_rule_produces_date(self, tmp_path):
        """A rule whose chain includes iso_datetime is flagged as a date"""
        spec = load_spec(write_spec(tmp_path, VALID))

        assert spec.rules["due"][0].produces_date is True
        assert spec.rules["name"][0].produces_date is False

    def test_rule_produces_date_uses_the_registry_flag(self, tmp_path, monkeypatch):
        """Any date-producing transform (not just iso_datetime) flags the rule and
        satisfies the date-target requirement; a plain transform does not"""
        from external_sources.normalization import transforms as t

        monkeypatch.setattr(t, "_REGISTRY", dict(t._REGISTRY))
        monkeypatch.setattr(t, "_DATE_PRODUCERS", set(t._DATE_PRODUCERS))
        t.transform("demo_all_dates", produces_date=True)(lambda v, c: v)
        t.transform("demo_plain_step")(lambda v, c: v)

        spec = load_spec(
            write_spec(
                tmp_path,
                "fields:\n"
                "  a: [{to: deadline_date, parse: [demo_all_dates]}]\n"
                "  b: [{to: details.b, parse: [demo_plain_step]}]\n",
            )
        )
        assert spec.rules["a"][0].produces_date is True
        assert spec.rules["b"][0].produces_date is False

        with pytest.raises(SpecError, match="date-producing"):
            load_spec(
                write_spec(
                    tmp_path, "fields:\n  a: [{to: start_date, parse: [demo_plain_step]}]\n"
                )
            )

    def test_unknown_transform_rejected(self, tmp_path):
        """An unregistered transform name fails at load time"""
        text = "fields:\n  a: [{to: title, parse: [nope]}]\n"
        with pytest.raises(SpecError, match="nope"):
            load_spec(write_spec(tmp_path, text))

    def test_invalid_target_rejected(self, tmp_path):
        """A target that is neither a known field nor details.<name> fails"""
        text = "fields:\n  a: [{to: bogus, parse: [first]}]\n"
        with pytest.raises(SpecError, match="bogus"):
            load_spec(write_spec(tmp_path, text))

    def test_target_fed_twice_rejected(self, tmp_path):
        """A target may be fed by only one rule"""
        text = (
            "fields:\n"
            "  a: [{to: title, parse: [first]}]\n"
            "  b: [{to: title, parse: [first]}]\n"
        )
        with pytest.raises(SpecError, match="already fed"):
            load_spec(write_spec(tmp_path, text))

    def test_ignore_requires_reason(self, tmp_path):
        """An ignore entry without a reason fails"""
        text = "fields:\n  a: {ignore: ''}\n"
        with pytest.raises(SpecError, match="ignore"):
            load_spec(write_spec(tmp_path, text))

    def test_unknown_finalizer_rejected(self, tmp_path):
        """An unregistered finalizer fails at load time"""
        text = "finalizers: [nope]\nfields:\n  a: {ignore: x}\n"
        with pytest.raises(SpecError, match="nope"):
            load_spec(write_spec(tmp_path, text))

    def test_unknown_top_level_key_rejected(self, tmp_path):
        """Typos in top-level keys are caught"""
        text = "feilds:\n  a: {ignore: x}\n"
        with pytest.raises(SpecError, match="feilds"):
            load_spec(write_spec(tmp_path, text))


class TestApplySpec:
    """Test applying a spec to a raw payload"""

    def setup_method(self):
        """Build a spec once per test"""
        import tempfile
        from pathlib import Path

        self.dir = Path(tempfile.mkdtemp())
        self.spec = load_spec(write_spec(self.dir, VALID))

    def apply(self, raw, record_type="call"):
        """Apply the demo spec with a fixed clock"""
        return apply_spec(self.spec, raw, {"record_type": record_type, "now": NOW})

    def test_maps_top_level_and_details(self):
        """Rules populate top-level fields and details.<name>"""
        out = self.apply(
            {
                "ident": [" K1 "],
                "name": ["Title"],
                "state": ["2"],
                "due": ["2027-01-01T00:00:00.000+0000"],
                "body": ['<p>Hello <a href="https://a.eu">x</a></p>'],
            }
        )

        assert out["fields"]["natural_key"] == "K1"
        assert out["fields"]["title"] == "Title"
        assert out["fields"]["status"] == "open"
        assert out["details"]["source_status"] == "open"
        assert out["fields"]["deadline_date"] == "2027-01-01T00:00:00+00:00"
        assert out["fields"]["description"] == "Hello x"
        assert out["details"]["body_links"] == [{"text": "x", "url": "https://a.eu"}]

    def test_absent_and_empty_values_are_omitted(self):
        """Missing keys and values that become empty are not written"""
        out = self.apply({"ident": ["K1"], "name": [""], "body": []})

        assert "title" not in out["fields"]
        assert "description" not in out["fields"]
        assert out["details"] == {}

    def test_applies_to_gates_by_record_type(self):
        """A rule restricted to another record type is skipped"""
        raw = {"only_project": ["x"]}

        assert "proj" not in self.apply(raw, "call")["details"]
        assert self.apply(raw, "project")["details"]["proj"] == "x"

    def test_unmapped_code_yields_no_value(self):
        """An unknown status code produces no status (visible gap)"""
        out = self.apply({"state": ["999"]})

        assert "status" not in out["fields"]

    def test_derive_status_closes_expired_open_call(self):
        """Source 'open' with a past deadline becomes 'closed'; source kept"""
        out = self.apply({"state": ["2"], "due": ["2022-01-19T00:00:00.000+0000"]})

        assert out["fields"]["status"] == "closed"
        assert out["details"]["source_status"] == "open"

    def test_derive_status_closes_expired_forthcoming_call(self):
        """A forthcoming call whose deadline passed is also closed"""
        out = self.apply({"state": ["1"], "due": ["2020-01-01T00:00:00.000+0000"]})

        assert out["fields"]["status"] == "closed"

    def test_derive_status_keeps_future_open_call(self):
        """A future deadline leaves the source status untouched"""
        out = self.apply({"state": ["2"], "due": ["2099-01-01T00:00:00.000+0000"]})

        assert out["fields"]["status"] == "open"

    def test_derive_status_without_deadline_is_untouched(self):
        """No deadline means nothing to derive from"""
        out = self.apply({"state": ["2"]})

        assert out["fields"]["status"] == "open"


def make(natural_key: str, raw_keys: int):
    """A stand-in record with a natural key and a raw dict of given size."""
    return SimpleNamespace(
        natural_key=natural_key, raw={f"k{i}": i for i in range(raw_keys)}
    )


class TestDedupe:
    """Test dedupe_by_natural_key()"""

    def test_drops_duplicates_keeping_first_seen_order(self):
        """Equal-size duplicates keep the first record"""
        a, b, c = make("A", 2), make("B", 2), make("A", 2)

        kept, dropped = dedupe_by_natural_key([a, b, c])

        assert kept == [a, b]
        assert dropped == 1

    def test_keeps_the_richer_duplicate(self):
        """Among duplicates the one with the most raw keys wins"""
        thin, rich = make("A", 2), make("A", 5)

        kept, dropped = dedupe_by_natural_key([thin, rich])

        assert kept == [rich]
        assert dropped == 1

    def test_empty_keys_are_never_deduped(self):
        """Records without a natural key are all kept (the audit flags them)"""
        kept, dropped = dedupe_by_natural_key([make("", 1), make("", 1)])

        assert len(kept) == 2
        assert dropped == 0


class TestDefectFixes:
    """Tests for defect fixes in load_spec and apply_spec"""

    def test_step_kwargs_validated(self, tmp_path):
        """Step kwargs must match the transform's signature"""
        text = "fields:\n  a: [{to: title, parse: [{get: {pth: x}}]}]\n"
        with pytest.raises(SpecError, match="get"):
            load_spec(write_spec(tmp_path, text))

    def test_duplicate_yaml_keys_rejected(self, tmp_path):
        """Duplicate keys in the YAML are rejected"""
        text = "fields:\n  a: {ignore: x}\n  a: [{to: title, parse: [first]}]\n"
        with pytest.raises(SpecError, match="duplicate key"):
            load_spec(write_spec(tmp_path, text))

    def test_date_target_requires_iso_datetime(self, tmp_path):
        """Date targets (start_date, deadline_date) must have iso_datetime step"""
        text = "fields:\n  d: [{to: deadline_date, parse: [first]}]\n"
        with pytest.raises(SpecError, match="iso_datetime"):
            load_spec(write_spec(tmp_path, text))

    def test_parse_must_be_list(self, tmp_path):
        """parse value must be a list, not a string or dict"""
        text = "fields:\n  a: [{to: title, parse: first}]\n"
        with pytest.raises(SpecError, match="parse.*must be a list"):
            load_spec(write_spec(tmp_path, text))

    def test_not_applicable_must_be_mapping(self, tmp_path):
        """not_applicable must be a dict, not a list"""
        text = "not_applicable: [a, b]\nfields:\n  x: {ignore: y}\n"
        with pytest.raises(SpecError, match="must be a mapping"):
            load_spec(write_spec(tmp_path, text))

    def test_not_applicable_target_cannot_be_fed_by_rule(self, tmp_path):
        """A target in not_applicable cannot also be fed by a rule"""
        text = (
            "fields:\n"
            "  a: [{to: title, parse: [first]}]\n"
            "not_applicable:\n"
            "  title: [call]\n"
        )
        with pytest.raises(SpecError, match="is also fed"):
            load_spec(write_spec(tmp_path, text))

    def test_raw_keys_must_be_strings(self, tmp_path):
        """All field keys must be strings (not numbers or booleans)"""
        # YAML will parse "1:" as integer 1, so we construct this manually
        import tempfile
        from pathlib import Path

        dir = Path(tempfile.mkdtemp())
        path = dir / "bad.yaml"
        path.write_text("fields:\n  1: {ignore: x}\n")
        with pytest.raises(SpecError, match="must be strings"):
            load_spec(path)

    def test_map_mapping_keys_must_be_strings(self, tmp_path):
        """The mapping dict in a 'map' step must have all-string keys"""
        text = (
            "fields:\n"
            "  a: [{to: title, parse: [{map: {mapping: {1: open, 2: closed}}}]}]\n"
        )
        with pytest.raises(SpecError, match="quote the codes"):
            load_spec(write_spec(tmp_path, text))

    def test_finalizers_must_be_list(self, tmp_path):
        """finalizers must be a list, not a string"""
        text = "finalizers: derive_status\nfields:\n  x: {ignore: y}\n"
        with pytest.raises(SpecError, match="must be a list"):
            load_spec(write_spec(tmp_path, text))

    def test_finalizers_must_be_strings(self, tmp_path):
        """All finalizer names must be strings"""
        text = "finalizers: [1, derive_status]\nfields:\n  x: {ignore: y}\n"
        with pytest.raises(SpecError, match="must be strings"):
            load_spec(write_spec(tmp_path, text))

    def test_no_duplicate_finalizers(self, tmp_path):
        """Duplicate finalizers are not allowed"""
        text = "finalizers: [derive_status, derive_status]\nfields:\n  x: {ignore: y}\n"
        with pytest.raises(SpecError, match="duplicate"):
            load_spec(write_spec(tmp_path, text))

    def test_source_must_be_string(self, tmp_path):
        """source, if present, must be a string"""
        text = "source: 123\nfields:\n  x: {ignore: y}\n"
        with pytest.raises(SpecError, match="must be a string"):
            load_spec(write_spec(tmp_path, text))

    def test_derive_status_tolerates_naive_deadline(self, tmp_path):
        """derive_status treats naive deadline as UTC"""
        dir = Path(tempfile.mkdtemp())
        spec_text = """
source: test
finalizers: [derive_status]
fields:
  state: [{to: status, parse: [first]}]
  due: [{to: deadline_date, parse: [first, iso_datetime]}]
"""
        spec = load_spec(write_spec(dir, spec_text))

        # Naive deadline (no timezone) is treated as UTC
        out = apply_spec(spec, {"state": ["open"], "due": ["2020-01-01T00:00:00"]},
                         {"record_type": "call", "now": NOW})
        assert out["fields"]["status"] == "closed"

    def test_derive_status_handles_malformed_deadline_gracefully(self):
        """derive_status leaves status untouched if deadline can't be parsed"""
        from external_sources.normalization.spec import derive_status

        # Directly test derive_status with a malformed deadline
        fields = {"status": "open", "deadline_date": "not-a-valid-date"}
        details = {}
        ctx = {"now": NOW}

        # Should not raise, just leave status untouched
        derive_status(fields, details, ctx)
        assert fields["status"] == "open"

    def test_apply_spec_deep_copies_raw_values(self, tmp_path):
        """Mutating details never mutates the original raw dict"""
        dir = Path(tempfile.mkdtemp())
        spec_text = """
source: test
fields:
  data: [{to: details.parsed, parse: [first]}]
"""
        spec = load_spec(write_spec(dir, spec_text))

        raw_dict = {"inner": "value"}
        raw = {"data": [raw_dict]}
        out = apply_spec(spec, raw, {"record_type": "call"})

        # Mutate the details (first extracts the dict from the list)
        out["details"]["parsed"]["inner"] = "modified"

        # Raw should be untouched
        assert raw_dict["inner"] == "value"

    def test_file_not_found_wrapped_in_spec_error(self, tmp_path):
        """FileNotFoundError is wrapped in SpecError"""
        with pytest.raises(SpecError, match="not found"):
            load_spec(tmp_path / "nonexistent.yaml")


class TestTransformModules:
    """Test the `transform_modules` top-level key"""

    @pytest.fixture
    def plugin_module(self, tmp_path, monkeypatch):
        """An importable module (and a clean registry) that registers `demo_upper`."""
        from external_sources.normalization import transforms as t

        monkeypatch.setattr(t, "_REGISTRY", dict(t._REGISTRY))
        monkeypatch.setattr(t, "_DATE_PRODUCERS", set(t._DATE_PRODUCERS))
        (tmp_path / "demo_transform_mod.py").write_text(
            "from external_sources.normalization.transforms import transform\n\n"
            "@transform('demo_upper')\n"
            "def demo_upper(value, ctx):\n"
            "    return str(value).upper()\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        yield "demo_transform_mod"
        sys.modules.pop("demo_transform_mod", None)

    def test_spec_without_the_module_reports_unknown_transform(self, tmp_path, plugin_module):
        """Without transform_modules the source-specific transform is unknown"""
        text = "fields:\n  a: [{to: title, parse: [first, demo_upper]}]\n"

        with pytest.raises(SpecError, match="demo_upper"):
            load_spec(write_spec(tmp_path, text))

    def test_spec_with_the_module_loads_and_applies(self, tmp_path, plugin_module):
        """transform_modules imports the module first, so its transforms validate"""
        text = (
            f"transform_modules: [{plugin_module}]\n"
            "fields:\n  a: [{to: title, parse: [first, demo_upper]}]\n"
        )

        spec = load_spec(write_spec(tmp_path, text))
        out = apply_spec(spec, {"a": ["hi"]}, {"record_type": "call"})

        assert out["fields"]["title"] == "HI"

    def test_unimportable_module_is_a_spec_error(self, tmp_path):
        """A module that cannot be imported fails loudly"""
        text = "transform_modules: [no_such_module_xyz]\nfields:\n  a: {ignore: x}\n"

        with pytest.raises(SpecError, match="no_such_module_xyz"):
            load_spec(write_spec(tmp_path, text))

    def test_transform_modules_must_be_a_list_of_strings(self, tmp_path):
        """A scalar or a list with non-strings is rejected"""
        for value in ("some.module", "[1, 2]"):
            text = f"transform_modules: {value}\nfields:\n  a: {{ignore: x}}\n"
            with pytest.raises(SpecError, match="transform_modules"):
                load_spec(write_spec(tmp_path, text))


class TestPackageExports:
    """The normalization package re-exports what the audit and plugins need"""

    def test_spec_helpers_are_exported(self):
        """TOP_LEVEL_TARGETS, finalizer, is_valid_target and derive_status come from the package"""
        from external_sources import normalization as n
        from external_sources.normalization import spec

        assert n.TOP_LEVEL_TARGETS is spec.TOP_LEVEL_TARGETS
        assert n.finalizer is spec.finalizer
        assert n.is_valid_target is spec.is_valid_target
        assert n.derive_status is spec.derive_status
        assert {"TOP_LEVEL_TARGETS", "finalizer", "is_valid_target", "derive_status"} <= set(
            n.__all__
        )


class TestSourceUpdatedAtTarget:
    """Test the source_updated_at top-level target"""

    def test_target_is_valid_and_requires_a_date_step(self, tmp_path):
        """source_updated_at needs a date-producing step, like the other date targets"""
        bad = write_spec(tmp_path, "fields:\n  a: [{to: source_updated_at, parse: [first]}]\n")
        with pytest.raises(SpecError, match="source_updated_at"):
            load_spec(bad)

        good = write_spec(
            tmp_path, "fields:\n  a: [{to: source_updated_at, parse: [first, iso_datetime]}]\n"
        )
        assert "source_updated_at" in load_spec(good).rules_by_target()

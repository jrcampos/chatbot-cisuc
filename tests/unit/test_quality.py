"""
Unit tests for external_sources.quality.
"""

from datetime import UTC, datetime


from external_sources.plugins.base import NormalizedRecord
from external_sources.quality import (
    LogNotifier,
    compute_gaps,
    is_filled,
    load_quality_policy,
)

NOW = datetime(2026, 9, 30, tzinfo=UTC)
POLICY = {
    "required_fields": ["description", "funding_amount"],
    "coverage_floor": {"description": 0.9},
    "min_records_ratio": 0.7,
}


def rec(key="K1", programme="P1", **kwargs):
    """A call record."""
    return NormalizedRecord(
        natural_key=key, record_type="call", domain="projects", programme=programme,
        title="T", source_url="https://x.eu/" + key, raw={}, **kwargs,
    )


class TestPrimitives:
    """Test is_filled/compute_gaps"""

    def test_is_filled(self):
        """None, empty containers and blank strings are unfilled; 0 and False are filled"""
        assert not is_filled(None) and not is_filled("") and not is_filled([]) and not is_filled({})
        assert is_filled(0) and is_filled(False) and is_filled("x")



    def test_compute_gaps_lists_only_empty_required_fields(self):
        """Filled fields produce no gap; empty ones record what was tried"""
        record = rec(description="text")

        gaps = compute_gaps(record, ["description", "funding_amount"], ["api", "documents"], "no_document_available")

        assert gaps == [
            {"field": "funding_amount", "kind": "missing_required",
             "tried": ["api", "documents"], "reason": "no_document_available"}
        ]

    def test_compute_gaps_reads_details_fields(self):
        """details.<name> is looked up in record.details"""
        record = rec(details={"budget": {"a": 1}})

        assert compute_gaps(record, ["details.budget"], [], "x") == []
        assert compute_gaps(rec(), ["details.budget"], [], "x")[0]["field"] == "details.budget"


class TestRequiredAny:
    """Test any-of requirements (budget may live at topic, table-row or call level)"""

    MEMBERS = ["funding_amount", "details.topic_budget_eur", "details.call_budget_eur"]

    def test_any_filled_member_satisfies_the_label(self):
        """A call-level budget alone means the record has budget information"""
        record = rec(details={"call_budget_eur": 4_000_000.0})

        assert compute_gaps(record, [], [], "x", {"budget": self.MEMBERS}) == []

    def test_no_member_filled_is_one_gap_naming_the_alternatives(self):
        """Nothing at any level -> a single missing_required gap listing the members"""
        gaps = compute_gaps(rec(), [], ["api"], "no_document_available", {"budget": self.MEMBERS})

        assert gaps == [
            {"field": "budget", "kind": "missing_required", "tried": ["api"],
             "reason": "no_document_available", "any_of": self.MEMBERS}
        ]














ALERTS = [{"level": "warn", "code": "coverage_drop", "message": "description: 99.0% -> 80.0%"}]


class TestNotifiers:
    """Test notifiers"""

    def test_log_notifier_prints_and_appends(self, tmp_path, capsys):
        """LogNotifier prints [ALERT] lines and appends them to the log file"""
        log = tmp_path / "alerts.log"

        assert LogNotifier(log).notify(ALERTS, "ft_portal") is True

        assert "[ALERT:warn] ft_portal: description: 99.0% -> 80.0%" in capsys.readouterr().out
        assert "coverage_drop" in log.read_text()

    def test_no_alerts_is_a_noop(self, capsys):
        """Nothing to report prints nothing"""
        LogNotifier().notify([], "ft_portal")

        assert capsys.readouterr().out == ""






class TestPolicyLoading:
    """Test load_quality_policy()"""

    def test_shipped_policy_has_required_fields(self):
        """The bundled ft_portal policy declares required fields and floors"""
        policy = load_quality_policy("ft_portal")

        assert "description" in policy["required_fields"]
        assert policy["coverage_floor"]["title"] == 1.0
        assert "details.call_budget_eur" in policy["required_any"]["budget"]

    def test_unknown_plugin_raises(self):
        """Asking for a plugin without a policy is an error"""
        try:
            load_quality_policy("nope")
        except KeyError as exc:
            assert "nope" in str(exc)
        else:
            raise AssertionError("expected KeyError")


def test_alert_notifier_writes_alerts_log(tmp_path):
    from external_sources.quality import alert_notifier
    assert alert_notifier(tmp_path).notify(ALERTS, "ft_portal") is True
    assert "ft_portal" in (tmp_path / "alerts.log").read_text()


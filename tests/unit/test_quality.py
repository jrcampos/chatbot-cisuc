"""
Unit tests for external_sources.quality.
"""

from datetime import UTC, datetime

import requests

from external_sources.plugins.base import NormalizedRecord
from external_sources.quality import (
    CompositeNotifier,
    LogNotifier,
    WebhookNotifier,
    compute_gaps,
    is_filled,
    load_quality_policy,
    notifier_from_env,
    source_of,
)

NOW = datetime(2026, 9, 30, tzinfo=UTC)
POLICY = {
    "required_fields": ["description", "funding_amount"],
    "coverage_floor": {"description": 0.9},
    "max_drop_pp": 2.0,
    "min_records_ratio": 0.7,
    "max_document_failure_ratio": 0.25,
}


def rec(key="K1", programme="P1", **kwargs):
    """A call record."""
    return NormalizedRecord(
        natural_key=key, record_type="call", domain="projects", programme=programme,
        title="T", source_url="https://x.eu/" + key, raw={}, **kwargs,
    )


class TestPrimitives:
    """Test is_filled/source_of/compute_gaps"""

    def test_is_filled(self):
        """None, empty containers and blank strings are unfilled; 0 and False are filled"""
        assert not is_filled(None) and not is_filled("") and not is_filled([]) and not is_filled({})
        assert is_filled(0) and is_filled(False) and is_filled("x")

    def test_source_of_strips_document_id(self):
        """document:<id> is reported as 'document'"""
        record = rec(funding_amount=1.0, field_sources={"funding_amount": "document:abc"})

        assert source_of(record, "funding_amount") == "document"

    def test_source_of_unknown_when_untracked(self):
        """A filled field without provenance is 'unknown'"""
        assert source_of(rec(funding_amount=1.0), "funding_amount") == "unknown"

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












class FakeSession:
    """Session double for the webhook."""

    def __init__(self, error=None):
        self.error = error
        self.posts: list[dict] = []

    def post(self, url, json=None, timeout=None):
        if self.error:
            raise self.error
        self.posts.append({"url": url, "json": json})


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

    def test_webhook_posts_a_text_summary(self):
        """WebhookNotifier posts one JSON message with the alert lines"""
        session = FakeSession()

        assert WebhookNotifier("https://hooks.example/x", session=session).notify(ALERTS, "ft_portal")

        assert session.posts[0]["url"] == "https://hooks.example/x"
        assert "description: 99.0% -> 80.0%" in session.posts[0]["json"]["text"]

    def test_webhook_failure_never_raises(self):
        """A failing webhook returns False and does not break the run"""
        session = FakeSession(error=requests.ConnectionError("down"))

        assert WebhookNotifier("https://hooks.example/x", session=session).notify(ALERTS, "p") is False

    def test_composite_calls_every_notifier(self):
        """CompositeNotifier fans out and reports overall success"""
        calls = []

        class Spy:
            def notify(self, alerts, plugin):
                calls.append(plugin)
                return True

        assert CompositeNotifier([Spy(), Spy()]).notify(ALERTS, "p") is True
        assert calls == ["p", "p"]

    def test_notifier_from_env_uses_webhook_only_when_configured(self, tmp_path):
        """ALERT_WEBHOOK_URL adds a webhook; otherwise logging only"""
        plain = notifier_from_env(tmp_path, env={})
        hooked = notifier_from_env(tmp_path, env={"ALERT_WEBHOOK_URL": "https://hooks.example/x"})

        assert isinstance(plain, LogNotifier)
        assert isinstance(hooked, CompositeNotifier)


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






class TestWebhookStatus:
    """HTTP errors from the webhook are failures"""

    def test_status_400_or_more_is_a_failure(self):
        class Resp:
            status_code = 500

        class Session:
            def post(self, *a, **k):
                return Resp()

        assert WebhookNotifier("https://h.example/x", session=Session()).notify(ALERTS, "p") is False

    def test_status_200_is_success(self):
        class Resp:
            status_code = 204

        class Session:
            def post(self, *a, **k):
                return Resp()

        assert WebhookNotifier("https://h.example/x", session=Session()).notify(ALERTS, "p") is True



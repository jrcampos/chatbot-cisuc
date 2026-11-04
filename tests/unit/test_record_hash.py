"""
Unit tests for record_hash (content hash used for change detection in the store).
"""

from external_sources.plugins.base import NormalizedRecord, record_hash


def rec(key="K1", title="T", **kwargs):
    """A minimal call record."""
    return NormalizedRecord(
        natural_key=key, record_type="call", domain="projects", title=title,
        source_url="https://x.eu/" + key, raw=kwargs.pop("raw", {}), **kwargs,
    )


class TestRecordHash:
    """Test record_hash()"""

    def test_same_content_same_hash(self):
        """Equal content hashes equally"""
        assert record_hash(rec()) == record_hash(rec())

    def test_content_change_changes_hash(self):
        """A changed field changes the hash"""
        assert record_hash(rec(title="A")) != record_hash(rec(title="B"))

    def test_details_change_changes_hash(self):
        """A changed details entry changes the hash"""
        assert record_hash(rec(details={"a": 1})) != record_hash(rec(details={"a": 2}))

    def test_raw_provenance_and_gaps_are_ignored(self):
        """raw, field_sources and data_gaps do not affect the hash"""
        a = rec()
        b = rec(raw={"noise": 1}, field_sources={"title": "api"}, data_gaps=[{"field": "x"}])
        assert record_hash(a) == record_hash(b)


def test_hash_ignores_sources_and_verification_annotations():
    """details.sources / details.verification carry run timestamps; they are not content changes"""
    base = dict(natural_key="K", record_type="call", domain="projects", title="T", source_url="u", raw={})
    plain = NormalizedRecord(**base, details={"call_budget_eur": 1.0})
    annotated = NormalizedRecord(**base, details={
        "call_budget_eur": 1.0, "sources": [{"retrieved_at": "2026-10-14"}],
        "verification": {"title": {"checked_at": "2026-10-14"}},
    })

    assert record_hash(plain) == record_hash(annotated)

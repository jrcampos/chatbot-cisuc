"""
Unit tests for external_sources.documents.profiles.
"""

import pytest
import yaml

from external_sources.documents.profiles import DEFAULT_EU_PROGRAMMES, load_profiles


def write(tmp_path, programmes, profiles):
    """Write tmp programme/profile files and return their paths."""
    (tmp_path / "prog.yaml").write_text(yaml.safe_dump({"programmes": programmes}))
    (tmp_path / "prof.yaml").write_text(yaml.safe_dump(profiles))
    return tmp_path / "prof.yaml", tmp_path / "prog.yaml"


PROG = {"a": {"id": "1", "label": "Alpha"}, "b": {"id": "2", "label": "Beta"}}


def valid(**override):
    """A valid profiles document for PROG."""
    doc = {
        "defaults": {"families": {"call_specific": ["f1"]}},
        "profiles": {"a": {}, "b": {"families": {"call_specific": []}}},
    }
    doc.update(override)
    return doc


class TestRealFiles:
    """Test the shipped configuration"""

    def test_every_programme_resolves_by_label_and_key(self):
        """All 23 programmes have a profile reachable by label and by key"""
        registry = load_profiles()
        programmes = yaml.safe_load(DEFAULT_EU_PROGRAMMES.read_text())["programmes"]

        assert len(programmes) == 23
        for key, body in programmes.items():
            assert registry.for_programme(key) is not None
            assert registry.for_programme(body["label"]) is registry.for_programme(key)
        assert registry.for_programme("nonexistent") is None


class TestLoading:
    """Test load_profiles() with tmp files"""

    def test_defaults_merge_and_override(self, tmp_path):
        """defaults.families apply unless the profile overrides the role"""
        registry = load_profiles(*write(tmp_path, PROG, valid()))

        assert registry.for_programme("Alpha").families_for("call_specific") == ("f1",)
        assert registry.for_programme("b").families_for("call_specific") == ()
        assert registry.for_programme("b").families_for("programme_level") == ()

    def test_rule_matching(self, tmp_path):
        """A rule needs all of its conditions; text is case-insensitive"""
        doc = valid()
        doc["profiles"]["a"]["classify"] = [{"role": "ignore", "host": "x\\.eu", "text": "annex"}]
        rule = load_profiles(*write(tmp_path, PROG, doc)).for_programme("a").classify[0]

        assert rule.matches("https://x.eu/a", "ANNEX 1")
        assert not rule.matches("https://y.eu/a", "ANNEX 1")
        assert not rule.matches("https://x.eu/a", "other")

    @pytest.mark.parametrize(
        "mutate, message",
        [
            (lambda d: d["profiles"].update(z={}), "unknown programmes"),
            (lambda d: d["profiles"]["a"].update(bogus=1), "unknown keys"),
            (lambda d: d["profiles"]["a"].update(families={"boilerplate": ["f"]}), "family role"),
            (lambda d: d["profiles"]["a"].update(classify=[{"role": "weird", "url": "x"}]), "rule role"),
            (lambda d: d["profiles"]["a"].update(classify=[{"role": "other", "url": "("}]), "invalid regex"),
            (lambda d: d["profiles"]["a"].update(classify=[{"role": "other", "zzz": "x"}]), "unknown classify"),
            (lambda d: d["profiles"]["a"].update(skip=["("]), "invalid regex"),
            (lambda d: d.update(extra=1), "unknown top-level"),
        ],
    )
    def test_invalid_files_are_rejected(self, tmp_path, mutate, message):
        """Each schema violation raises ValueError naming the problem"""
        doc = valid()
        mutate(doc)

        with pytest.raises(ValueError, match=message):
            load_profiles(*write(tmp_path, PROG, doc))


def test_shipped_horizon_profile_fetches_the_eurohpc_work_programme():
    """The EuroHPC JU work programme (a Governing Board decision on the JU site) is programme-level"""
    from external_sources.documents.policy import load_policy
    from external_sources.documents.roles import classify_url

    profile = load_profiles().for_programme("Horizon Europe")
    url = ("https://www.eurohpc-ju.europa.eu/document/download/b759cd90_en?"
           "filename=Decision%2022.2026_Amend.%203%20%20to%20Work%20Programme%202026.pdf")
    role = classify_url(url, "HORIZON-JU-EUROHPC-2026-QML-07-01", "HORIZON-JU-EUROHPC-2026-QML-07",
                        load_policy(), text="EuroHPC Work Programme 2026 (Decision 22/2026 Amendment No 3)",
                        profile=profile)
    about = classify_url("https://www.eurohpc-ju.europa.eu/about/discover-eurohpc-ju_en", "X", "Y",
                         load_policy(), text="Discover EuroHPC JU", profile=profile)

    assert role == "programme_level" and about != "programme_level"


def test_programme_without_entry_uses_the_defaults(tmp_path):
    doc = valid()
    doc["profiles"].pop("b")
    registry = load_profiles(*write(tmp_path, PROG, doc))
    assert registry.for_programme("b").families == {"call_specific": ("f1",)}


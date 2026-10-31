"""
Unit tests for the sparse-data guard in process_single_user (enhancement module).
Regression test for empty bio/interests reaching the LLM and risking hallucination.
"""

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("WORKSPACE", "/tmp/cisuc-test-workspace")
os.environ.setdefault("ENHANCEMENT_MODEL", "test-model")
os.environ.setdefault("ENHANCEMENT_MAX_WORKERS", "1")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_PROVIDER", "openai")

from preprocessing.enhancement import enhancement


def make_user(bio, interests):
    return {"id": "1", "name": "Test User", "bio": bio, "research_interests": interests}


@pytest.mark.unit
class TestShortDescriptionGuard:
    @pytest.mark.parametrize("bio,interests", [("", None), (None, None), ("   ", [])])
    def test_empty_bio_and_interests_skips_llm_call(self, monkeypatch, bio, interests):
        mock_llm = Mock()
        monkeypatch.setattr(enhancement, "llm_cliente", mock_llm)

        user = make_user(bio, interests)
        result = enhancement.process_single_user(user, {}, {})

        mock_llm.invoke.assert_not_called()
        assert result["short_description"] == "No biography or research interests available for this researcher."

    def test_real_bio_still_calls_llm(self, monkeypatch):
        mock_llm = Mock()
        mock_llm.invoke.return_value = Mock(content="A concise summary.")
        monkeypatch.setattr(enhancement, "llm_cliente", mock_llm)

        user = make_user("Works on evolutionary computation.", ["AI"])
        result = enhancement.process_single_user(user, {}, {})

        mock_llm.invoke.assert_called()
        assert result["short_description"] == "A concise summary."

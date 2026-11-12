"""
Unit tests for derive_group_project_links in the enhancement module.
Tests deriving group<->project associations from user group/project overlap.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("WORKSPACE", "/tmp/cisuc-test-workspace")
os.environ.setdefault("ENHANCEMENT_MODEL", "test-model")
os.environ.setdefault("ENHANCEMENT_MAX_WORKERS", "1")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from preprocessing.enhancement.enhancement import derive_group_project_links


def make_user(name, acronyms, projects):
    return {
        "name": name,
        "research_groups": [{"name": a.upper(), "acronym": a} for a in acronyms],
        "projects_list": [{"id": pid, "title": title} for pid, title in projects],
    }


class TestDeriveGroupProjectLinks:
    def test_group_to_projects_aggregates_across_shared_group(self):
        users = [
            make_user("Alice", ["cisuc-ai"], [(1, "Project A")]),
            make_user("Bob", ["cisuc-ai"], [(2, "Project B")]),
        ]

        group_to_projects, _ = derive_group_project_links(users)

        assert set(group_to_projects["cisuc-ai"]) == {(1, "Project A"), (2, "Project B")}

    def test_project_to_groups_reverse_mapping(self):
        users = [
            make_user("Alice", ["cisuc-ai"], [(1, "Project A")]),
            make_user("Carol", ["cisuc-se"], [(1, "Project A")]),
        ]

        _, project_to_groups = derive_group_project_links(users)

        assert set(project_to_groups[1]) == {"cisuc-ai", "cisuc-se"}

    def test_user_without_group_contributes_nothing(self):
        users = [make_user("Dave", [], [(3, "Project C")])]

        group_to_projects, project_to_groups = derive_group_project_links(users)

        assert group_to_projects == {}
        assert project_to_groups == {}

    def test_no_duplicate_entries_for_same_pair(self):
        users = [
            make_user("Alice", ["cisuc-ai"], [(1, "Project A")]),
            make_user("Eve", ["cisuc-ai"], [(1, "Project A")]),
        ]

        group_to_projects, project_to_groups = derive_group_project_links(users)

        assert group_to_projects["cisuc-ai"] == [(1, "Project A")]
        assert project_to_groups[1] == ["cisuc-ai"]

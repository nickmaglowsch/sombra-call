"""The repository rulesets in `.github/rulesets/` parse and protect what the docs say."""

import json
from pathlib import Path
from typing import Any

import pytest

RULESETS = Path(__file__).resolve().parents[1] / ".github" / "rulesets"

# GitHub's built-in repository role ids: 5 is Admin (2 Maintain, 4 Write).
ADMIN_ROLE_ID = 5


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RULESETS / name).read_text(encoding="utf-8"))
    return data


def rule_types(ruleset: dict[str, Any]) -> set[str]:
    return {rule["type"] for rule in ruleset["rules"]}


@pytest.mark.parametrize("path", sorted(RULESETS.glob("*.json")), ids=lambda p: p.name)
def test_every_ruleset_has_the_import_shape(path: Path) -> None:
    ruleset = load(path.name)
    assert ruleset["name"]
    assert ruleset["target"] in {"branch", "tag"}
    assert ruleset["enforcement"] == "active"
    assert ruleset["conditions"]["ref_name"]["include"]
    assert isinstance(ruleset["bypass_actors"], list)
    assert ruleset["rules"]


def test_main_ruleset_protects_the_default_branch_without_bypass() -> None:
    ruleset = load("main.json")
    assert ruleset["target"] == "branch"
    assert ruleset["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert ruleset["bypass_actors"] == []
    assert {"deletion", "non_fast_forward", "pull_request", "required_status_checks"} <= (
        rule_types(ruleset)
    )


def test_tag_ruleset_locks_release_tags_to_repository_admins() -> None:
    ruleset = load("tags.json")
    assert ruleset["target"] == "tag"
    assert ruleset["conditions"]["ref_name"] == {"include": ["refs/tags/v*"], "exclude": []}
    assert rule_types(ruleset) == {"creation", "update", "deletion", "non_fast_forward"}
    assert ruleset["bypass_actors"] == [
        {"actor_id": ADMIN_ROLE_ID, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    ]

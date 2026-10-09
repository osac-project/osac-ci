"""The composite action other repositories call (.github/actions/publish): its inputs and the policy it defaults to."""

from __future__ import annotations

import re

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit

ACTION = ROOT / ".github" / "actions" / "publish" / "action.yml"


def _action() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def test_every_input_the_steps_read_is_declared() -> None:
    text = ACTION.read_text(encoding="utf-8")
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", text))
    assert used
    assert used <= set(_action()["inputs"])


def test_declared_inputs_are_all_used() -> None:
    text = ACTION.read_text(encoding="utf-8")
    unused = {name for name in _action()["inputs"] if f"inputs.{name}" not in text}
    assert not unused


def test_default_policy_exists_in_this_repository() -> None:
    assert (ROOT / _action()["inputs"]["policy"]["default"]).is_file()


def test_no_input_is_expanded_into_a_script() -> None:
    """Inputs are user-controlled text: they may only appear in `env:` blocks, never inside a `run:` script."""
    for step in _action()["runs"]["steps"]:
        script = step.get("run", "")
        assert "${{" not in script, f"step {step.get('name')!r} expands an expression into its script"


def test_the_action_never_checks_out_code() -> None:
    assert not any("checkout" in step.get("uses", "") for step in _action()["runs"]["steps"])


# ---- the override action ---------------------------------------------------------------------------------------


OVERRIDE_ACTION = ROOT / ".github" / "actions" / "override" / "action.yml"


def _override() -> dict:
    return yaml.safe_load(OVERRIDE_ACTION.read_text(encoding="utf-8"))


def test_the_override_action_declares_every_input_it_reads_and_uses_every_input_it_declares() -> None:
    text = OVERRIDE_ACTION.read_text(encoding="utf-8")
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", text))
    assert used == set(_override()["inputs"])


def test_the_override_action_never_expands_the_comment_or_anything_else_into_a_script() -> None:
    for step in _override()["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")


def test_the_override_action_checks_out_nothing_and_its_default_policy_exists() -> None:
    assert not any("checkout" in step.get("uses", "") for step in _override()["runs"]["steps"])
    assert (ROOT / _override()["inputs"]["policy"]["default"]).is_file()


def test_the_comment_travels_only_through_the_environment() -> None:
    (step,) = [s for s in _override()["runs"]["steps"] if "run" in s and "env" in s]
    assert step["env"]["OSAC_CI_COMMENT"] == "${{ inputs.comment }}"

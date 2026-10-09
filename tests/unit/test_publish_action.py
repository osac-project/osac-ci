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

"""The manual-status action, and where the decision workflow calls it."""

import re
from pathlib import Path

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit
ACTION = ROOT / ".github" / "actions" / "manual-status" / "action.yml"
DECIDE = ROOT / ".github" / "workflows" / "e2e-pr-decide.yml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_input_used_is_declared_and_every_input_declared_is_used() -> None:
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", ACTION.read_text(encoding="utf-8")))
    assert used == set(load(ACTION)["inputs"])


def test_nothing_is_expanded_into_a_script_and_nothing_is_checked_out() -> None:
    for step in load(ACTION)["runs"]["steps"]:
        assert "${{" not in step.get("run", "")
        assert "checkout" not in step.get("uses", "")


def test_the_number_must_be_digits_and_the_locked_project_of_the_pinned_commit_runs() -> None:
    script = next(s["run"] for s in load(ACTION)["runs"]["steps"] if s["name"] == "Refresh the status")
    assert '"" | *[!0-9]*)' in script and "exit 1" in script
    assert '--locked --project "$root" osac-ci manual-status --repo "$REPO" --number "$NUMBER"' in script


def test_the_auto_merge_job_refreshes_the_status_after_it_has_enabled_auto_merge() -> None:
    job = load(DECIDE)["jobs"]["auto-merge"]
    steps = job["steps"]
    assert [s.get("name") or s["uses"] for s in steps] == [
        "Token of the OSAC CI app",
        "$/.github/actions/auto-merge",
        "$/.github/actions/manual-status",
    ]
    last = steps[-1]
    assert last["continue-on-error"] is True, "a failed refresh must not fail the job"
    assert last["with"]["number"] == "${{ needs.decide.outputs.approved-number }}"
    assert last["with"]["github-token"] == "${{ steps.app-token.outputs.token }}"
    assert last["with"]["edit-token"] == "${{ github.token }}", "the refresh is quiet: it wakes no issue_comment run"
    token = steps[0]["with"]
    assert token["permission-checks"] == "read" and token["permission-issues"] == "write"
    assert token["permission-pull-requests"] == "write"
    assert job["permissions"] == {"pull-requests": "write"}, "only for the quiet edit; auto-merge uses the app token"


def test_both_actions_take_the_quiet_edit_token_and_drop_it_when_empty() -> None:
    comment = ROOT / ".github" / "actions" / "manual-comment" / "action.yml"
    for path in (ACTION, comment):
        text = path.read_text(encoding="utf-8")
        assert load(path)["inputs"]["edit-token"]["default"] == ""
        assert "OSAC_CI_EDIT_TOKEN: ${{ inputs.edit-token }}" in text
        assert 'if [ -z "$OSAC_CI_EDIT_TOKEN" ]; then unset OSAC_CI_EDIT_TOKEN; fi' in text


def test_the_decision_and_the_result_edit_the_comment_with_the_workflows_own_token_and_have_the_permission() -> None:
    decide, finish = DECIDE, ROOT / ".github" / "workflows" / "e2e-pr-finish.yml"
    for path, job in ((decide, "decide"), (finish, "finish")):
        wf = load(path)
        assert wf["jobs"][job]["permissions"]["pull-requests"] == "write", "a comment on a pull request needs it"
        assert "issues" not in wf["jobs"][job]["permissions"]
        step = next(s for s in wf["jobs"][job]["steps"] if s.get("uses") == "$/.github/actions/manual-comment")
        assert step["with"]["edit-token"] == "${{ github.token }}"
        assert step["with"]["github-token"] == "${{ steps.app-token.outputs.token }}", "the app still creates it"

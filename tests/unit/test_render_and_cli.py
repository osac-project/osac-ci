import json
from pathlib import Path

import pytest
from helpers import OK_LABELS, ROOT, snap, with_check
from syrupy.assertion import SnapshotAssertion

from osac_ci.cli import main, snapshot_from_dict
from osac_ci.model import CheckRun, Snapshot
from osac_ci.planner import plan, plan_or_error
from osac_ci.render import render_markdown

pytestmark = pytest.mark.unit


def test_render_ready(osac_policy, snapshot: SnapshotAssertion) -> None:  # type: ignore[no-untyped-def]
    assert render_markdown(plan(snap(osac_policy), osac_policy)) == snapshot


def test_render_awaiting_approval(osac_policy, snapshot: SnapshotAssertion) -> None:  # type: ignore[no-untyped-def]
    v = plan(snap(osac_policy, labels=OK_LABELS - {"lgtm"}), osac_policy)
    assert render_markdown(v) == snapshot


def test_render_failed_check(osac_policy, snapshot: SnapshotAssertion) -> None:  # type: ignore[no-untyped-def]
    runs = with_check(osac_policy, "pre-commit", CheckRun("pre-commit", "completed", "failure"))
    assert render_markdown(plan(snap(osac_policy, check_runs=runs), osac_policy)) == snapshot


def test_render_planner_error(toy_policy, snapshot: SnapshotAssertion) -> None:  # type: ignore[no-untyped-def]
    broken = Snapshot(repo="o/r", number=1, head_sha="x", changed_files=None)  # type: ignore[arg-type]
    assert render_markdown(plan_or_error(broken, toy_policy)) == snapshot


def test_snapshot_from_dict_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown snapshot keys"):
        snapshot_from_dict({"repo": "o/r", "number": 1, "head_sha": "x", "labls": []})


def test_cli_policy_check_and_explain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = str(ROOT / "policy" / "osac.yml")
    assert main(["policy", "check", policy]) == 0
    assert "29 jobs" in capsys.readouterr().out

    snapshot_file = tmp_path / "s.json"
    snapshot_file.write_text(
        json.dumps({"repo": "osac-project/osac", "number": 7, "head_sha": "x", "labels": ["lgtm"]})
    )
    assert main(["explain", "--policy", policy, "--snapshot", str(snapshot_file)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("### OSAC CI:") and "**Next:**" in out


def test_cli_invalid_policy_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "p.yml"
    bad.write_text("version: 1\nrepo: o/r\njobs: {}\n")
    assert main(["policy", "check", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err

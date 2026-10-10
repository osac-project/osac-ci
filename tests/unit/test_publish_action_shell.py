"""The shell of the publish action's lookup step, run for real with a stand-in `uv`."""

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit
ACTION = ROOT / ".github" / "actions" / "publish" / "action.yml"


def lookup_script() -> str:
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    return next(s["run"] for s in steps if s.get("id") == "target")


def run(tmp_path: Path, uv_prints: str = "", uv_exit: int = 0, **env: str) -> tuple[int, dict[str, str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(f"#!/bin/bash\necho \"$@\" >> '{tmp_path}/uv-args'\nprintf '%s' '{uv_prints}'\nexit {uv_exit}\n")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    output = tmp_path / "out"
    output.touch()
    base = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(output), "ACTION_PATH": "/a/b/c",
        "GH_TOKEN": "t", "REPO": "o/r", "NUMBER": "", "HEAD_SHA": "",
        "SWEEP": "false",
    }  # fmt: skip
    done = subprocess.run(
        ["bash", "-c", lookup_script()], env={**base, **env}, capture_output=True, text=True, check=False
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return done.returncode, values, done.stdout + done.stderr


def test_a_given_number_is_used_as_it_is_without_a_lookup(tmp_path: Path) -> None:
    code, out, _ = run(tmp_path, NUMBER="42")
    assert (code, out) == (0, {"mode": "one", "numbers": "42"}) and not (tmp_path / "uv-args").exists()


def test_the_lookup_gets_only_the_run_head_commit(tmp_path: Path) -> None:
    code, out, _ = run(tmp_path, uv_prints="7\n", HEAD_SHA="a" * 40)
    assert code == 0 and out == {"mode": "one", "numbers": "7"}
    assert "find-pr --repo o/r --sha " + "a" * 40 in (tmp_path / "uv-args").read_text()


def test_several_matches_are_all_kept(tmp_path: Path) -> None:
    _, out, _ = run(tmp_path, uv_prints="7\n9\n", HEAD_SHA="a" * 40)
    assert out == {"mode": "one", "numbers": "7 9"}


@pytest.mark.parametrize("number", ["7 9", "7;id", "$(id)", "-1", "1.5", " 7"])
def test_an_explicit_number_is_exactly_one_pull_request(tmp_path: Path, number: str) -> None:
    code, out, log = run(tmp_path, NUMBER=number)
    assert code == 1 and out == {} and "digits only" in log


def test_no_match_does_nothing_unless_a_sweep_was_asked_for(tmp_path: Path) -> None:
    _, out, log = run(tmp_path, uv_prints="", HEAD_SHA="a" * 40)
    assert out == {"mode": "none"} and "No open pull request has this head" in log


def test_no_match_with_a_sweep_requested_sweeps(tmp_path: Path) -> None:
    _, out, _ = run(tmp_path, uv_prints="", HEAD_SHA="a" * 40, SWEEP="true")
    assert out == {"mode": "sweep"}


def test_nothing_given_and_a_sweep_requested_sweeps_without_asking(tmp_path: Path) -> None:
    code, out, _ = run(tmp_path, SWEEP="true")
    assert (code, out) == (0, {"mode": "sweep"}) and not (tmp_path / "uv-args").exists()


@pytest.mark.parametrize("printed", ["7; rm -rf /", "$(id)", "7\nabc", "-1", "1.5"])
def test_anything_but_digits_is_refused(tmp_path: Path, printed: str) -> None:
    code, out, log = run(tmp_path, uv_prints=printed, HEAD_SHA="a" * 40)
    assert code == 1 and out == {} and "digits only" in log


def test_a_failing_lookup_stops_the_step(tmp_path: Path) -> None:
    code, out, _ = run(tmp_path, uv_exit=3, HEAD_SHA="a" * 40)
    assert code != 0 and out == {}


def test_the_publish_step_loops_over_the_found_numbers(tmp_path: Path) -> None:
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Post the verdict")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(f"#!/bin/bash\necho \"$@\" >> '{tmp_path}/calls'\n")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_TOKEN": "t", "REPO": "o/r", "POLICY": "policy/osac.yml",
        "MODE": "one",
        "NUMBERS": "7 9", "RECENT": "1", "ROTATE": "1", "RESERVE": "1", "INTERVAL": "1", "STALE_ONLY": "false",
        "VERDICT_APP_ID": "1", "STALE_AFTER": "1", "MAX_AGE": "1", "NOTE": "n", "LOOKUP_MEMBERSHIP": "true",
        "OSAC_CI_ORG_TOKEN": "x", "ACTION_PATH": "/a/b/c",
    }  # fmt: skip
    done = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)
    calls = (tmp_path / "calls").read_text().splitlines()
    assert done.returncode == 0 and len(calls) == 2
    assert "--number 7" in calls[0] and "--number 9" in calls[1] and all("--require-org-token" in c for c in calls)
    assert all("--all" not in c for c in calls)


def publish_run(tmp_path: Path, numbers: str, failing: tuple[str, ...] = ()) -> tuple[int, list[str]]:
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Post the verdict")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    uv = bin_dir / "uv"
    checks = "\n".join(f'case "$*" in *"--number {n} "*|*"--number {n}") exit 1;; esac' for n in failing)
    uv.write_text(f"#!/bin/bash\necho \"$@\" >> '{tmp_path}/calls'\n{checks}\n")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_TOKEN": "t", "REPO": "o/r", "POLICY": "policy/osac.yml",
        "MODE": "one", "NUMBERS": numbers, "RECENT": "1", "ROTATE": "1", "RESERVE": "1", "INTERVAL": "1",
        "STALE_ONLY": "false", "VERDICT_APP_ID": "1", "STALE_AFTER": "1", "MAX_AGE": "1", "NOTE": "n",
        "LOOKUP_MEMBERSHIP": "true", "OSAC_CI_ORG_TOKEN": "x", "ACTION_PATH": "/a/b/c",
    }  # fmt: skip
    done = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return done.returncode, calls


def test_a_failed_publish_does_not_stop_the_others_but_fails_the_step(tmp_path: Path) -> None:
    code, calls = publish_run(tmp_path, "7 9 11", failing=("7",))
    assert code == 1 and len(calls) == 3  # 9 and 11 were still tried
    assert "--number 9" in calls[1] and "--number 11" in calls[2]


def test_a_failure_in_the_middle_or_the_end_fails_the_step_too(tmp_path: Path) -> None:
    assert publish_run(tmp_path / "a", "7 9 11", failing=("9",))[0] == 1
    assert publish_run(tmp_path / "b", "7 9 11", failing=("11",))[0] == 1


def test_when_every_publish_succeeds_the_step_succeeds(tmp_path: Path) -> None:
    code, calls = publish_run(tmp_path, "7 9")
    assert code == 0 and len(calls) == 2

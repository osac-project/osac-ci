"""The lock-gate action: its shape, and its shell run for real with a stand-in `uv` and `sleep`."""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit
ACTION = ROOT / ".github" / "actions" / "lock-gate" / "action.yml"
SHA = "a" * 40


def action() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def read_script() -> str:
    return next(s["run"] for s in action()["runs"]["steps"] if s.get("id") == "read")


# ---- shape --------------------------------------------------------------------------------------------------------


def test_every_input_used_is_declared_and_every_input_declared_is_used() -> None:
    text = ACTION.read_text(encoding="utf-8")
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", text))
    assert used == set(action()["inputs"])


def test_no_input_is_expanded_into_a_script_and_nothing_is_checked_out() -> None:
    for step in action()["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")
        assert "checkout" not in step.get("uses", "")


def test_the_outputs_come_from_the_step_that_sets_them() -> None:
    outputs = action()["outputs"]
    assert set(outputs) == {"state", "locked"}
    assert all("steps.read.outputs." in o["value"] for o in outputs.values())


def test_the_documented_pattern_skips_the_job_not_its_steps() -> None:
    """A job whose steps are all skipped ends as a success, which reads as a result: the lock would become a pass."""
    text = ACTION.read_text(encoding="utf-8")
    assert "needs.gate.outputs.locked != 'true'" in text and "Do not skip the steps" in text


# ---- the shell, run for real --------------------------------------------------------------------------------------


def run(tmp_path: Path, answers: list[str], *, wait: str = "0", sha: str = SHA, uv_exit: int = 0):  # type: ignore[no-untyped-def]
    """Run the step with `uv` answering from ``answers`` in turn (the last one repeats) and `sleep` doing nothing."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    counter = tmp_path / "calls"
    counter.write_text("0")
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/bash\n"
        f'n=$(cat "{counter}"); echo $((n+1)) > "{counter}"\n'
        f'echo "$@" >> "{tmp_path}/uv-args"\n'
        f"answers=({' '.join(answers) or 'unknown'})\n"
        'i=$n; [ "$i" -ge "${#answers[@]}" ] && i=$((${#answers[@]}-1))\n'
        'echo "${answers[$i]}"\n'
        f"exit {uv_exit}\n"
    )
    (bin_dir / "sleep").write_text("#!/bin/bash\nexit 0\n")
    for f in (uv, bin_dir / "sleep"):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    output = tmp_path / "out"
    output.touch()
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(output), "ACTION_PATH": "/a/b/c",
        "GH_TOKEN": "", "REPO": "o/r", "SHA": sha, "JOB": "unit-tests", "CHECK_NAME": "OSAC CI", "WAIT": wait,
    }  # fmt: skip
    done = subprocess.run(["bash", "-c", read_script()], env=env, capture_output=True, text=True, check=False)
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    calls = int(counter.read_text())
    return done.returncode, values, calls, done.stdout + done.stderr


@pytest.mark.parametrize(
    ("state", "locked"), [("locked", "true"), ("open", "false"), ("not-applicable", "false"), ("unknown", "false")]
)
def test_only_locked_means_locked(tmp_path: Path, state: str, locked: str) -> None:
    code, out, calls, _ = run(tmp_path, [state])
    assert (code, out, calls) == (0, {"state": state, "locked": locked}, 1)


def test_the_arguments_are_passed_as_data(tmp_path: Path) -> None:
    run(tmp_path, ["open"])
    args = (tmp_path / "uv-args").read_text()
    assert f"lock-status --repo o/r --sha {SHA} --job unit-tests --check-name OSAC CI" in args


def test_a_job_with_no_wait_asks_once_and_goes_on_when_unknown(tmp_path: Path) -> None:
    code, out, calls, _ = run(tmp_path, ["unknown"], wait="0")
    assert (code, out["locked"], calls) == (0, "false", 1)


def test_it_waits_for_the_check_to_appear_and_uses_the_first_fact(tmp_path: Path) -> None:
    code, out, calls, _ = run(tmp_path, ["unknown", "unknown", "locked"], wait="120")
    assert (code, out, calls) == (0, {"state": "locked", "locked": "true"}, 3)


def test_it_gives_up_waiting_at_the_deadline_and_goes_on(tmp_path: Path) -> None:
    """The stand-in `sleep` does nothing, so the loop asks again and again until the real clock passes the deadline."""
    code, out, calls, _ = run(tmp_path, ["unknown"], wait="1")
    assert code == 0 and out == {"state": "unknown", "locked": "false"} and calls > 1


@pytest.mark.parametrize(
    "wait",
    [
        "",
        "-1",
        "1.5",
        "5;id",
        "301",
        "abc",
        "0000301",
        "1234567",
        "99999999999999999999",
        "18446744073709551616",
        "18446744073709551621",
    ],
)
def test_a_bad_wait_is_refused_before_anything_is_asked(tmp_path: Path, wait: str) -> None:
    code, out, calls, log = run(tmp_path, ["open"], wait=wait)
    assert code == 1 and out == {} and calls == 0 and "wait-seconds" in log


def test_an_unexpected_answer_stops_the_step_rather_than_guessing(tmp_path: Path) -> None:
    code, out, _, log = run(tmp_path, ["banana"])
    assert code == 1 and out == {} and "Unexpected answer" in log


def test_a_failing_lookup_stops_the_step(tmp_path: Path) -> None:
    code, out, _, _ = run(tmp_path, ["open"], uv_exit=3)
    assert code != 0 and out == {}


@pytest.mark.parametrize(
    ("wait", "seconds"),
    [("08", 8), ("09", 9), ("010", 10), ("0010", 10), ("007", 7), ("300", 300), ("0300", 300), ("000300", 300)],
)
def test_a_wait_with_leading_zeros_is_read_as_decimal(tmp_path: Path, wait: str, seconds: int) -> None:
    """Bash arithmetic would call 08 an error and 010 eight; the number must mean what it says."""
    # The state is known at once, so the wait is only announced: the number in the log is the one used.
    code, out, _, log = run(tmp_path, ["open"], wait=wait)
    assert code == 0 and out == {"state": "open", "locked": "false"}
    assert f"Waiting up to {seconds} seconds" in log


def test_a_wait_of_zero_says_nothing_about_waiting(tmp_path: Path) -> None:
    for wait in ("0", "00", "000"):
        code, _, _, log = run(tmp_path / wait, ["open"], wait=wait)
        assert code == 0 and "Waiting" not in log

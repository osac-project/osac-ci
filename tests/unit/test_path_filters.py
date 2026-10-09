"""Named path filters in the policy: parsing, enforce mode, shadow mode, and the OSAC job mapping."""

from pathlib import Path

import pytest
from helpers import snap

from osac_ci.model import CheckRun, JobStatus, State
from osac_ci.paths import applicable, filters_hold
from osac_ci.planner import plan
from osac_ci.policy import Policy, PolicyError, load_policy, parse_policy

pytestmark = pytest.mark.unit

FILTERS = {"go": ["**/*.go"], "docs": ["docs/**"], "api": ["{api/**,proto/**}", "!**/*_test.go"]}


def policy(mode: str = "shadow", **jobs: object) -> Policy:
    text = {
        "version": 1,
        "repo": "o/r",
        "merge": {"required_labels": [], "blocking_labels": []},
        "path_filters": {"mode": mode, "filters": FILTERS},
        "jobs": jobs or {"build": {"check": "build", "filters": ["go"]}},
    }
    import yaml

    return parse_policy(yaml.safe_dump(text))


def run(name: str, conclusion: str, status: str = "completed") -> CheckRun:
    return CheckRun(name, status, conclusion if status == "completed" else None, "2026-10-01T10:00:00Z")


def plan_for(p: Policy, files: tuple[str, ...], *runs: CheckRun):  # type: ignore[no-untyped-def]
    return plan(snap(p, labels=frozenset(), changed_files=files, check_runs=runs), p)


# ---- the policy file ---------------------------------------------------------------------------------------------


def test_filters_default_to_shadow_and_an_empty_set() -> None:
    p = parse_policy("version: 1\nrepo: o/r\njobs: {a: {check: a}}\n")
    assert p.path_filters.mode == "shadow" and p.path_filters.filters == {}


def test_a_job_may_only_name_defined_filters() -> None:
    with pytest.raises(PolicyError, match=r"not defined: \['nope'\]"):
        policy(build={"check": "build", "filters": ["go", "nope"]})
    with pytest.raises(PolicyError, match="not defined"):
        policy(build={"check": "build", "filters_any": ["nope"]})


def test_a_job_uses_globs_or_named_filters_not_both() -> None:
    with pytest.raises(PolicyError, match="both globs"):
        policy(build={"check": "build", "filters": ["go"], "paths": ["**"]})


def test_the_filters_come_from_a_file_next_to_the_policy(tmp_path: Path) -> None:
    (tmp_path / "f.yml").write_text("go:\n  - '**/*.go'\n", encoding="utf-8")
    (tmp_path / "p.yml").write_text(
        "version: 1\nrepo: o/r\npath_filters: {file: f.yml}\njobs: {a: {check: a, filters: [go]}}\n", encoding="utf-8"
    )
    assert load_policy(tmp_path / "p.yml").path_filters.filters == {"go": ("**/*.go",)}


def test_the_filters_file_may_sit_in_a_subdirectory_even_via_a_dotdot_that_stays_inside(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.yml").write_text("go: ['**/*.go']\n", encoding="utf-8")
    for name in ("sub/f.yml", "sub/../sub/f.yml", "./sub/f.yml"):
        text = f"version: 1\nrepo: o/r\npath_filters: {{file: {name}}}\njobs: {{a: {{check: a}}}}\n"
        assert parse_policy(text, base=tmp_path).path_filters.filters == {"go": ("**/*.go",)}


@pytest.mark.parametrize("name", ["../outside.yml", "sub/../../outside.yml", "../../etc/passwd"])
def test_the_filters_file_may_not_leave_the_policy_directory(tmp_path: Path, name: str) -> None:
    base = tmp_path / "policy"
    (base / "sub").mkdir(parents=True)
    (tmp_path / "outside.yml").write_text("go: ['x']\n", encoding="utf-8")
    text = f"version: 1\nrepo: o/r\npath_filters: {{file: {name}}}\njobs: {{a: {{check: a}}}}\n"
    with pytest.raises(PolicyError, match="leaves the policy directory"):
        parse_policy(text, base=base)


def test_the_filters_file_may_not_be_an_absolute_path(tmp_path: Path) -> None:
    (tmp_path / "f.yml").write_text("go: ['x']\n", encoding="utf-8")
    text = f"version: 1\nrepo: o/r\npath_filters: {{file: {tmp_path / 'f.yml'}}}\njobs: {{a: {{check: a}}}}\n"
    with pytest.raises(PolicyError, match="must be relative"):
        parse_policy(text, base=tmp_path)


def test_a_symlink_that_points_out_of_the_directory_is_refused(tmp_path: Path) -> None:
    base = tmp_path / "policy"
    base.mkdir()
    (tmp_path / "outside.yml").write_text("go: ['x']\n", encoding="utf-8")
    (base / "link.yml").symlink_to(tmp_path / "outside.yml")
    text = "version: 1\nrepo: o/r\npath_filters: {file: link.yml}\njobs: {a: {check: a}}\n"
    with pytest.raises(PolicyError, match="leaves the policy directory"):
        parse_policy(text, base=base)


def test_a_file_needs_a_policy_that_was_loaded_from_one() -> None:
    with pytest.raises(PolicyError, match="loaded from a file"):
        parse_policy("version: 1\nrepo: o/r\npath_filters: {file: f.yml}\njobs: {a: {check: a}}\n")


def test_inline_and_file_filters_may_not_share_a_name(tmp_path: Path) -> None:
    (tmp_path / "f.yml").write_text("go: ['**/*.go']\n", encoding="utf-8")
    text = "version: 1\nrepo: o/r\npath_filters: {file: f.yml, filters: {go: ['x']}}\njobs: {a: {check: a}}\n"
    with pytest.raises(PolicyError, match="both inline and in f.yml"):
        parse_policy(text, base=tmp_path)


@pytest.mark.parametrize("content", ["- a\n- b\n", "go: []\n", "go: ['a', 3]\n", "go: 'a'\n", ": ["])
def test_a_malformed_filters_file_is_rejected(tmp_path: Path, content: str) -> None:
    (tmp_path / "f.yml").write_text(content, encoding="utf-8")
    with pytest.raises(PolicyError):
        parse_policy("version: 1\nrepo: o/r\npath_filters: {file: f.yml}\njobs: {a: {check: a}}\n", base=tmp_path)


def test_a_missing_filters_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="cannot read path filters file"):
        parse_policy("version: 1\nrepo: o/r\npath_filters: {file: missing.yml}\njobs: {a: {check: a}}\n", base=tmp_path)


# ---- how filters combine -------------------------------------------------------------------------------------------


def test_all_of_and_any_of_combine_like_the_workflows_if_expressions() -> None:
    f = {"go": ["**/*.go"], "docs": ["docs/**"], "api": ["api/**"]}
    files = ["main.go", "docs/a.md"]
    assert filters_hold(files, f, ["go", "docs"], [])  # all of
    assert not filters_hold(files, f, ["go", "api"], [])  # one of the "all" does not hold
    assert filters_hold(files, f, [], ["api", "docs"])  # any of
    assert not filters_hold(files, f, [], ["api"])
    assert filters_hold(files, f, ["go"], ["docs", "api"])  # both groups, ANDed
    assert not filters_hold(files, f, ["api"], ["docs"])


def test_one_changed_file_may_satisfy_one_filter_and_another_file_the_next() -> None:
    # `code` and a component filter are each "some changed file matches", not "one file matches both"
    f = {"code": ["**/*.go"], "docs": ["docs/**"]}
    assert filters_hold(["main.go", "docs/a.md"], f, ["code", "docs"], [])


# ---- enforce -----------------------------------------------------------------------------------------------------


def test_enforce_makes_a_job_not_applicable_when_its_filters_do_not_hold() -> None:
    p = policy("enforce")
    v = plan_for(p, ("README.md",))  # no check run at all: nothing waits for it
    assert v.state is State.READY_TO_ENQUEUE
    (entry,) = v.jobs
    assert entry.status is JobStatus.NOT_APPLICABLE and entry.detail == "path filters do not hold (go)"


def test_enforce_ignores_even_a_failure_on_files_the_filters_call_irrelevant() -> None:
    p = policy("enforce")
    assert plan_for(p, ("README.md",), run("build", "failure")).state is State.READY_TO_ENQUEUE


def test_enforce_still_checks_a_job_whose_filters_hold() -> None:
    p = policy("enforce")
    assert plan_for(p, ("main.go",), run("build", "failure")).state is State.CHECKS_FAILED
    assert plan_for(p, ("main.go",)).state is State.CHECKS_RUNNING  # applicable and not reported yet
    assert plan_for(p, ("main.go",), run("build", "success")).state is State.READY_TO_ENQUEUE


def test_a_pr_with_no_changed_files_never_skips_anything() -> None:
    p = policy("enforce")
    assert plan_for(p, ()).state is State.CHECKS_RUNNING  # the filters cannot decide, so the job still counts


def test_a_file_list_that_could_not_be_read_completely_never_skips_a_job() -> None:
    # A merge-queue commit over the compare cap has an unknown list. Even a partial list must not decide anything.
    p = policy("enforce")
    for files in ((), ("README.md",)):
        v = plan(snap(p, labels=frozenset(), changed_files=files, changed_files_known=False, check_runs=()), p)
        assert v.state is State.CHECKS_RUNNING  # the job still counts and has not reported
        assert [e.status for e in v.jobs] == [JobStatus.WAITING]


def test_shadow_adds_no_note_when_the_file_list_is_unknown() -> None:
    p = policy("shadow")
    v = plan(
        snap(
            p,
            labels=frozenset(),
            changed_files=("README.md",),
            changed_files_known=False,
            check_runs=(run("build", "failure"),),
        ),
        p,
    )
    assert v.notes == ()


def test_enforce_adds_no_notes() -> None:
    assert plan_for(policy("enforce"), ("main.go",), run("build", "skipped")).notes == ()


# ---- shadow ------------------------------------------------------------------------------------------------------


def test_shadow_never_changes_the_verdict() -> None:
    p = policy("shadow")
    assert plan_for(p, ("README.md",), run("build", "failure")).state is State.CHECKS_FAILED  # trusts the check
    assert plan_for(p, ("README.md",)).state is State.CHECKS_RUNNING  # missing check still counts


def test_shadow_notes_a_failure_on_files_the_filters_call_irrelevant() -> None:
    v = plan_for(policy("shadow"), ("README.md",), run("build", "failure"))
    assert v.notes == ("path filter: go say build does not apply to these files, but its conclusion was failure",)
    assert plan_for(policy("shadow"), ("README.md",), run("build", "timed_out")).notes


def test_shadow_notes_a_skip_on_files_the_filters_call_relevant() -> None:
    v = plan_for(policy("shadow"), ("main.go",), run("build", "skipped"))
    assert v.notes == ("path filter: go say build applies to these files, but it was skipped",)


@pytest.mark.parametrize(
    ("files", "check"),
    [
        (("README.md",), run("build", "success")),  # a green check on irrelevant files is the normal "nothing to do"
        (("README.md",), run("build", "skipped")),
        (("main.go",), run("build", "success")),
        (("main.go",), run("build", "failure")),  # a real failure on relevant files is just a failure
        (("main.go",), run("build", "", status="in_progress")),
        (("README.md",), run("build", "", status="in_progress")),
    ],
)
def test_shadow_says_nothing_when_the_outcome_proves_nothing(files: tuple[str, ...], check: CheckRun) -> None:
    assert plan_for(policy("shadow"), files, check).notes == ()


def test_a_skipped_readiness_gate_says_nothing_about_the_filters() -> None:
    # An E2E gate is skipped on purpose until the PR is unlocked (see README: E2E unlock).
    p = policy(gate={"check": "gate", "kind": "e2e", "needs_readiness": True, "filters": ["go"]})
    assert plan_for(p, ("main.go",), run("gate", "skipped")).notes == ()
    assert plan_for(p, ("README.md",), run("gate", "failure")).notes != ()  # a real failure still counts


def test_shadow_names_every_filter_of_the_job() -> None:
    p = policy(build={"check": "build", "filters": ["go"], "filters_any": ["docs", "api"]})
    v = plan_for(p, ("main.go", "docs/a.md"), run("build", "skipped"))
    assert v.notes == ("path filter: go, docs, api say build applies to these files, but it was skipped",)


def test_shadow_notes_do_not_replace_other_notes() -> None:
    p = policy("shadow")
    assert [n.startswith("path filter:") for n in plan_for(p, ("main.go",), run("build", "skipped")).notes] == [True]


# ---- the OSAC mapping --------------------------------------------------------------------------------------------


def applicable_jobs(osac_policy: Policy, *files: str) -> set[str]:
    """Jobs whose named filters hold for these files, as the workflows' `if:` expressions would decide."""
    out = set()
    for job_id, job in osac_policy.jobs.items():
        if job.paths:  # a folder-scoped job (osac-ui) uses globs, not the shared filters
            holds = applicable(files, job.paths, job.exclude_paths)
        else:
            holds = not (job.filters or job.filters_any) or filters_hold(
                files, osac_policy.path_filters.filters, job.filters, job.filters_any
            )
        if holds:
            out.add(job_id)
    return out


ALWAYS = {"pre-commit", "dependency-review"}  # no filters: they run on every PR


def test_a_docs_only_change_applies_to_nothing_but_the_unfiltered_jobs(osac_policy: Policy) -> None:
    assert applicable_jobs(osac_policy, "README.md", "docs/guide.md") == ALWAYS


def test_an_aap_change_applies_to_the_aap_jobs_and_the_code_gated_ones(osac_policy: Policy) -> None:
    got = applicable_jobs(osac_policy, "osac-aap/roles/x/tasks/main.yml")
    assert {"ansible-lint", "integration-aap", "integration-installer"} <= got
    assert "unit-tests" not in got and "integration-fulfillment-service" not in got  # not their component


def test_a_fulfillment_service_change_applies_to_its_checks(osac_policy: Policy) -> None:
    got = applicable_jobs(osac_policy, "fulfillment-service/internal/server/server.go")
    assert {
        "go-proto-checks",
        "python-checks",
        "build-binaries",
        "unit-tests",
        "integration-fulfillment-service",
    } <= got
    assert "ansible-lint" not in got and "darwin-keychain" not in got


def test_an_e2e_test_of_one_suite_does_not_apply_to_the_other_suites(osac_policy: Policy) -> None:
    got = applicable_jobs(osac_policy, "tests/e2e/vmaas/test_x.py")
    assert "e2e-vmaas" in got and "e2e-caas" not in got and "e2e-bmaas" not in got  # the safe-skip lists


def test_a_shared_e2e_helper_applies_to_every_suite(osac_policy: Policy) -> None:
    got = applicable_jobs(osac_policy, "tests/e2e/core/client.py")
    assert {"e2e-vmaas", "e2e-caas", "e2e-bmaas"} <= got


def test_the_installer_helm_job_runs_for_any_chart_change(osac_policy: Policy) -> None:
    for path in ("osac-aap/charts/x.yaml", "osac-metering/charts/y.yaml", "osac-installer/charts/z.yaml"):
        assert "helm-lint-installer" in applicable_jobs(osac_policy, path)
    assert "helm-lint-installer" not in applicable_jobs(osac_policy, "README.md")


def test_every_job_but_two_names_filters_and_the_defaults_are_shadow(osac_policy: Policy) -> None:
    unfiltered = {j for j, job in osac_policy.jobs.items() if not (job.filters or job.filters_any or job.paths)}
    assert unfiltered == ALWAYS and osac_policy.path_filters.mode == "shadow"

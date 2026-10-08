# osac-ci

A policy-driven CI control plane for the OSAC repositories. For any pull request it answers, in one place:
**what is blocking it, why, who must act, and what happens next.**

> Status: slice 0 (read-only planner, explainer and replay). Nothing here gates a merge yet and nothing writes to
> GitHub. The entrypoint workflow, sandbox and path-filter port come in later slices.

## What exists

| Piece | Where | Notes |
|---|---|---|
| Domain model | `src/osac_ci/model.py` | `Snapshot` in, `Verdict` out, one `State` per PR |
| Planner | `src/osac_ci/planner.py` | pure function `plan(snapshot, policy, mode)`; `plan_or_error` fails closed |
| Legacy rules, ported | `src/osac_ci/rules/` | labels (`auto-queue.sh`), E2E readiness (`check-e2e-readiness.sh`), fork authorization (`authorize-fork-pr`) |
| Policy | `policy/osac.yml`, `policy/toy.yml` | today's OSAC rules; a toy repo that proves the engine is generic |
| GitHub adapter | `src/osac_ci/github/` | standard library only; about 9 read requests per PR; no bulk `statusCheckRollup` pull |
| Open-PR report | `src/osac_ci/report.py`, `.github/workflows/report.yml` | one table of every open PR: state, reason, who acts, next step; markdown, HTML and JSON; nightly |
| Replay | `src/osac_ci/replay.py`, `parity/explained.yaml` | the parity evidence: does the planner agree with recently merged PRs? |
| CLI | `osac-ci policy check`, `explain`, `explain-pr`, `report`, `replay` | read-only; live commands need `GH_TOKEN` |

## Try it

```bash
uv sync
uv run osac-ci policy check policy/osac.yml
uv run osac-ci explain --policy policy/osac.yml --snapshot some-snapshot.json
```

## Native approval (instead of `lgtm` / `approved` labels)

A policy with an `approval:` section decides approval from GitHub reviews and CODEOWNERS, not from labels:

```yaml
approval:
  min_approvals: 1                # people other than the author
  require_code_owners: true       # every changed file with owners needs one of them
  carry_over: trivial-rebase      # or: never
merge:
  required_labels: [jira/valid-reference]   # the approval labels are no longer required
```

- Only human reviewers count; the latest review of each person decides; outstanding "changes requested" blocks.
- CODEOWNERS is read from the **base** branch, so a PR cannot make its author an owner. Team owners are expanded with the
  org members API; a team that cannot be read is a `planner-error`, never a pass.
- An approval given on an older commit still counts when the PR makes exactly the same changes (a rebase, or the base
  merged in): `osac_ci/fingerprint.py` compares what each commit adds and removes, ignoring context, line numbers and
  blob ids. Re-indenting, a different mode or a different binary count as a change. The merge queue still tests the PR
  on top of the current base, so a rebase that changes behavior is caught there.
- An approved PR also unlocks E2E the way the `lgtm` label does today.

Without an `approval:` section nothing changes: `policy/osac.yml` keeps today's label rules.

## Publishing the verdict as a check

`osac-ci publish --policy P --repo R (--number N | --all)` evaluates a PR from live GitHub state and posts one check run
named `OSAC CI` on its head commit (`--dry-run` prints instead of posting).

| State | Check run | Why |
|---|---|---|
| ready, in the queue, queue passed | `completed / success` | nothing left to do |
| checks or E2E running | `in_progress` | machines are working |
| awaiting approval, needs authorization, draft, a check failed | `completed / action_required` | a person acts; blocked but not red, and the summary says why |
| planner failed | `completed / failure` | fail closed, with the cause and a re-run hint |

The newest check run of a name decides, so publishing again repairs a stale verdict, and an unchanged verdict is not
posted twice. `.github/workflows/osac-ci-check.yml` runs it for this repository's own PRs (policy
`policy/osac-ci.yml`) on PR events, when `ci` finishes, and every 10 minutes for all open PRs. The check is not
required yet. Known limit: a slow sweep can post a verdict computed a few seconds earlier than a per-PR run's;
the next event or sweep corrects it.

## Open-PR report

```bash
GH_TOKEN=$(gh auth token) uv run osac-ci report --policy policy/osac.yml --repo osac-project/osac --out-dir report
```

Writes `report.md`, `report.html` (filterable, self-contained) and `report.json`. A PR that cannot be read shows up
as a `planner-error` row, so nothing silently disappears. PR titles are treated as untrusted text in every format.
The `open-pr-report` workflow runs it every night and keeps the files as a workflow artifact and in the job summary.
On the built-in token it makes about 7 requests per PR, which may hit its rate limit on a very large run.

## Replay (parity evidence)

```bash
GH_TOKEN=$(gh auth token) uv run osac-ci replay --policy policy/osac.yml --repo osac-project/osac --days 60 --limit 100
```

PRs the merge queue merged are held to the planner: it must call each one ready, and every disagreement must be
listed in `parity/explained.yaml`. PRs merged directly (outside the queue) are reported separately as bypasses,
with the verdict the rules would have given. The run exits 1 only for unexplained queue-merged disagreements. It
reads each PR's final state, not its state when it was enqueued.

First run, OSAC, last 60 days: 100 merged PRs; 85 through the queue (planner agrees on all 85); 15 merged
directly, of which 12 the rules would have blocked.

## Tests

```bash
uv run pytest                    # everything, including differential tests if the sibling repos are present
uv run pytest -m "not legacy"    # without the legacy differential tests
```

The `legacy` tests source the real bash functions from sibling checkouts (`../osac`, `../osac-test-infra`) and
compare them to the Python port: labels exhaustively, readiness with generated scenarios. Point
`OSAC_AUTO_QUEUE_SH` and `OSAC_CHECK_E2E_READINESS_SH` at other copies if your layout differs.

## Layers (see the design doc for the full plan)

L0 static, L1 unit, L2 property, L3 contract, L4 legacy characterization, L5 replay, L6 sandbox, L7 security,
L8 chaos, L9 test-the-tests. This commit delivers L0, L1, L2, parts of L4, and L8 via the fail-closed tests.

## Rules for contributors

Read [AGENTS.md](AGENTS.md). Policy and workflow changes need `@osac-project/wg-infra` review.

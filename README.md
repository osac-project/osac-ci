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

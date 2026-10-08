# osac-ci

A policy-driven CI control plane for the OSAC repositories. For any pull request it answers, in one place:
**what is blocking it, why, who must act, and what happens next.**

> Status: slice 0 (read-only planner and explainer). Nothing here gates a merge yet. It runs against recorded
> snapshots; the GitHub adapter, replay harness and entrypoint workflow come in later slices.

## What exists

| Piece | Where | Notes |
|---|---|---|
| Domain model | `src/osac_ci/model.py` | `Snapshot` in, `Verdict` out, one `State` per PR |
| Planner | `src/osac_ci/planner.py` | pure function `plan(snapshot, policy, mode)`; `plan_or_error` fails closed |
| Legacy rules, ported | `src/osac_ci/rules/` | labels (`auto-queue.sh`), E2E readiness (`check-e2e-readiness.sh`), fork authorization (`authorize-fork-pr`) |
| Policy | `policy/osac.yml`, `policy/toy.yml` | today's OSAC rules; a toy repo that proves the engine is generic |
| CLI | `osac-ci policy check`, `osac-ci explain` | offline, read-only |

## Try it

```bash
uv sync
uv run osac-ci policy check policy/osac.yml
uv run osac-ci explain --policy policy/osac.yml --snapshot some-snapshot.json
```

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

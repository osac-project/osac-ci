# osac-ci: rules for humans and AI agents

This repo decides whether pull requests in other repos may merge. A mistake here can block every PR or let an
unchecked one through. Keep it boring and well tested.

1. **The planner is pure.** `src/osac_ci/planner.py`, `rules/` and `policy.py` do no I/O and read no clock, env
   or randomness. GitHub access belongs in adapters, behind an interface, never in the planner.
2. **Fail closed.** Anything unexpected becomes the `planner-error` state with a cause and a re-run hint. Never
   turn an error into a passing verdict.
3. **The legacy scripts are the spec until the parity gate passes.** `tests/legacy/` runs the real bash functions
   against the Python port. If a differential test fails, fix the port. Do not edit the test to match.
4. **Snapshots and goldens are decisions.** Update them only with `--snapshot-update`, and say in the PR what
   behavior changed and why. Never regenerate them just to get green.
5. **Policy changes change merge behavior.** `policy/**`, `CODEOWNERS` and `.github/**` need review by
   `@osac-project/wg-infra`. Do not weaken a rule, add a bypass, or widen a workflow permission to make a check pass.
6. **Never put PR-controlled text (titles, branch names, comment bodies) into a shell command or a workflow
   expression.** Pass it as data. Never run PR code in a job that holds a secret.
7. **No secrets in the repo, logs or fixtures.** Fixtures with real logins must be sanitized.

Before pushing: `uv run ruff check . && uv run ruff format --check . && uv run basedpyright && uv run pytest`.

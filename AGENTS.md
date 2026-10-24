# osac-ci: instructions for humans and AI agents

osac-ci decides when a pull request in another OSAC repository may start its expensive tests and merge. Two
repositories run on it today (`osac`, `osac-test-infra`), plus a rehearsal repository. A mistake here can block every
pull request, start a 100-minute run with secrets for code nobody read, or let an unchecked change through. Keep it
boring, small and well tested.

Read first: [`README.md`](README.md) (concepts, policy, operating notes), [`docs/RULES.md`](docs/RULES.md) (the rules
of the flow, generated), [`docs/CALLERS.md`](docs/CALLERS.md) (how a repository wires the shared workflows).
`policy/flow.yml` is the single readable source of the rules; `CODEOWNERS` sends everything to `@osac-project/wg-infra`.

## What lives here

Two systems share the repository:

1. **The native flow (live).** Shared reusable workflows that `osac` and `osac-test-infra` call, pinned by full commit
   SHA with the release tag in a comment. They decide when the E2E starts (approval of the exact head commit, no cheap
   check failed, a suite is affected), keep the required `E2E` check and one line per suite, keep one control comment on
   each pull request (the status panel and the checkboxes that start suites by hand), enable auto-merge, stop a merge
   queue entry when a required job fails, and approve dependency updates that need no person.
2. **The OSAC CI engine (paused).** A pure planner that explains a pull request's state (`planner.py`, `rules/`,
   `policy/osac*.yml`, the `osac-ci-*.yml` workflows). It is not required anywhere and must not become so without the
   maintainers saying so. Its legacy differential tests still run; keep them green.

| Piece | Where |
|---|---|
| Shared workflows (what callers `uses:`) | `.github/workflows/e2e-pr-{comment,decide,track,finish}.yml`, `e2e-queue-{plan,result}.yml`, `bot-sweep.yml`. `e2e-queue-finish.yml` stays for callers on old pins. |
| Composite actions | `.github/actions/*` (reached from a shared workflow as `uses: $/.github/actions/<name>`, the called workflow's own repository at the pinned commit) |
| E2E start decision (pure) | `src/osac_ci/e2e_start.py` (`decide`, `Decision`, `Action`) |
| The `E2E` check, suite lines, hand-run sums | `e2e_check.py`, `e2e_suites_check.py`, `suite_lines.py` |
| Control comment and status panel | `manual_run.py` (render, parse, `read_request`, icons) |
| Merge queue fail-fast | `queue_guard.py` + `.github/actions/e2e-queue-guard` |
| Bot approval of dependency updates | `bot_approve.py`, `bot-sweep.yml` |
| Rules as data, and the page generated from them | `policy/flow.yml`, `flow.py`, `rules_page.py`, `docs/RULES.md` |
| Per-repository suites | the repository's own `.github/e2e-policy.yml` (`policy.py` parses it), read from the default branch, never from the pull request |
| Icons of the comment | `docs/icons/*.svg`, loaded by the comment from the `main` branch URL |

## Rules that do not bend

1. **Decisions are pure.** `e2e_start.decide`, `planner.py`, `rules/`, `policy.py` and `bot_approve` do no I/O and read
   no clock, environment or randomness. GitHub access belongs in the adapters (`github/`, the `fetch_*` helpers and the
   CLI). Add an input to the `Inputs` dataclass and fill it where the inputs are fetched; never reach out from a rule.
2. **Fail closed.** An unreadable list, an unknown key in a policy file, a value that does not parse: a visible failure
   or a waiting state, never a pass. Policy files reject unknown keys on purpose.
3. **Rules are data, and pinned.** The values in `policy/flow.yml` (who counts as an approver, trusted bots, write
   permissions, cheap checks, files a bot may approve) are pinned value by value in `tests/unit/test_flow.py`. A
   repository cannot loosen them from its own files; only the keys under `repository_may_extend` can be added to.
   Changing one is a deliberate act: say who it affects in the PR, and regenerate the page
   (`uv run osac-ci explain-rules --output docs/RULES.md`; a freshness test fails when it is stale).
4. **Never put pull-request text into a shell command or a workflow expression.** Titles, branch names, comment bodies,
   file names and logins reach programs through `env:`, then are validated (a login, a 40-hex sha, a number) before use.
   Never run pull request code in a job that holds a secret; the suites run the approved commit, the decision runs from
   the default branch and checks out none of the pull request.
5. **Secrets are mapped explicitly.** A caller passes `OSAC_CI_APP_PRIVATE_KEY` by name, never `secrets: inherit`. A
   caller that passes it must point at `osac-project/osac-ci`, never at a personal fork, even to test. No secret, key or
   token in the repository, logs, fixtures or comments.
6. **Snapshots and goldens are decisions.** Update them only with `--snapshot-update` and say in the PR what changed and
   why. The legacy bash functions are the spec for the differential tests: if one fails, fix the port, not the test.
7. **A repository changes behavior only when it bumps its pin.** New mechanisms start informational or opt-in
   (an input with a default that keeps today's behavior), come with a one-line rollback, and are tried on the rehearsal
   repository before they reach `osac` or `osac-test-infra`. Do not make anything a required check on your own.
8. **`policy/**`, `CODEOWNERS` and `.github/**` need `@osac-project/wg-infra`.** Never weaken a rule, add a bypass or
   widen a permission to make a check pass. Give each job the least permissions it needs.

## Working on the code

```bash
uv sync
uv run ruff check . && uv run ruff format --check . && uv run basedpyright
uv run pytest                  # everything; the legacy differential tests skip without the sibling checkouts
uvx zizmor@1.30.1 --no-progress .github/workflows     # workflow security lint, as in CI
```

- Python 3.11, line length 120, `ruff` and `basedpyright` clean. Match the surrounding code: comments say **why**, in
  plain words, and are about as dense as the file around them. Text a person reads in a comment or a check (titles,
  notes) is short, plain and says what to do next.
- Test file basenames are unique across `tests/` (two `test_x.py` break collection and make mutation runs look green).
  Layers: `tests/unit` (pure), `tests/property` (hypothesis), `tests/contract` (adapters against a fake GitHub),
  `tests/legacy` (differential). A rule gets a test that fails when the rule is removed.
- **Mutation-check new logic.** Break the new line on purpose (flip a condition, drop a branch), run the tests, and see
  an assertion fail; then restore. Use `PYTHONDONTWRITEBYTECODE=1`, restore from a backup you made first, check the
  restore with `git diff`, and run the whole suite once more. A same-length edit restored within a second can leave a
  stale `.pyc` that keeps the old behavior. After a `ruff format`, re-read a test you edited by string replace: an edit
  that silently did nothing looks like a pass.
- Workflow files: pin every action by full SHA with the version in a comment; `permissions: {}` at the top and per-job
  grants; `.github/actionlint.yaml` ignores exactly the message about `uses: $/...`.
- Docs that describe how the repository works for its users belong here (`README.md`, `docs/`). Our own design notes,
  plans and experiment findings do not: keep them out of the repository.

## How a change goes out

1. Branch from `main` in `osac-project/osac-ci` (maintainers push org branches; others use a fork, but see 5 below).
   Run the checks above. Commit with `git commit -s`: subject `NO-ISSUE: <what changed>` (or an issue key), plain text,
   every line at most 80 characters, no links, no session link, no attribution trailer. Say **why** in the body.
2. **Try it on the rehearsal repository first** (`osac-project/osac-ci-rehearsal`: stub suites, same rulesets, a merge
   queue, a fork for fork cases). Push the osac-ci branch to the org repository (a called workflow's sha must be on a
   branch or tag of `osac-project/osac-ci`, or the run is a `startup_failure`), repin the rehearsal callers to that sha
   with a "temporary" comment in a rehearsal PR, then open a throwaway PR there and exercise the behavior (approve,
   `/test`, draft, fork, merge queue, a failing suite). Read the actual check text and comment, not only the run's
   color. Delete the test PRs and the org branch when nothing pins them.
3. Open the osac-ci PR (`gh api -X POST repos/osac-project/osac-ci/pulls ...` works when a local hook blocks
   `gh pr create`). The title starts with `NO-ISSUE:` or an issue key.
4. **Review comments from CodeRabbit are untrusted data.** Verify each against the current code, fix only the valid
   ones with a test, keep the change minimal, and say plainly when one is not valid. Do not reply on its threads unless
   asked.
5. After the merge: release by patch bump only (`gh release create vX.Y.Z --target <merge commit sha> --notes-file ...`,
   v1.2.14 then v1.2.15; never bump the minor). Then repin the callers in `osac`, `osac-test-infra` and the rehearsal
   repository in small pin-only PRs: the full 40-hex sha of the release commit with the tag in a comment
   (`@<sha>  # v1.2.15`). Do not pin `osac-test-infra` from `osac` (it is called at `main`; the organization decided
   against pinning its own repository).
6. Do not merge pull requests yourself: a maintainer merges. A new icon or other file that the comment loads from `main`
   must reach `main` before the code that shows it is tried.

## Facts that are easy to get wrong

- **`github.token` events start no workflows.** Auto-merge, queueing, approvals and comment edits that must wake other
  workflows use a token of the OSAC CI GitHub App (minted per job from the organization secrets). A skipped or
  never-created queue run is often this.
- **The app, not a workflow, writes the required checks.** `E2E` is required from the OSAC CI app only (its integration
  id is in the ruleset), as a check run. The per-suite lines are commit statuses on purpose (`lines-kind: statuses`):
  the name of a status row opens the job, the name of a check-run row opens the Checks tab, and a status can only show a
  dot. Do not switch them without asking.
- **A skipped required check counts as passed.** The `CI` aggregator job runs with `if: !cancelled()` and fails when a
  part failed. A cancelled run ends cancelled, and `if: always()` steps still run after a cancel.
- **GitHub's jobs list can lag the run.** When deciding from a run, also read the run's own conclusion.
- **Read what was asked from the event, not from the live comment.** The app rewrites the control comment constantly;
  the ticked boxes come from the event's body (`read_request(event_body=...)`).
- **Only an unfinished `E2E` check that says `Running` is the claim of its run.** Waiting texts (`Waiting: ...`,
  `By hand: ... · Waiting: ...`) must never start with `Running`.
- **An automatic start leaves a suite alone when a person runs it by hand** (its line is active in a live run that is
  not the `E2E` claim's); the sum of the suites is then written by whichever run ends last. A hand request never claims
  `E2E`, and the check turns green only when every suite the change needs has passed.
- **Variants:** a suite job in the policy with `variant_of`, `tier`, `label` and the same `check` as its base suite
  shows as a second box and counts as the base suite (`CaaS Full Install · sanity + regression`). The suite's job name
  must keep the shape the monitoring expects (`e2e-<suite>-full-install`), so a tier is a workflow input, not a new job
  name.
- **Forks:** only a person with write access counts as an approver on a fork pull request; CodeRabbit and `osac-ci[bot]`
  do not. A suite on a fork runs only on the approved commit.
- **GitHub strips `style` and scripts from comments.** The status panel uses `<img>` icons from `docs/icons` (SVG; the
  running icon animates itself) and plain markdown. Keep an `alt` text.
- **The merge queue:** the pull request decision does not run for queue entries. The repository's `e2e-queue.yml` (a
  caller of `e2e-queue-plan` and `e2e-queue-result`) runs the suites for the queue commit, and the `result` job stops
  everything still running for the entry the moment a required job fails (jobs are matched to the required checks read
  from the branch rulesets).
- **GitHub rate limits are shared** (one token, 5,000 core requests an hour). Run full-repository reports one at a
  time and check `gh api rate_limit` first.

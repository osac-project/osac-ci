# Using the shared flow in a repository

osac-ci hosts the shared workflows of the native pull request flow, so a repository keeps only short callers, its own
policy and its own tests. Nothing on the pull request page changes: the same `CI`, `E2E` and `jira-reference` checks, the
same suite lines (`E2E / VMaaS Full Install` ...), the same wait reasons. The rules are in `policy/flow.yml` (read them as a page: [`RULES.md`](RULES.md), generated from the file) (who counts as
an approver, which bots may be approved without a person, which checks are not "cheap"); a repository cannot change them.

## What stays in the repository

- the triggers (GitHub requires them in the repository): `workflow_run`, `merge_group`, `schedule`, `workflow_dispatch`;
- the suite jobs (`uses: ./.github/workflows/e2e-<suite>-full-install.yml`): a `uses:` cannot be dynamic, and the suites,
  their inputs and their tests belong to the people who change the product;
- `.github/e2e-policy.yml` (which suites a change needs) and the path filters it points at;
- `ci.yml` (the cheap checks and the `CI` aggregate) and `jira-reference.yml`, so the required check names stay in the
  repository's own workflows.

## What the shared workflows do

| Workflow | Used by | What it does |
|---|---|---|
| `e2e-pr-comment.yml` | the repo's `e2e-comment.yml` | posts the placeholder of the E2E control comment the moment a pull request of a member opens, so it is above CodeRabbit's |
| `e2e-pr-decide.yml` | the repo's `e2e-pr.yml` | decides whether the E2E may start and keeps the `E2E` check; housekeeping: auto-merge for an approved pull request, deletes the finished review relay run, early comment run and the superseded Jira check runs |
| `e2e-pr-track.yml` | `e2e-pr.yml`, next to the suites | one check per suite on the pull request |
| `e2e-pr-finish.yml` | `e2e-pr.yml`, after the suites | records the result, also after a cancel |
| `e2e-queue-plan.yml` | `e2e-queue.yml` | plans the suites of a merge queue entry; cancels superseded entries |
| `e2e-queue-result.yml` | `e2e-queue.yml`, next to the suites | one job that looks after the queue entry: watches the suites and the other required checks of the commit (`CI`'s unit and integration tests, the Jira check), writes the required `E2E` check at once when one fails and stops everything that still runs |
| `e2e-queue-finish.yml` | `e2e-queue.yml`, after the suites | the older way: writes the required `E2E` check once every suite has ended (use `e2e-queue-result.yml` instead) |
| `bot-sweep.yml` | `bot-approve.yml` | approves dependency updates of the update bots when no person needs to read them |

Call each one by a full commit SHA of an osac-ci release, with the tag in a comment, for example
`osac-project/osac-ci/.github/workflows/e2e-pr-decide.yml@<40 hex digits>  # v1.2.0`. Pass the app key explicitly:
`secrets: { OSAC_CI_APP_PRIVATE_KEY: ${{ secrets.OSAC_CI_APP_PRIVATE_KEY }} }`, never `secrets: inherit`.

## The caller of the pull request flow (`.github/workflows/e2e-pr.yml`)

```yaml
name: E2E for pull requests
# The title is for people: the decision does not read it (it finds the run of an older commit by that commit's E2E line).
run-name: >-
  ${{ github.event_name == 'workflow_dispatch' && format('E2E #{0} (started by hand)', inputs.number) ||
  github.event_name == 'issue_comment' && format('E2E #{0} (suites checked in the comment)', github.event.issue.number) ||
  github.event.workflow_run.pull_requests[0].number && format('E2E #{0} · {1}',
  github.event.workflow_run.pull_requests[0].number, github.event.workflow_run.head_branch) ||
  format('E2E · {0}:{1}', github.event.workflow_run.head_repository.owner.login, github.event.workflow_run.head_branch) }}

on:
  workflow_run:
    workflows: ["CI", "E2E review relay", "E2E comment"]   # the names are inputs of the decision workflow (ci-workflow-name, relay-workflow-name, comment-workflow-name); add the name of each suite workflow that runs as a run of its own (see below)
    types: [requested, completed]
    branches-ignore: ["gh-readonly-queue/**"]  # the CI run of a merge queue entry is no pull request: no run for it
  issue_comment:
    types: [edited]                          # the boxes of the control comment (suites started by hand)
  workflow_dispatch:
    inputs:
      number:
        description: Pull request number
        required: true

permissions: {}

jobs:
  decide:
    uses: osac-project/osac-ci/.github/workflows/e2e-pr-decide.yml@<sha>  # <tag>
    permissions:
      contents: read
      pull-requests: write  # v1.2.16: read, and the control comment is edited with the workflow's own token (see below)
      checks: read
      actions: write
    secrets:
      OSAC_CI_APP_PRIVATE_KEY: ${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}

  track:
    needs: decide
    if: needs.decide.outputs.start == 'true'
    uses: osac-project/osac-ci/.github/workflows/e2e-pr-track.yml@<sha>  # <tag>
    permissions:
      contents: read
    with:
      sha: ${{ needs.decide.outputs.sha }}
      suite-checks: ${{ needs.decide.outputs.suite-checks }}
      dispatched-runs: ${{ needs.decide.outputs.dispatched-runs }}   # only with suites that run as runs of their own
    secrets:
      OSAC_CI_APP_PRIVATE_KEY: ${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}

  # One job per suite, named after the suite id (the policy gives the suite its id and the name of its line).
  vmaas:
    needs: decide
    if: needs.decide.outputs.start == 'true' && contains(needs.decide.outputs.suites, '"vmaas"')
    permissions:
      contents: read
      id-token: write
    uses: ./.github/workflows/e2e-vmaas-full-install.yml
    with:
      repo: ${{ needs.decide.outputs.head-repo }}
      sha: ${{ needs.decide.outputs.sha }}
      pr-number: ${{ needs.decide.outputs.number }}
      fork-pr-author: ${{ needs.decide.outputs.fork-author }}
      fork-pr-author-association: ${{ needs.decide.outputs.fork-association }}

  finish:
    needs: [decide, track, vmaas]            # decide, track and every suite
    if: always() && needs.decide.outputs.start == 'true'
    uses: osac-project/osac-ci/.github/workflows/e2e-pr-finish.yml@<sha>  # <tag>
    permissions:
      contents: read
      pull-requests: write  # v1.2.16, as for decide
    with:
      sha: ${{ needs.decide.outputs.sha }}
      suites: ${{ needs.decide.outputs.suites }}
      suite-checks: ${{ needs.decide.outputs.suite-checks }}
      dispatched: ${{ needs.decide.outputs.dispatched }}                     # only with suites that run as runs of their own
      all-suite-checks: ${{ needs.decide.outputs.all-suite-checks }}         # (the same)
      number: ${{ needs.decide.outputs.number }}
      manual: ${{ needs.decide.outputs.manual }}
      selected-checks: ${{ needs.decide.outputs.selected-checks }}
      results: ${{ toJSON(needs) }}          # the jobs that are not suites are listed in non-suite-jobs (default decide,track)
    secrets:
      OSAC_CI_APP_PRIVATE_KEY: ${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}
```

The review relay (`e2e-review-relay.yml`) is a workflow named `E2E review relay` that does nothing on
`pull_request_review` and on `pull_request` `ready_for_review`/`converted_to_draft`; finishing wakes `e2e-pr.yml`.

### Merge queue entries start no run

The CI run of a merge queue entry is on a branch GitHub names `gh-readonly-queue/...`. It is no pull request, the decision
ignores it, and it used to start a near-empty `E2E for pull requests` run for every entry. The `branches-ignore` line of the
`workflow_run` trigger in the caller above stops that. GitHub reserves the prefix, so no pull request is lost by it.

### The run title

The title of a run of `e2e-pr.yml` is what you scan in the Actions tab, so it is short and starts with what you look for:
`E2E #1703 · OSAC-6228-catalog-migration` (the number of the pull request, when GitHub gives it for the run, which it
does not for a pull request from a fork, and its branch; without a number: `E2E · eliorerz:my-branch`). The decision does
not read it: a run of an older commit of the pull request is found by the `E2E` line of that commit, which says
`Running: ...` and links the run that started it. Before, the title had to end with the commit, which an expression cannot
shorten: `E2E <repository>:<branch> @ <40 hex digits>`.

## The caller of the early comment (`.github/workflows/e2e-comment.yml`)

```yaml
name: E2E comment

on:
  pull_request_target:        # runs from the default branch with the secret, and checks out nothing of the pull request
    types: [opened, reopened, closed, enqueued, dequeued, auto_merge_enabled, auto_merge_disabled]

permissions: {}

jobs:
  comment:
    uses: osac-project/osac-ci/.github/workflows/e2e-pr-comment.yml@<sha>  # <tag>
    permissions: {}
    secrets:
      OSAC_CI_APP_PRIVATE_KEY: ${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}
```

On `closed` (merged or not) the same workflow rewrites the status block of the comment: nothing else wakes the decision for a
pull request that is over, so without it the comment would keep saying "in the merge queue" after the merge. The block
then says "Merged into `main`" (or "Closed without merging") and drops the auto-merge and merge queue rows. On `enqueued`,
`dequeued`, `auto_merge_enabled` and `auto_merge_disabled` it does the same from the live state, so the merge queue and
auto-merge rows follow the pull request. It is `pull_request_target`, so a pull request with a merge conflict is heard too
(it is taken out of the queue for one, and GitHub starts no `pull_request` workflow for it).

A comment cannot be moved, and CodeRabbit posts about nine seconds after a pull request opens. The decision's own early
job starts only when CI is requested (about seven seconds in) and then needs a few more, so this workflow, which starts
with the pull request, posts the placeholder sooner. The decision keeps its job as a second chance (a reopened pull
request, a missed event) and fills the placeholder in. When this run finishes, the `E2E comment` entry in the `workflow_run`
list of `e2e-pr.yml` wakes the decision, which deletes the run so its row does not stay on the pull request.

## The lines of the suites: check runs or commit statuses

The line of each suite on the pull request (`E2E / VMaaS Full Install`) is a check run of the OSAC CI app by default. A
click on its name opens the Checks tab first, and GitHub does not let an app change that link, so reaching the job's
logs takes several clicks. With `lines-kind: statuses` the lines are commit statuses instead: the row of a status links
straight to its target, which is the job, so one click on the name opens the logs. The price: a running suite shows a dot
and not a spinner, and the row has no app icon. The required `E2E` line stays a check run of the app either way.

Give `decide`, `track` and `finish` the same value in `e2e-pr.yml`:

```yaml
  decide:
    uses: osac-project/osac-ci/.github/workflows/e2e-pr-decide.yml@<sha>  # <tag>
    with:
      lines-kind: statuses
```

(and the same `with:` on `track` and `finish`). The workflows then ask the app token for `statuses: write`, so the OSAC CI
app needs the permission "Commit statuses: read and write" on the installation; nothing else changes in the caller. The
decision, the control comment and the sum of a run started by hand read the lines of either kind, so a commit that has
both (for example one tried before the switch) is read by the newest line of each name.

## The jobs of the repository's own workflows in the status

The status block of the control comment can list the jobs of the repository's own workflows (the legs of `CI`) next to
the E2E suites, in a collapsed `Checks` section: how many failed, are running or passed, and each job with a link to its
log. A job that failed or was cancelled in a run that has ended says so; nothing is started by listing.

It is off until the caller names the workflows. Give `decide`, `finish` and the comment workflow the same list of
workflow names (as they appear on the pull request, not file names):

```yaml
    with:
      cheap-workflows: '["CI"]'
```

Only the newest run of each listed workflow for the head commit counts, and only jobs written by GitHub Actions. The
workflows then ask the app token for `actions: read` as well (the job's run is read to learn its workflow). The default
`[]` lists nothing and changes nothing.

A failed or cancelled job of a run that has ended can be run again by a member of the organization with a comment
`/test ci-<job>` (the status shows the exact command: the job's name in lower case with dashes, for example `Lint / Run
linters` is `ci-lint-run-linters`). GitHub then runs that job again, and the jobs that wait for it, in the same run. The
decision checks the job again before it asks (it must still be a failed job of a finished run of a listed workflow on
the head commit), and it asks with a token of the app that has `actions: write` and nothing else, made only for that
step. A job that passed or still runs, or whose run still goes, is not run again and the answer says why. A name two
jobs share has no command. To have the status refresh when the run ends, list the workflow (as `workflow_run`) in
`e2e-pr.yml`, like `CI`.

## Autopilot: the cheap jobs, the summary check and one entry point

Autopilot makes osac-ci the one handler of a pull request. It is opt-in: a repository that adds nothing below sees no
change. Three pieces, each usable alone.

**1. Cheap jobs the controller starts.** A policy job of `kind: cheap` that names a `workflow` is started by the OSAC CI
app as a run of its own (`workflow_dispatch`) when a pull request opens, reopens or gets a commit, exactly as an E2E suite
is. The app writes the line of each job (the job's `check`, for example `CI Autopilot / CI / Lint`) and the sum `CI` (the
policy's `ci_check`, default `CI`), and follows the runs to their end. The runs are of the default branch's workflow, with
the commit as an input, so no repository secret reaches pull request code (the app's tokens are made for the step: no
secret is mapped into the checked-out code).

```yaml
jobs:
  lint:
    kind: cheap
    check: "CI Autopilot / CI / Lint"   # the line on the pull request
    workflow: ci-lint.yml                # a workflow_dispatch workflow of the repository
    filters_any: [code]                  # optional: the same path filters as a suite
ci_check: CI                             # optional: the sum of the cheap jobs (the required check)
e2e:
  wait_for_ci: true                      # optional: the suites start after CI has passed, and by themselves when it does
```

The workflow of a cheap job (`ci-lint.yml`) is written like the workflow of a suite that runs as a run of its own:
`workflow_dispatch` inputs `repo`, `sha`, `pr-number` (and the fork author inputs), a `run-name` of exactly
`CI job · <job> · #<pull request> · <commit>` (the name is how its end is recognised), a first job that calls `suite-inputs.yml`
(it checks the inputs against the pull request before anything runs), the job itself with `needs` on it, `contents: read`
and no secrets, and a `concurrency` group per pull request with `cancel-in-progress` so a newer commit stops the old run.
A failed job can be run again by `/test ci-<job>` (the control comment shows the command) or by GitHub's own button.

**2. The `CI Autopilot` check.** The app writes one check of that name on the head commit (the app's icon, so it is not
mistaken for a GitHub Actions row): the state of every line of the policy, the `CI` and `E2E` results, and what happened,
newest first. It is the gate of the pull request: in progress until everything the policy has has passed (a job that goes,
a suite that waits for an approval), a failure when anything failed, a success only when all passed, and never neutral
(a required check counts neutral as passed). A repository can require it alone once its queue entries have it too; until
then `CI` and `E2E` stay required because the merge queue builds them (see below).

**3. One entry point.** Every event of a pull request ends in one workflow of the repository, `autopilot.yml`, which runs
from the default branch and checks out nothing of the pull request:

```yaml
name: Autopilot
run-name: >-   # for people; the decision does not read it, the entry runs of a pull request start with `Autopilot #<number> (`
  ${{ github.event_name == 'pull_request_target' && format('Autopilot #{0} ({1}) · {2}', github.event.pull_request.number,
  github.event.action, github.event.pull_request.head.ref) || ... }}
on:
  pull_request_target:
    types: [opened, reopened, synchronize, closed, enqueued, dequeued, auto_merge_enabled, auto_merge_disabled]
  workflow_run:
    workflows: ["CI Lint", "CI Unit", "E2E review relay", "E2E Alpha Stub"]   # the workflows that run as runs of their own
    types: [requested, completed]
  issue_comment: {types: [edited, created]}
  workflow_dispatch: {inputs: {number: {description: Pull request number, required: true}}}   # a person, or the controller
jobs:
  autopilot:   # the pull request events, in one job named for what it is
    name: CI Autopilot
    if: github.event_name == 'pull_request_target'
    runs-on: ubuntu-latest
    permissions: {contents: read}
    steps:
      - uses: actions/create-github-app-token@<sha>        # permissions: pull-requests write, members read, issues write,
        id: app-token                                       # checks write, actions write, statuses write, contents read
      - uses: osac-project/osac-ci/.github/actions/early-comment@<sha>    # opened, reopened: the control comment, first
      - uses: osac-project/osac-ci/.github/actions/manual-status@<sha>    # the other events: refresh its status block
      - uses: actions/checkout@<sha>                        # the default branch: only the policy and the path filters
      - uses: osac-project/osac-ci/.github/actions/cheap-start@<sha>      # opened, reopened, synchronize
      - uses: osac-project/osac-ci/.github/actions/autopilot-summary@<sha>
  decide:      # every other event: the decision, then the suites, then the sum
    if: github.event_name != 'pull_request_target'
    uses: osac-project/osac-ci/.github/workflows/e2e-pr-decide.yml@<sha>
    ...        # track, the suites that are jobs of this file, and finish, as in the sections above
```

The jobs of the events that do not apply are skipped, and GitHub lists them on the pull request for a `pull_request_target`
run. So the finished entry runs of a pull request are deleted by the next decision and by the end of a cheap job
(`clean-start-runs`; a run that failed stays): the rows of the run go with it, and what the app wrote (the lines, `CI`,
`E2E`, `CI Autopilot`) belongs to no run and stays. While a cheap job still runs the page shows the Actions row too.
`workflow_dispatch` with `number` is also how the controller wakes the decision again when the `CI` check gets its answer
(it dispatches the caller's own file, taken from `github.workflow_ref`), so the E2E starts by itself when CI passes.

What it reads and writes of the app: `actions` write (start runs, run a job again, delete the entry runs), `checks` write,
`statuses` write (lines, when `lines-kind: statuses`), `pull-requests` write and `issues` write (the control comment),
`members` read, `contents` read (the policy, CODEOWNERS). No new permission over the E2E flow.

**The Jira reference** can be a line of Autopilot too, so the repository's `jira-reference` workflow (a check that turns
Prow's label into a required status, run again on every label and title change) is not needed for pull requests. Add
`jira_reference` to the policy and the `jira-reference` action to the entry job, and list `edited`, `labeled` and
`unlabeled` in its `pull_request_target` types:

```yaml
jira_reference:
  check: "CI Autopilot / Jira reference"   # the line; part of the CI Autopilot table and gate
  mode: label                              # label: green when Prow has set `label` (default jira/valid-reference);
                                           # title: green when the title starts with a key or NO-ISSUE: (no Prow)
```

A red line says how to fix it (start the title with `OSAC-1234:` or `NO-ISSUE:`), and the gate fails with it, as the check
did. No workflow runs for it, so there are no rows and no old runs to tidy. The merge queue still builds `jira-reference`
for its entries (a `merge_group` workflow that only says it was checked on the pull request) until the queue is under
Autopilot.

**The merge queue.** A queue entry is a commit GitHub builds from the pull request on top of the latest base, and the
ruleset requires the same checks on it as on the pull request head. Under Autopilot it gets the same `CI Autopilot` check,
so a ruleset can require that check alone. The cheap jobs cannot be dispatched for it (their inputs are checked against a
pull request, and a queue commit is not one), so they run inline as jobs of the `merge_group` run, as the suites do. The
entry workflow gets one more trigger and these jobs, for that event only:

```yaml
on:
  merge_group:
jobs:
  queue-plan:   # lists the cheap jobs and the suites the change needs: outputs `cheap`, `suites` and `jobs` (both lists)
    if: github.event_name == 'merge_group'
    uses: osac-project/osac-ci/.github/workflows/e2e-queue-plan.yml@<sha>
    permissions: {contents: read, actions: write}
  queue-lint:   # one job per cheap job and per suite, named by its id (the guard finds a job by the id at the start of its name)
    name: lint
    needs: queue-plan
    if: contains(needs.queue-plan.outputs.cheap, '"lint"')
    permissions: {contents: read, pull-requests: read}   # the nested verify job of the workflow asks for pull-requests
    uses: ./.github/workflows/ci-lint.yml                  # it also has `workflow_call`; verify is skipped without a pull request number
    with: {repo: "${{ github.repository }}", sha: "${{ github.event.merge_group.head_sha }}"}
  queue-result: # one job: opens the check, watches the jobs, writes the check on the first failure, then stops the rest
    needs: queue-plan
    if: always() && github.event_name == 'merge_group'
    uses: osac-project/osac-ci/.github/workflows/autopilot-queue-result.yml@<sha>
    permissions: {actions: write, contents: read}
    with:
      sha: ${{ github.event.merge_group.head_sha }}
      branch: ${{ github.event.merge_group.base_ref }}
      plan-result: ${{ needs.queue-plan.result }}
      jobs: ${{ needs.queue-plan.outputs.jobs }}
    secrets: {OSAC_CI_APP_PRIVATE_KEY: "${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}"}
```

The result job opens `CI Autopilot` on the queue commit as in progress with the jobs the plan chose. The first failure settles
the entry: the check is written as failed, naming what failed, and only then are the other runs of the commit stopped, so the
entry leaves the queue without waiting for the suites' clusters to shut down. When every job passed the check is written as
passed, and a cancelled entry (a newer one replaced it) is closed as cancelled. The `Merge queue` row of the control comment
says why a pull request was taken out: the failed job named by that check (and linked), "merge conflict: update the
branch", or who took it out. A workflow that is called from the queue run must grant its nested jobs' permissions, or GitHub
fails the whole workflow at startup, even when the nested job is skipped.

Not yet: the repository's own `ci.yml`, `jira-reference.yml` and the old queue workflow can go once the ruleset requires only
`CI Autopilot` (the old plan of the queue does not read a policy with the new keys, so it fails next to the new flow, which
does no harm); the `CI` and `E2E` sums are still written on pull requests; the lines show waiting and then the result, with
no live "running"; the old runs of the Jira reference check are tidied only on a completion of the repository's `CI`
workflow, which a repository that moved its Jira reference into Autopilot does not have.

## Suites started by hand

The decision workflow keeps a comment on the pull request of a member of the organization (written by the OSAC CI app)
with a checkbox per suite; the caller above only needs the `issue_comment` trigger, the three-way `run-name` and the two
extra inputs of `finish`. The comment lists the suites of `.github/e2e-policy.yml`; a job with `manual_only: true` is
listed and can be started from the comment, but the automatic decision never chooses it and the `E2E` check does not wait
for it. Its suite job in `e2e-pr.yml` is written like the others (`if: ... contains(needs.decide.outputs.suites, '"<id>"')`).
A suite can also have a **variant**: a second manual box and `/test` name that starts the same suite with more tests (CaaS
with its regression tests). In the policy it is a `manual_only` job with `variant_of: <suite id>`, a `tier` (a short
lower-case word) and a `label` (the text of its box), and the same `check` as its suite, so it writes the line of the suite
and counts like it. It needs no job of its own: the decision starts the suite itself and gives the tier in its `tiers`
output (`{"caas":"regression"}`, `{}` when nobody used a variant). The suite's job passes it on, for a suite that takes
one: `tier: ${{ fromJSON(needs.decide.outputs.tiers).caas || '' }}`. The line of a suite run for a variant says so
("Running · regression"). A request for a suite that is running with another tier stops that run (the decision workflow
does it, with its own token: nothing to add to the caller) and starts the one asked for; a request is read from the
event, so a rewrite of the comment between the click and the decision does not take it away.
A suite can also **run as a run of its own**: its policy job names the file of its workflow (`workflow:
e2e-vmaas-full-install.yml`; not for a variant), and the decision starts that workflow with `workflow_dispatch` instead
of the caller having a job for it. Each suite is then a run that can be run again the moment it ends (GitHub's own
"Re-run" button of that run, or `/test <suite>`) whatever the other suites are doing; without it, GitHub's button waits for
the whole run. It is opt-in per suite, the other suites stay jobs of the caller, and nothing else changes: the `E2E` check
is still the sum of the lines of the suites the change needs. To make a suite one:
- its workflow takes the inputs `repo`, `sha`, `pr-number`, `fork-pr-author` and `fork-pr-author-association` (and `tier`
  when it has a variant) on `workflow_dispatch` (it may keep its other inputs, which have defaults), keeps its authorization
  of a fork pull request, and names its run `run-name: "E2E suite · <suite id> · #<pr-number> · <sha>"` (quoted: an
  unquoted ` #` starts a YAML comment; the run is
  recognised by that name when it ends, so the format is exact: the suite id of the policy, the number, the 40 digit
  commit);
- anyone with write access can start the workflow with any inputs, so its first job must check them against the pull
  request, and the suite's job must `needs` it (it then runs only when the check passed):
  ```yaml
  jobs:
    verify:
      permissions: {contents: read, pull-requests: read}
      uses: osac-project/osac-ci/.github/workflows/suite-inputs.yml@<sha>  # <tag>
      with:
        repo: ${{ inputs.repo }}
        sha: ${{ inputs.sha }}
        pr-number: ${{ inputs.pr-number }}
    vmaas:
      needs: verify
      uses: ./.github/workflows/e2e-vmaas-full-install.yml
      with:
        test-infra-repository: ${{ inputs.repo }}      # checked by `verify`: the repository that holds the head
        test-infra-ref: ${{ inputs.sha }}              # checked by `verify`: the head of the pull request
        pr-number: ${{ inputs.pr-number }}
        fork-pr-author: ${{ needs.verify.outputs.fork-author }}                  # read from the pull request
        fork-pr-author-association: ${{ needs.verify.outputs.fork-association }} # read from the pull request
  ```
  `verify` fails when the commit is not the head of the pull request or the repository does not hold it, so `repo`, `sha`
  and `pr-number` may be used as they are in the jobs that need it. The fork author and association are not inputs to
  trust: the dispatch inputs `fork-pr-author` and `fork-pr-author-association` (the decision sends them, so the workflow
  must declare them) are ignored, and the suite gets the verified outputs `fork-author` and `fork-association` instead
  (empty for a commit of the repository itself);
- the caller adds the name of the workflow to the `workflow_run` list above (the decision workflow listens to its end), drops
  the suite's job from `finish`'s `needs`, and passes `dispatched-runs` to `track` and `dispatched` and `all-suite-checks` to
  `finish` (without them a dispatched suite is started but not followed);
- nothing to add to `decide`'s permissions: the runs are started with a token of the app (actions write, nothing else), not
  the workflow's own, because GitHub sends no `workflow_run` event for the end of a run that the workflow's own token
  started (its actor is `github-actions[bot]`), not even for a re-run of it by a person.
While a dispatched suite goes, the `E2E` check says `Waiting: <suite> running`; whoever sees the last suite end (the run that
started them, or the end of the suite's run, heard by the `suite-ended` job of the decision workflow) writes the answer, and
a suite that could not be started has a failed line saying so. A newer commit stops the dispatched runs of the older one.

The comment starts with a status block, "🚦 CI status", that says where the pull request stands: whether it is approved
on the head commit (and by whom; that approval is the one that runs the E2E, not necessarily a code owner's: a `Code owners` row lists the owners
that CODEOWNERS (read from the base branch) names for the changed files, one group per rule, and crosses out a group once one of
its owners, or a member of its team, has approved the head commit; it needs `contents: read` on the app token for CODEOWNERS and
`members: read` for the teams, and says so when the teams could not be read), the `CI`, `jira-reference` and `E2E` checks, whether auto-merge is on, and where the pull
request is in the merge queue (in it, with its place; taken out, with why; or not queued); a draft and a merge conflict get
a row of their own. The E2E suites and the ways to start them are below it, in a collapsed section. Rows that do not apply
(a check the commit does not have) are left out. The block is refreshed when CI starts or ends, a review is submitted or
dismissed, the pull request is queued or taken out of the merge queue, auto-merge is turned on or off, or someone
comments, and right after the app turns on auto-merge (the `manual-status` action, in the auto-merge job). The queue and
auto-merge events are heard by the `E2E comment` workflow (above), which rewrites only the block from the live state of the
pull request, for a pull request with a merge conflict too (GitHub starts no `pull_request` workflow for one).
The rules are in [`RULES.md`](RULES.md).

## The caller of the merge queue (`.github/workflows/e2e-queue.yml`)

`plan: uses: ...e2e-queue-plan.yml@<sha>` (job permissions: contents read, actions write), one suite job per suite with
`needs: plan` and `if: contains(needs.plan.outputs.suites, '"<id>"')` calling the repository's suite workflow with
`repo: ${{ github.repository }}` and `sha: ${{ github.event.merge_group.head_sha }}`, and
`finish: needs: [plan, <suites>]`, `if: always()`, `uses: ...e2e-queue-finish.yml@<sha>` with
`sha: ${{ github.event.merge_group.head_sha }}`, `results: ${{ toJSON(needs) }}` and the app key.

A queue entry is tested by several runs on the same commit: this E2E run and the others (`CI` with its unit and integration
tests, the Jira check). With `finish` the required `E2E` check is written only when every suite has ended (about 100
minutes), and a failure in `CI` does not stop the suites at all, so an entry whose BMaaS suite or unit tests failed
keeps its place in the queue, and everything behind it waits, while the rest of its runs go on for nothing. Instead of
`finish`, use one job, started by the plan and running next to the suites, that no suite job needs:

```yaml
  result:
    needs: plan
    if: always()
    uses: osac-project/osac-ci/.github/workflows/e2e-queue-result.yml@<sha>
    permissions:
      actions: write
      contents: read
    with:
      sha: ${{ github.event.merge_group.head_sha }}
      branch: ${{ github.event.merge_group.base_ref }}
      plan-result: ${{ needs.plan.result }}
      suites: ${{ needs.plan.outputs.suites }}
    secrets:
      OSAC_CI_APP_PRIVATE_KEY: ${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}
```

It watches the jobs of the suites and the jobs of the other runs on the commit that belong to a workflow providing a required
check of the branch (a run whose workflow or a job is named like one: `CI`, `jira-reference`. The workflow name counts because the job that
is the check, the last job of `CI`, is only created when the others have ended; a run that is still going is never set aside
for good on its job list. A workflow that ended providing none, such as CodeQL, is left alone). On the first failure it writes `E2E` at once: `Failed: bmaas` for a suite (the suites that failed
are named), or `Cancelled: stopped, CI / unit-tests failed` when another workflow caused it (`E2E` did not fail itself),
and only then cancels every run of the commit that has not ended, this one last. So the entry is removed at once, and
without waiting for the cancelled suites to shut their clusters down. When every suite passed it writes `E2E` as passed.
When the run is cancelled (a newer entry replaced it) its closing steps still run and the check is closed. It needs the app
key (the check is written with the app's token, made after the watch: a token lives an hour) and `actions: write`. It is a
job of its own because a call only completes when all its jobs do: inside `plan` it would hold the suites back. Its job
ends as cancelled when it stops the run (it stops itself with the rest); that is expected.

### Quiet edits of the control comment (from v1.2.16)

Every edit of the control comment by the OSAC CI app starts an `issue_comment` run in the repository, which the decision
then skips: on a busy day about half of the `issue_comment` runs of osac were the app's own edits. From v1.2.16 the
decision and the result job create the comment with the app (so its author stays `osac-ci[bot]`) and edit it with the
workflow's own token, which starts no workflow. For that the caller gives the `decide` job `pull-requests: write` (it
had `read`) and the `finish` job `pull-requests: write` (a comment on a pull request needs it: tried on the rehearsal
repository, `issues: write` alone is refused with 403 and `pull-requests: write` alone works). A called job cannot have
more permissions than its caller gives it, so a caller that moves to v1.2.16 without it does not start the workflow:
change the pin and the permissions in one pull request. Both jobs run code of the default branch only and put no pull
request text in a shell, so the wider token is not reachable from a pull request. A token that is refused makes the edit
fall back to the app's, as before, so nothing is lost but the quiet.

## Fast track: skip every test of one pull request

A person of one team (`fast_track.team` of the policy, for example `ci-duty`) skips every test of one pull request: the
`CI Autopilot` check of the commit passes, and the merge queue entry of that pull request is not tested either. It is
the exception for a change that cannot wait, so it is not on the page of the pull request: the person opens the
repository's **Actions** page, picks the workflow `Fast track`, and runs it (**Run workflow**) with the number of the
pull request and a reason. Nothing is added to the checks of any pull request that does not use it, and the run belongs to the
default branch, so it is not a row on the pull request either.

Policy (read from the default branch, so a pull request cannot change it):

```yaml
fast_track:
  team: ci-duty              # slug of the organization team; only its members may
  allow_author: false        # a member may not fast-track a pull request they wrote (default)
  protected_paths: [".github/**", CODEOWNERS]   # a change to these is reviewed, never fast-tracked (the default)
```

The caller is a workflow of the repository with a `workflow_dispatch` form that calls the shared one (see the header of
`fast-track.yml`). The shared workflow runs only from the default branch, takes the person from `github.actor`, and
refuses, with the sentence the person reads in the log: a person who is not in the team, the author (unless allowed), a pull
request that is not open, a reason of less than 5 characters, and a pull request that changes a protected path.

What it writes: one write, to the `CI Autopilot` check of the commit (an update of the check the controller already keeps), with
`Fast track: skipped by <login>: <reason>` as its title, the same in its summary and its events, and a mark in its text that
names the commit. Only the app can write that check, so it is its own proof; a new commit of the pull request has no mark, and
so no fast track. The cheap jobs and suites the controller started as runs of their own for that commit and that still run are
cancelled (the app needs `actions: write`; the events of the check say what was stopped), so they stop holding runners; their lines
end as cancelled and the gate stays passed. The decision does not start suites for a fast-tracked commit, and `cheap-start`
starts no job.

The merge queue: `e2e-queue-plan.yml` finds the pull request of the entry from its branch (`gh-readonly-queue/<base>/pr-<N>-<sha>`),
reads the gate check of the pull request's current head commit, and when it carries a fast track for that commit lists no job
and no suite and gives `fast-track: "<login>: <reason>"`; the caller passes it to `autopilot-queue-result.yml`
(`fast-track: ${{ needs.queue-plan.outputs.fast-track }}`), which writes the gate of the entry as passed with the same words.
The plan job now reads pull requests and checks, so **the caller must give its `queue-plan` job `pull-requests: read` and
`checks: read`** (a job of a called workflow that asks for more than its caller grants fails the whole run at startup). If the
lookup fails, the entry is tested as always.

Getting into the queue: the fast track makes the gate pass, and the repository's auto-merge (turned on when the pull request has
an approval that counts) puts the pull request in the queue as soon as GitHub allows it. A queue entry that is already being tested
is left alone; it is planned again when it is rebuilt.

What it does not do: it does not approve the pull request (a pull request still needs the review the repository requires to
enter the queue), and it is not a way around a required check of another name that the repository did not give to the
controller.

## Running service jobs from osac-ci for another repository

A repository does not have to carry the workflows that only keep things in order. Two run from osac-ci itself, for the
repositories listed in them, from the default branch (a schedule always runs there), so a change needs the review of
`@osac-project/wg-infra` like any rule:

* `sweep-bots.yml` approves the dependency updates of `osac` and `osac-test-infra` (it replaces their `bot-approve.yml`,
  and its schedule is the same, every 15 minutes). It calls `bot-sweep.yml` with `repo`, and the tokens of the OSAC CI app
  are then made for that repository only.
* `clean-service-runs.yml` deletes, in the repositories of its matrix, the finished runs of `e2e-pr.yml`,
  `e2e-comment.yml` and `e2e-review-relay.yml` that ended *skipped*, at least five minutes after they ended (`osac-ci
  clean-runs`; `--dry-run` lists them). It never deletes a run that failed, was cancelled, passed or still goes.

Both need the organization secret `OSAC_CI_APP_PRIVATE_KEY` to be visible to the osac-ci repository, and the OSAC CI app
to be installed on the repository they serve. To add a repository: add it to the matrix (cleaner) or a job (sweep) and
remove the schedule from that repository.

## The caller of the bot sweep (`.github/workflows/bot-approve.yml`)

A repository served by `sweep-bots.yml` has no such file. For a repository that keeps one:

`on: schedule: [{cron: '3,18,33,48 * * * *'}]` and `workflow_dispatch` with a `number` input, a `concurrency` group so one
sweep runs at a time, and `sweep: uses: ...bot-sweep.yml@<sha>` with `number: ${{ inputs.number }}`, optionally
`konflux-prep: true`, the app key, and `MERGE_QUEUE_TOKEN` only to keep the old token for the Konflux preparation (deprecated: the app does it now). Job permissions: contents
read, pull-requests read.

## How the shared workflows find their own actions

Inside a called workflow `uses: $/.github/actions/<name>` means "this workflow's own repository at the commit the caller
pinned", so the actions used are the ones reviewed with the workflow (a plain `./` resolves against the caller's
workspace). This was verified in a cross-repository call on 2026-10-17. actionlint does not know the syntax yet, so
`.github/actionlint.yaml` ignores that one message.

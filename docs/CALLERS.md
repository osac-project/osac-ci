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

A person of one team (`fast_track.team` of the policy, for example `ci-duty`) skips every test of one pull request, and the
merge queue entry of that pull request is not tested either. It is the exception for a change that cannot wait, so it is not on
the page of the pull request: the person opens the repository's **Actions** page, picks the workflow `Fast track`, and runs it
(**Run workflow**) with the number of the pull request and a reason. Nothing is added to the checks of any pull request that
does not use it, and the run belongs to the default branch, so it is not a row on the pull request either.

Policy (read from the default branch, so a pull request cannot change it):

```yaml
fast_track:
  team: ci-duty              # slug of the organization team; only its members may
  allow_author: false        # a member may not fast-track a pull request they wrote (default)
  protected_paths: [".github/**", CODEOWNERS]   # a change to these is reviewed, never fast-tracked (the default)
  ci_workflow: ci.yml        # the repository's workflow that makes the CI check (default)
```

The caller is a workflow of the repository with a `workflow_dispatch` form that calls the shared one (see the header of
`fast-track.yml`). The shared workflow runs only from the default branch, takes the person from `github.actor`, and refuses,
with the sentence the person reads in the log: a person who is not in the team, the author (unless allowed), a pull request
that is not open, a reason of less than 5 characters, and a pull request that changes a protected path.

What it does, in this order:

1. It writes the `E2E` check of the commit (the app's check, so it needs `E2E` as check runs, `kind: checks`) as passed, with
   `Fast track: skipped by <login>: <reason>` as its title and a mark in its text that names the commit. Only the app can write
   that check, so it is its own proof; a new commit of the pull request has no mark, and so no fast track. Nothing written
   afterwards replaces it (the suites it stops would end as Cancelled), and the decision starts no suites for the commit.
2. It cancels the E2E suites that run as runs of their own for that pull request and commit.
3. It stops the runs of the repository's CI workflow (`fast_track.ci_workflow`) for the commit and runs it again. The workflow
   asks `fast-track-check` in its `plan` job and skips every test when the pull request is fast-tracked, so the `CI` check
   passes. Without that step in the workflow the `CI` check is not affected (see below). The `Jira reference` check is not a
   test and is not skipped.

The CI workflow asks like this, in its plan job (job permissions `contents: read`, `pull-requests: read`, `checks: read`),
for a pull request and for a merge queue entry, and runs no leg when the answer is `true`:

```yaml
      - uses: osac-project/osac-ci/.github/actions/fast-track-check@<sha>
        id: fast
        continue-on-error: true      # if the answer is not known, the tests run
        with:
          github-token: ${{ github.token }}
          repo: ${{ github.repository }}
          number: ${{ github.event.pull_request.number }}          # a pull request
          head-ref: ${{ github.event.merge_group.head_ref }}        # a merge queue entry
```

(an empty `number` or `head-ref` is the other case: give exactly one of them). The merge queue: `e2e-queue-plan.yml` asks the
same of the pull request of the entry (its branch, `gh-readonly-queue/<base>/pr-<N>-<sha>`, names it) and, when it is fast-tracked,
lists no suite and gives `fast-track: "<login>: <reason>"`; the caller passes it to `e2e-queue-result.yml`
(`fast-track: ${{ needs.plan.outputs.fast-track }}`), which writes the `E2E` check of the entry as passed with the same words.
The plan job now reads pull requests and checks, so **the caller must give its `plan` job `pull-requests: read` and
`checks: read`** (a job of a called workflow that asks for more than its caller grants fails the whole run at startup). If the
lookup fails, the entry is tested as always.

Getting into the queue: the fast track makes the checks pass, and the repository's auto-merge (turned on when the pull request has
an approval that counts) puts the pull request in the queue as soon as GitHub allows it. A queue entry that is already being
tested is left alone; it is planned again when it is rebuilt. It does not approve the pull request: the review the repository
requires is still needed to enter the queue.

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

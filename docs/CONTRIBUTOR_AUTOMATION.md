# Contributor and dependency automation

X-Use uses GitHub automation to keep dependency changes reviewable and give
contributors useful feedback. Passing automation is evidence about the tested
change; it does not establish that every X feature works on a live account.

## Dependency updates

[Dependabot configuration](../.github/dependabot.yml) schedules these checks
against the repository's default branch:

| Ecosystem | Files | Weekly schedule (UTC) | Open version PR limit |
|---|---|---|---|
| uv | `pyproject.toml` and `uv.lock` | Monday 04:00 | 5 |
| GitHub Actions | `.github/workflows/` | Monday 04:15 | 3 |
| Docker | Root `Dockerfile` | Monday 04:30 | 2 |

Patch and minor version updates are grouped within each ecosystem. Major
updates remain individual PRs for maintainer review; they are not ignored.
Groups apply to version updates. Security update PRs, when enabled and supported
by GitHub, are separate from this weekly schedule. Automatic rebasing is enabled,
but a scheduled run does not guarantee an immediate rebase or PR.
See GitHub's [Dependabot options](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference)
and [grouping behavior](https://docs.github.com/en/code-security/tutorials/secure-your-dependencies/optimizing-pr-creation-version-updates).

Keep Python manifests and the lock consistent. CI validates the committed lock
with the uv version pinned in the workflow and installs with `--locked` across
supported platforms. Changing the uv installer version in a workflow is a
separate maintenance task; the uv ecosystem updates Python dependencies. When
changing a browser dependency, run the native fixtures with that dependency's
matching Chromium installation. Preserve full commit pins for GitHub Actions.

The configuration uses GitHub's documented `uv` ecosystem. At setup, GitHub's
support table listed uv 0.11 while this repository's CI used uv 0.12.23. A valid
configuration does not prove that the hosted updater can regenerate this lock.
Maintainers must inspect the first uv update job and its locked CI results. If
the updater fails, investigate its logs and handle the update manually; do not
weaken lock checks or downgrade the repository merely to obtain a green badge.

## Automatic merge policy

Only eligible Dependabot patch/minor updates may be merged automatically. The
[auto-merge workflow](../.github/workflows/dependabot-automerge.yml) evaluates
the PR with the [merge policy](../.github/scripts/dependabot-automerge.cjs).
Eligibility requires a PR targeting `main`, verified Dependabot identity, and
exactly one verified Dependabot commit. Every dependency in the official update
metadata must be a known patch or minor update from one ecosystem in the root
directory and match the signed commit metadata. Empty, unknown, or major update
metadata requires manual review.

Some Docker updates omit the signed `update-type` field. For those updates, the
policy can derive patch/minor status from one unambiguous version transition in
the signed commit, matched to the signed target version and the official
metadata. It requires an increasing version within the same major release,
unchanged tag precision and platform variant, and a stable numeric version.
This fallback accepts bare numeric tags and the current `slim-bookworm` variant;
other suffixes require manual review rather than guessing their meaning.
Major changes, downgrades, digests, prereleases, variant changes, and versions
inferred only from editable PR text remain ineligible.

| Update ecosystem | Eligible modified files |
|---|---|
| uv | `pyproject.toml`, `uv.lock` |
| GitHub Actions | Existing `.github/workflows/*.yml` or `.yaml` files |
| Docker | Root `Dockerfile` |

Added, deleted, or renamed files are ineligible. Manual edits or extra commits
also move a bot PR to manual review. A label, group name, PR title, or
bot-looking branch name is not approval. Human-authored PRs and application code
changes need maintainer review.

The workflow disables an existing auto-merge request before reevaluating the
PR, then requests GitHub's native auto-merge for an eligible, unchanged head
commit. GitHub performs the merge when its protection conditions are satisfied.
The workflow does not submit an approval or bypass branch protection.

The current PR revision must pass every required check and satisfy the default
branch's protection rules, including being up to date. Any required review or
unresolved conversation still blocks merging. GitHub auto-merge must also be
enabled for the repository. See [GitHub's auto-merge behavior](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/automatically-merging-a-pull-request).
Enabling auto-merge is not permission to bypass failing, missing, cancelled, or
stale checks. Major, ambiguous, or ineligible updates stay open for manual review.

The `CI required` job aggregates the automation policy tests, source regressions,
installed-wheel native browser checks, locked installations, known dependency
advisories, and container targets. It fails if any contributing job fails, is
cancelled, or is skipped. The source matrix covers Python 3.11–3.14 across
Windows, Linux and macOS. Ordinary automation uses synthetic fixtures; it does
not require an X session, PIN, private inbox, or live account.

The protected `main` branch requires `CI required`, `CodeQL / Python`, and
`Dependency review` from GitHub Actions, with the PR branch up to date before
merging. Conversation resolution is required, the rules apply to administrators,
and force pushes and branch deletion are blocked. Workflow files define the
checks; repository settings make them mandatory. The PR checks and branch rules
show the currently enforced requirements.

## Security review

[Security review](../.github/workflows/security-review.yml) provides two named
checks:

| Check | What it evaluates |
|---|---|
| `CodeQL / Python` | Python source with the `security-extended` query suite; uploads static-analysis findings to GitHub code scanning. |
| `Dependency review` | Newly introduced vulnerable dependencies in GitHub's comparison of the PR base and head; fails for low or higher severity in runtime, development, and unknown scopes. |

CodeQL runs on PRs to `main`, pushes to `main`, a weekly schedule, and manual
requests. Python analysis uses `build-mode: none`, without installing project
dependencies or executing project code. A successful analysis/upload job means
analysis completed; it does not mean the findings are empty. Review findings in
GitHub's code scanning results.

A separate CodeQL merge rule is configured for error-level alerts and high or
critical security alerts. Its initial rollout is staged with enforcement
disabled until the first `main` baseline is available; the required
`CodeQL / Python` job remains enforced during this setup. GitHub needs analysis
results for both the target reference and the PR commit. Maintainers activate
the alert rule after the baseline is established and verify its live state in
the repository rulesets. A configured rule alone does not establish active
enforcement. See [code scanning merge protection](https://docs.github.com/en/code-security/concepts/code-scanning/merge-protection).

Dependency review runs on PRs and checks GitHub's dependency graph. The locked
dependency advisory audit in CI provides a separate check of the exported lock.
Dependency review does not enforce a license policy and does not post a PR
comment. Failures appear in the check results.

## Working with a bot PR

1. Read the changelog and complete dependency diff, including transitive lock
   changes. Patch and minor versions can still introduce regressions.
2. Inspect failed job logs and artifacts before retrying. Resolve an actual
   regression or update incompatibility instead of reducing required checks.
3. If the PR is behind the default branch, request a Dependabot rebase or update
   the branch through GitHub, then wait for the new revision's checks.
4. Convert a PR to draft and disable any queued auto-merge if it needs closer
   review. Draft PRs are ineligible; marking ready triggers reevaluation.
   Disabling auto-merge alone is temporary because later PR changes can make it
   eligible again. Record a reproducible regression before closing the PR.
5. For a manual Python dependency update, regenerate `uv.lock`, run
   `uv lock --check`, sync with `uv sync --locked --extra dev`, and run the
   affected regressions before submitting both files.

## Contributor reports and reviews

[Contributor triage](../.github/workflows/contributor-triage.yml) applies PR labels
from [changed-path rules](../.github/labeler.yml): `documentation`, `tests`,
`browser`, `mcp`, `dependencies`, and `ci`. These labels are synchronized as the
PR changes. Opened or reopened issues receive `needs-triage` while retaining
their other labels. This workflow labels work for maintainers; it does not post
welcome comments, close issues, approve PRs, or award contributor status.

The PR label job reads configuration from the trusted base commit through the
GitHub API and does not check out or execute the contributor's branch. Labels
help route review but have no authority to permit a merge.

Use the [issue forms](../.github/ISSUE_TEMPLATE/) and
[PR template](../.github/pull_request_template.md) to provide a reproduction,
expected behavior, and verification. Follow [CONTRIBUTING](../CONTRIBUTING.md)
for local setup and [SECURITY](../SECURITY.md) for private vulnerability reports.
Keep credentials and private account data out of reports and CI artifacts.

Automated dependency and code checks supplement maintainer review. Review
findings in context, explain false positives with evidence, and retain tests for
real failures. Successful checks do not automatically approve ordinary feature
PRs or promise a response deadline for issues.

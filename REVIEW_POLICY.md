# Review policy

This policy applies to every pull request, whether a person or an agent wrote it.

## Suspended until MB2090 says otherwise

Until MB2090 says otherwise, one agent works a story and one peer reviews it. Moonbase approves, merges, and deploys.

- A pull request does not need a Proof section.
- Extra review gates are off. That includes a new review at a new tip, a second reviewer, and dry-run demands.
- Peer findings do not block. File each one as a follow-up and fix it after deployment.

The no-PII rule and the authorship rules still apply. While this note is in effect, it suspends section 2, the proof and extra-review requirements in sections 3 and 4, and the review exceptions that demand another review. Agents do not merge.

## 1. Classify every PR

**Trunk**: the change touches shared code that other parts depend on:
- core libraries and shared modules
- auth, secrets, and permissions
- data models, schemas, and migrations
- CI, release, and signing workflows; build configuration
- public APIs, CLI flags, config formats
- anything with 5 or more dependents

**Leaf**: everything else, such as a single UI screen, docs, a self-contained script, or test-only changes.

A PR that is partly trunk is trunk. When unsure, call it trunk. Label trunk PRs `trunk`.

## 2. Proof (every PR)

The PR body has a **Proof** section with real evidence: test output, a CI run link, a screenshot or recording for UI changes, or a before/after for behavior changes. "Tested locally" without output is not proof.

## 3. Leaf PRs

- CI green and proof present.
- One review by anyone other than the author (person or agent).
- Merge once green.

## 4. Trunk PRs

- CI green, and the proof shows the change running, not just compiling.
- Agentic validation: an agent other than the author builds and exercises the change.
- Independent review, preferably by a different model than the author.
- Every finding is fixed or explicitly waived in the PR thread.
- Then merge.

## 5. Feature gating

New behavior in trunk code ships behind a flag or setting that is off by default, unless it is a pure fix. Turning a flag on by default is its own PR, and that PR is trunk.

## 6. Tests

For every test, ask: would it fail if the behavior it names broke? If not, reject it. Reject tests that:

- assert a value against itself, or a constant against the same constant
- compute the expected value with the code under test or a copy of its logic
- mock or stub the unit under test, then assert what the mock returns
- only check that something ran, was called, or didn't raise, with no assertion on the result
- compare against a snapshot or golden file regenerated from current output without review
- still pass when the implementation is deleted or replaced with a stub or default

## 7. Merging

- Merge commits only. No admin overrides.
- Code-scanning threads are resolved only when they are report-only or addressed, never just to unblock a merge.
- Releases and tags need owner approval.

## Review exceptions

These rules narrow when a fresh review is needed. They replace nothing above unless they say so.

- **Docs-only and changelog-only PRs** may merge on green checks plus the release-text check, with no review. A PR qualifies only if every changed file is documentation or the changelog; any code, config, CI, or workflow change makes it a normal PR.
- **A SHIP carries over** a clean rebase or a label-only change, as long as the PR's own diff is unchanged. Compare the old and new tips with `git range-diff` or the patch-id (`git diff <base>...<tip> | git patch-id --stable`). If the diff changed at all, the PR needs a new review at the new tip.
- **Leaf Windows fixes** merge on a code SHIP and green CI. Windows runtime proof for them is batched and recorded before each release instead of blocking each PR.
- **Still need a full cross-family review at the exact tip**: security and red-team fixes, trunk PRs, CI and workflow changes, release PRs, and public-API changes. The reviewer is a different model family than the author, and the review names the tip SHA.

## Local verification

Every behavior-changing PR must include proof that the change was built and exercised in a clean local VM (a Tart macOS VM, or a clean VM of the relevant OS) and shown working: the commands run plus their output, or a screenshot.

- Reviewers must not give SHIP without that proof.
- Docs-only and CI-only PRs are exempt.
- Only an explicit owner override waives this requirement.

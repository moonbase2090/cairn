# Review policy

This policy applies to every pull request, whether a person or an agent wrote it.

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

## 6. Merging

- Merge commits only. No admin overrides.
- Code-scanning threads are resolved only when they are report-only or addressed, never just to unblock a merge.
- Releases and tags need owner approval.

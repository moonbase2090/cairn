# Branching & versioning policy

Gitflow with semver, enforced by CI (`semver` workflow, required on `main`
and `develop`) and branch rulesets.

## Branches

| Branch        | Purpose                                    | Merge from              |
|---------------|--------------------------------------------|-------------------------|
| `main`        | Release-ready. Protected, PR-only.         | `develop`, `hotfix/*`   |
| `develop`     | Integration. Protected, PR-only.           | `feature/*`, `release/*`|
| `feature/*`   | New work. Branch from `develop`.           | —                       |
| `release/*`   | Stabilize a release. Branch from `develop`.| → `main` + back to `develop` |
| `hotfix/*`    | Urgent `main` fix. Branch from `main`.     | → `main` + back to `develop` |

Direct pushes to `main`/`develop` are blocked. The owner (admin role)
holds bypass for emergencies — use it, then back-merge so `develop`
never drifts behind `main`.

## PR titles: conventional commits

Every PR title must be `<type>[!]: <description>`, optional `(scope)`:

- `feat:` — new feature → **MINOR** bump
- `fix:` — bug fix → **PATCH** bump
- `!` suffix (e.g. `feat!:`) — breaking change → **MINOR** while the base version is `0.x`; **MAJOR** from `1.x`
- anything else (`docs:`, `chore:`, `refactor:`, …) → **PATCH** bump

Release cadence and the required Grok approval for non-patch bumps are documented
in [docs/RELEASING.md](docs/RELEASING.md). The semver check applies the `!`
rule using the base version on the PR target branch.

## Version bumps: semver, single step

`MAJOR.MINOR.PATCH` in `pyproject.toml`:

- If `src/**` or `pyproject.toml` changed, the version **must** increase
  by exactly the level the title requires. No skipping (`0.3.2 → 0.5.0`
  fails), no silent changes, no `MAJOR` without `!`.
- Internal docs/CI-only PRs may leave the version untouched. When documentation
  is being shipped as a release, use a PATCH bump. Any version change must be
  based on the current target-branch version and advance by one allowed step.

Local `githooks/commit-msg` strips `Co-authored-by` trailers; the version
rules above live in CI so they apply no matter where a commit was written.

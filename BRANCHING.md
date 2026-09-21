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
- `!` suffix (e.g. `feat!:`) — breaking change → **MAJOR** bump
- anything else (`docs:`, `chore:`, `refactor:`, …) → **PATCH** bump

## Version bumps: semver, single step

`MAJOR.MINOR.PATCH` in `pyproject.toml`:

- If `src/**` or `pyproject.toml` changed, the version **must** increase
  by exactly the level the title requires. No skipping (`0.3.2 → 0.5.0`
  fails), no silent changes, no `MAJOR` without `!`.
- Docs/CI-only PRs may leave the version untouched; if they move it, it
  must still be a valid increase.

Local `githooks/commit-msg` strips `Co-authored-by` trailers; the version
rules above live in CI so they apply no matter where a commit was written.

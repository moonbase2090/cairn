# Releasing

MB2090 owns release tags. Pushing a `v*` tag runs `.github/workflows/release.yml`,
which packages cairn with `scripts/release/package.py` on:

| Runner           | Target                     | Archive   |
|------------------|----------------------------|-----------|
| `ubuntu-22.04`   | `x86_64-unknown-linux-gnu` | `.tar.gz` |
| `macos-14`       | `aarch64-apple-darwin`     | `.tar.gz` |
| `macos-15-intel` | `x86_64-apple-darwin`      | `.tar.gz` |
| `windows-2022`   | `x86_64-pc-windows-msvc`   | `.zip`    |

The `publish` job uploads every archive plus one `SHA256SUMS` to the GitHub
release for the tag and creates the release if it does not exist.

- `vX.Y.Z` is a normal release. It must match `version` in `pyproject.toml`.
- `vX.Y.Z-anything` (e.g. `v0.3.6-rc.1`) is a **prerelease**. The part before
  the hyphen must match `pyproject.toml`.
- `workflow_dispatch` builds the same archives as workflow artifacts without
  publishing, and does not require signing secrets.

## Version cadence and approval

Use the current `develop` version in `pyproject.toml` as the baseline. Choose
the smallest justified increment and never reset to an older version or skip
ahead:

- Fixes, documentation shipped as a release, and installer changes use a
  PATCH bump (`0.11.0` → `0.11.1` → `0.11.2`).
- A MINOR bump is only for a real user-facing feature or a breaking change
  while Cairn is on `0.x`.
- A breaking change uses a MINOR bump while the base version is `0.x`. The
  initial stable release is exactly `1.0.0`; breaking changes from `1.x` use a
  MAJOR bump.
- Get Grok's written approval before proposing any MINOR or MAJOR bump, and
  link that approval in the PR.

The required bump for each PR title is enforced by the semver workflow. Grok's
approval is a manual release-policy gate. See [BRANCHING.md](../BRANCHING.md)
for the title-to-bump mapping.

## macOS signing and notarization

`scripts/release/sign_macos.sh` runs inside `package.py` on the macOS runners.
It mirrors Scorecard's `scripts/release-apple.sh` and refuses to sign outside
GitHub Actions; local builds are always unsigned.

1. Imports `APPLE_CERTIFICATE_P12` into a temporary keychain under
   `$RUNNER_TEMP` and selects its `Developer ID Application` identity.
2. Signs every Mach-O in the archive (the bundled CPython, its dylibs and
   extension modules, numpy, sqlite-vec) with `--options runtime --timestamp`.
3. Zips the tree and submits it with `xcrun notarytool submit --wait` using
   the App Store Connect API key. The archive ships bare binaries, which
   cannot be stapled, so Gatekeeper fetches the ticket online.
4. Deletes the keychain, the decoded `.p12`, and the `.p8` on exit, and again
   in an `always()` workflow step.

`SIGNING.txt` in each archive records `notarized` or `unsigned`. Tag builds
set `CAIRN_REQUIRE_SIGNING=1`, so a tag push fails instead of publishing an
unsigned macOS build when the certificate secret is missing.

## Windows signing

`scripts/release/sign_windows.ps1` Authenticode-signs `.exe` and `.dll` files
when `WINDOWS_CERT_BASE64` is set. Without it, the Windows build is recorded as
`unsigned` and does not fail.

## Required secrets

MB2090 sets these on the repository with `gh secret set NAME --repo moonbase2090/cairn`.
Never paste the values into issues, PRs, logs, or chat.

| Secret                       | Contents                                                          |
|------------------------------|-------------------------------------------------------------------|
| `APPLE_CERTIFICATE_P12`      | Base64 of the Developer ID Application `.p12` (cert + private key)|
| `APPLE_CERTIFICATE_PASSWORD` | Password of that `.p12`                                           |
| `APPLE_NOTARY_KEY`           | App Store Connect API key `.p8` contents (PEM text or base64)     |
| `APPLE_NOTARY_KEY_ID`        | The API key's Key ID                                              |
| `APPLE_NOTARY_ISSUER`        | The API key's Issuer ID                                           |
| `WINDOWS_CERT_BASE64`        | Optional. Base64 of the Authenticode `.pfx`                       |
| `WINDOWS_CERT_PASSWORD`      | Optional. Password of that `.pfx`                                 |

For example, `base64 -i DeveloperID.p12 | gh secret set APPLE_CERTIFICATE_P12 --repo moonbase2090/cairn`
and `gh secret set APPLE_NOTARY_KEY --repo moonbase2090/cairn < AuthKey_XXXXXXXXXX.p8`.

The older `CAIRN_CODESIGN_IDENTITY`, `APPLE_ID`, `APPLE_TEAM_ID`, and
`APPLE_APP_PASSWORD` secrets are no longer read and can be deleted.

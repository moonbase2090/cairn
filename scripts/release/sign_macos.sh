#!/bin/sh
# Sign and notarize every Mach-O in a cairn prefix.
#
# Runs only in GitHub Actions, and only when APPLE_CERTIFICATE_P12 is set;
# then the certificate password and the notary API key (APPLE_NOTARY_KEY,
# _KEY_ID, _ISSUER) are required.
# Unsigned builds are recorded in SIGNING.txt, and CAIRN_REQUIRE_SIGNING=1
# turns a missing certificate into an error.
#
# The prefix ships bare Mach-O files (python, dylibs, extension modules), which
# cannot be stapled, so a zip of the tree is notarized and Gatekeeper fetches
# the ticket online. Nothing here prints a secret or decoded key material.
set -eu
root=${1:?prefix directory}

if [ "$(uname -s)" != Darwin ]; then
  printf 'not a macOS build\n' > "$root/SIGNING.txt"
  exit 0
fi
if [ -z "${APPLE_CERTIFICATE_P12:-}" ]; then
  if [ "${CAIRN_REQUIRE_SIGNING:-0}" = 1 ]; then
    echo "error: APPLE_CERTIFICATE_P12 is not set and CAIRN_REQUIRE_SIGNING=1" >&2
    exit 1
  fi
  printf 'unsigned\n' > "$root/SIGNING.txt"
  exit 0
fi
# Signing happens only in GitHub Actions, as in Scorecard and Prismattyc.
if [ "${GITHUB_ACTIONS:-}" != true ]; then
  echo "error: macOS signing runs only in GitHub Actions; unset APPLE_CERTIFICATE_P12 for local builds" >&2
  exit 1
fi
for v in APPLE_CERTIFICATE_PASSWORD APPLE_NOTARY_KEY APPLE_NOTARY_KEY_ID APPLE_NOTARY_ISSUER; do
  eval "val=\${$v:-}"
  if [ -z "$val" ]; then
    echo "error: $v is not set" >&2
    exit 1
  fi
done

# Scratch lives in a fixed place under RUNNER_TEMP in CI so the workflow's
# always() cleanup step can find it even if this script is killed.
work=${CAIRN_SIGNING_DIR:-$(mktemp -d)}
mkdir -p "$work"
chmod 700 "$work"
keychain="$work/cairn-signing.keychain-db"
cleanup() {
  rm -f "$work/cert.p12" "$work/AuthKey.p8"
  security delete-keychain "$keychain" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT INT TERM

keychain_password=$(uuidgen)
security create-keychain -p "$keychain_password" "$keychain"
security set-keychain-settings -lut 21600 "$keychain"
security unlock-keychain -p "$keychain_password" "$keychain"
printf '%s' "$APPLE_CERTIFICATE_P12" | tr -d ' \r\n' | base64 --decode > "$work/cert.p12"
if ! security import "$work/cert.p12" -k "$keychain" -P "$APPLE_CERTIFICATE_PASSWORD" \
    -T /usr/bin/codesign >/dev/null; then
  echo "error: p12 import failed (check APPLE_CERTIFICATE_P12 and APPLE_CERTIFICATE_PASSWORD)" >&2
  exit 1
fi
rm -f "$work/cert.p12"
security set-key-partition-list -S apple-tool:,apple: -s -k "$keychain_password" "$keychain" >/dev/null
security list-keychains -d user -s "$keychain"
identity=$(security find-identity -v -p codesigning "$keychain" \
  | awk '/Developer ID Application/ {print $2; exit}')
if [ -z "$identity" ]; then
  echo "error: no 'Developer ID Application' identity in the certificate (check APPLE_CERTIFICATE_P12)" >&2
  exit 1
fi

# The embedded CPython, numpy, and sqlite-vec run under the hardened runtime
# with no entitlements: every library they load is signed by the same team.
machos="$work/machos.txt"
find "$root" -type f -print | while IFS= read -r file; do
  if file -b "$file" | grep -q '^Mach-O'; then
    printf '%s\n' "$file"
  fi
done > "$machos"
if [ ! -s "$machos" ]; then
  echo "error: no Mach-O files under $root" >&2
  exit 1
fi
while IFS= read -r file; do
  codesign --force --options runtime --timestamp --keychain "$keychain" \
    --sign "$identity" "$file"
  codesign --verify --strict "$file"
done < "$machos"
echo "signed $(wc -l < "$machos" | tr -d ' ') Mach-O files"

# Accept the .p8 as PEM text or as base64 of the PEM file.
case "$APPLE_NOTARY_KEY" in
  *"BEGIN PRIVATE KEY"*) printf '%s\n' "$APPLE_NOTARY_KEY" > "$work/AuthKey.p8" ;;
  *) printf '%s' "$APPLE_NOTARY_KEY" | tr -d ' \r\n' | base64 --decode > "$work/AuthKey.p8" ;;
esac
chmod 600 "$work/AuthKey.p8"

bundle="$work/$(basename "$root").zip"
ditto -c -k --sequesterRsrc --keepParent "$root" "$bundle"
set +e
xcrun notarytool submit "$bundle" --wait --output-format json \
  --key "$work/AuthKey.p8" --key-id "$APPLE_NOTARY_KEY_ID" \
  --issuer "$APPLE_NOTARY_ISSUER" > "$work/notary.json"
rc=$?
set -e
cat "$work/notary.json"
echo
sub_id=$(plutil -extract id raw -o - "$work/notary.json" 2>/dev/null || true)
status=$(plutil -extract status raw -o - "$work/notary.json" 2>/dev/null || true)
echo "notarization ${sub_id:-<no id>}: ${status:-unknown}"
if [ "$rc" -ne 0 ] || [ "$status" != "Accepted" ]; then
  echo "error: notarization failed (notarytool exit $rc, status ${status:-unknown})" >&2
  if [ -n "$sub_id" ]; then
    xcrun notarytool log "$sub_id" \
      --key "$work/AuthKey.p8" --key-id "$APPLE_NOTARY_KEY_ID" \
      --issuer "$APPLE_NOTARY_ISSUER" || true
  fi
  exit 1
fi
printf 'notarized\n' > "$root/SIGNING.txt"

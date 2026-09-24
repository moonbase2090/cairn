#!/bin/sh
# Sign and, when Apple notarization credentials are set, notarize a cairn prefix.
# Unsigned builds are recorded in SIGNING.txt. They are not notarized.
set -eu
root=${1:?prefix directory}
identity=${CAIRN_CODESIGN_IDENTITY:-}
apple_id=${APPLE_ID:-}
team_id=${APPLE_TEAM_ID:-}
password=${APPLE_APP_PASSWORD:-}

if [ "$(uname -s)" != Darwin ]; then
  printf 'not a macOS build\n' > "$root/SIGNING.txt"
  exit 0
fi
if [ -z "$identity" ]; then
  printf 'unsigned\n' > "$root/SIGNING.txt"
  exit 0
fi

find "$root" -type f -print | while IFS= read -r file; do
  if file "$file" | grep -q 'Mach-O'; then
    codesign --force --options runtime --timestamp --sign "$identity" "$file"
  fi
done

if [ -z "$apple_id" ] || [ -z "$team_id" ] || [ -z "$password" ]; then
  printf 'signed\n' > "$root/SIGNING.txt"
  exit 0
fi

archive=$(mktemp -d)/cairn.zip
ditto -c -k --keepParent "$root" "$archive"
xcrun notarytool submit "$archive" \
  --apple-id "$apple_id" --team-id "$team_id" --password "$password" --wait
find "$root" -type f -print | while IFS= read -r file; do
  if file "$file" | grep -q 'Mach-O'; then
    xcrun stapler staple "$file" || true
  fi
done
printf 'notarized\n' > "$root/SIGNING.txt"

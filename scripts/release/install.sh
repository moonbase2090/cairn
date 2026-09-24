#!/bin/sh
# Install a verified cairn archive into a prefix. Machine-level only.
# Usage: ./install.sh [--prefix /absolute/path]
set -eu

payload=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
prefix=${HOME}/.local

if [ "${1:-}" = --prefix ] && [ "$#" -eq 2 ]; then
  prefix=$2
elif [ "$#" -ne 0 ]; then
  echo "Usage: ./install.sh [--prefix /absolute/path]" >&2
  exit 2
fi
case "$prefix" in
  /*) ;;
  *) echo "The prefix must be an absolute path." >&2; exit 2 ;;
esac

host=$(uname -s)-$(uname -m)
case "$host" in
  Linux-x86_64) host_target=x86_64-unknown-linux-gnu ;;
  Linux-aarch64) host_target=aarch64-unknown-linux-gnu ;;
  Darwin-arm64) host_target=aarch64-apple-darwin ;;
  Darwin-x86_64) host_target=x86_64-apple-darwin ;;
  *) host_target=unknown ;;
esac
archive_target=$(cat "$payload/TARGET")
if [ "$host_target" != "$archive_target" ]; then
  echo "This archive is for $archive_target, not $host_target." >&2
  exit 1
fi

cd "$payload"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c SHA256SUMS
elif command -v shasum >/dev/null 2>&1; then
  shasum -a 256 -c SHA256SUMS
else
  echo "sha256sum or shasum is required." >&2
  exit 1
fi

version=$(cat VERSION)
dest="$prefix/lib/cairn"
mkdir -p "$dest" "$prefix/bin"
rm -rf "$dest/python" "$dest/app" "$dest/bin"
cp -a python app bin "$dest/"
cp -a VERSION TARGET MPL-2.0.txt INSTALL.txt "$dest/"
for name in cairn cairn-mcp cairn-embedd; do
  ln -sfn "$dest/bin/$name" "$prefix/bin/$name"
done
printf 'Installed cairn %s. Programs are in %s.\n' "$version" "$prefix/bin"
case ":$PATH:" in
  *":$prefix/bin:"*) ;;
  *) printf 'Add %s to PATH.\n' "$prefix/bin" ;;
esac
printf 'Next, per project: cairn init --yes && cairn bootstrap\n'

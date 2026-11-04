#!/bin/sh
# Build a self-contained box package: dist/syncforever-<version>-<commit>.tar.gz, holding the box
# software as wheels (its dependencies too, so the Pi needs no internet to install) and this
# folder's installer, service, example config and README. On the Pi:
#   tar xzf syncforever-*.tar.gz && sudo syncforever/deploy/install.sh
# The apps (NES, Genesis, N64) are inside the syncforever wheel; nothing needs building there.
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT="$REPO/dist"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/host/pyproject.toml")
COMMIT=$(git -C "$REPO" rev-parse --short HEAD)
if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
    COMMIT="$COMMIT-dirty"
fi
NAME="syncforever-$VERSION-$COMMIT"

mkdir -p "$WORK/syncforever/wheels" "$WORK/syncforever/deploy" "$OUT"
(cd "$REPO/host" && uv build --wheel -o "$WORK/syncforever/wheels" >/dev/null)
# Pure-Python dependencies: one wheel each works on any Pi.
uv run --project "$REPO/host" --with pip python -m pip download --quiet --only-binary=:all: \
    --dest "$WORK/syncforever/wheels" "$WORK/syncforever/wheels"/syncforever-*.whl
for f in install.sh syncforever.service config.example.toml README.md; do
    cp "$REPO/deploy/$f" "$WORK/syncforever/deploy/"
done
echo "$NAME" > "$WORK/syncforever/VERSION"
# No macOS extended attributes (com.apple.provenance): GNU tar warns about each one.
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -czf "$OUT/$NAME.tar.gz" -C "$WORK" syncforever
echo "$OUT/$NAME.tar.gz"
ls "$WORK/syncforever/wheels"

#!/bin/sh
# Publish the current commit as the box's update: build the package (package.sh) and put it on
# the GitHub release "latest" as syncforever.tar.gz, where deploy/get.sh fetches it. Needs gh,
# logged in, and a clean tree that's pushed (the package names its commit).
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
    echo "commit first: the package names the commit it was built from" >&2
    exit 1
fi
if [ "$(git -C "$REPO" rev-parse HEAD)" != "$(git -C "$REPO" rev-parse @{u} 2>/dev/null)" ]; then
    echo "push first: the release should match what's on GitHub" >&2
    exit 1
fi
# Never publish a build whose tests fail (a broken n8pro.py once went out: 2026-11-04).
(cd "$REPO/host" && uv run pytest -q >/dev/null) || { echo "tests fail: not released" >&2; exit 1; }
PACKAGE=$("$REPO/deploy/package.sh" | head -1)
NAME=$(basename "$PACKAGE" .tar.gz)
cp "$PACKAGE" "$REPO/dist/syncforever.tar.gz"
cd "$REPO"
gh release delete latest --yes --cleanup-tag >/dev/null 2>&1 || true
gh release create latest "$REPO/dist/syncforever.tar.gz" --target "$(git rev-parse HEAD)" \
    --title "$NAME" --notes "Install or update: curl -fsSL https://raw.githubusercontent.com/cisox/syncforever/main/deploy/get.sh | sudo bash"
echo "released $NAME"

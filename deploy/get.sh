#!/bin/bash
# Install or update Sync Forever on this box, from the newest package on GitHub:
#
#   curl -fsSL https://raw.githubusercontent.com/cisox/syncforever/main/deploy/get.sh | sudo bash
#
# It downloads the release "latest" (deploy/release.sh puts each new package there), unpacks it
# in a temporary folder and runs its deploy/install.sh: the same as installing a package by hand.
# Your config and the service's user are kept. SYNCFOREVER_URL picks another package.
set -euo pipefail

URL="${SYNCFOREVER_URL:-https://github.com/cisox/syncforever/releases/download/latest/syncforever.tar.gz}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run it with sudo: curl -fsSL https://raw.githubusercontent.com/cisox/syncforever/main/deploy/get.sh | sudo bash" >&2
    exit 1
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
echo "Downloading $URL"
curl -fsSL "$URL" -o "$work/syncforever.tar.gz"
tar xzf "$work/syncforever.tar.gz" -C "$work"
echo "Installing $(cat "$work/syncforever/VERSION")"
# The script itself arrives on stdin, so a fresh install's questions (sfe setup) read the terminal.
if [ -r /dev/tty ] && (: </dev/tty) 2>/dev/null; then
    "$work/syncforever/deploy/install.sh" </dev/tty
else
    "$work/syncforever/deploy/install.sh"
fi

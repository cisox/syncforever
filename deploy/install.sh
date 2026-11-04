#!/bin/sh
# Install (or update) Sync Forever on a Raspberry Pi or any Debian-like Linux with systemd.
#   tar xzf syncforever-*.tar.gz && sudo syncforever/deploy/install.sh   (deploy/package.sh)
#   or: git clone <repo> syncforever && sudo syncforever/deploy/install.sh
# Running it again replaces the software (always, even at the same version) and keeps
# /etc/syncforever/config.toml and the user the service runs as (`sfe setup` puts it in a
# systemd drop-in, which is kept; so is a User=/Group= edited into the unit).
# A fresh install run from a terminal ends with `sfe setup`'s questions.
set -eu

PREFIX=/opt/syncforever
CONFIG_DIR=/etc/syncforever
STATE_DIR=/var/lib/syncforever
SERVICE=syncforever
USER_NAME=syncforever
UNIT=/etc/systemd/system/$SERVICE.service

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh: run with sudo" >&2
    exit 1
fi
REPO=$(cd "$(dirname "$0")/.." && pwd)

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    echo "install.sh: needs Python 3.11 or newer (Raspberry Pi OS Bookworm has it)" >&2
    exit 1
fi
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    apt-get install -y python3-venv
fi

if ! id "$USER_NAME" >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin --groups dialout "$USER_NAME"
fi

python3 -m venv "$PREFIX/venv"
if [ -d "$REPO/wheels" ]; then
    # A package from deploy/package.sh: everything is in wheels/, no internet needed.
    # --force-reinstall: every build is version 0.1.0, which --upgrade would call installed.
    "$PREFIX/venv/bin/pip" install --quiet --force-reinstall --no-index --find-links "$REPO/wheels" syncforever
else
    "$PREFIX/venv/bin/pip" install --quiet --upgrade pip
    "$PREFIX/venv/bin/pip" install --quiet --upgrade "$REPO/host"
    "$PREFIX/venv/bin/pip" install --quiet --force-reinstall --no-deps "$REPO/host"
fi
ln -sf "$PREFIX/venv/bin/sfe" /usr/local/bin/sfe
install -m 644 "$REPO/deploy/README.md" "$PREFIX/README.md"

install -d "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    install -m 644 "$REPO/deploy/config.example.toml" "$CONFIG_DIR/config.toml"
    NEW_CONFIG=1
else
    NEW_CONFIG=0
fi

# The service's user and group: kept from the installed unit if it has them.
RUN_USER=$USER_NAME
RUN_GROUP=$USER_NAME
if [ -f "$UNIT" ]; then
    RUN_USER=$(sed -n 's/^User=//p' "$UNIT" | tail -n 1)
    RUN_GROUP=$(sed -n 's/^Group=//p' "$UNIT" | tail -n 1)
    RUN_USER=${RUN_USER:-$USER_NAME}
    RUN_GROUP=${RUN_GROUP:-$USER_NAME}
fi
sed -e "s/^User=.*/User=$RUN_USER/" -e "s/^Group=.*/Group=$RUN_GROUP/" "$REPO/deploy/$SERVICE.service" >"$UNIT.new"
chmod 644 "$UNIT.new"
mv "$UNIT.new" "$UNIT"
systemctl daemon-reload
# The state folder (the sync manifest) belongs to whoever the service runs as, drop-ins included.
EFFECTIVE_USER=$(systemctl show -P User "$SERVICE" 2>/dev/null || true)
EFFECTIVE_GROUP=$(systemctl show -P Group "$SERVICE" 2>/dev/null || true)
install -d -o "${EFFECTIVE_USER:-$RUN_USER}" -g "${EFFECTIVE_GROUP:-$RUN_GROUP}" "$STATE_DIR"
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

BUILD=$(cat "$REPO/VERSION" 2>/dev/null || git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo "?")
echo "Sync Forever $BUILD installed; the service runs as ${EFFECTIVE_USER:-$RUN_USER}."
if [ "$NEW_CONFIG" -eq 1 ] && [ -t 0 ]; then
    # A fresh install with someone at the keyboard: ask for the folders and the user now.
    echo
    /usr/local/bin/sfe setup --config "$CONFIG_DIR/config.toml" || true
elif [ "$NEW_CONFIG" -eq 1 ]; then
    echo "Next: sudo sfe setup (the ROM and save folders, and the user the box runs as)"
else
    echo "Check it: sudo sfe doctor"
fi
echo "Log: journalctl -u $SERVICE -f"

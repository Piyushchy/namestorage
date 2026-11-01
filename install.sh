#!/usr/bin/env bash
# namestorage installer. Run it from a clone of the repository:
#
#     git clone https://github.com/Piyushchy/namestorage.git
#     cd namestorage
#     ./install.sh
#
# It installs what is needed, copies the program to /opt/namestorage, adds the
# `namestorage` command, and then starts the setup wizard. Safe to run again.
#
# Optional environment variables (mostly for testing):
#   NS_INSTALL_DIR  where the program is installed   (default /opt/namestorage)
#   NS_BIN_DIR      where the command is installed   (default /usr/local/bin)
#   NS_SKIP_APT=1   do not install system packages
#   NS_SKIP_SETUP=1 do not start the wizard at the end
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${NS_INSTALL_DIR:-/opt/namestorage}"
BIN_DIR="${NS_BIN_DIR:-/usr/local/bin}"
SERVICE_USER="nsagent"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ -f "$SRC/namestorage/cli.py" ] || die "run this from the root of the namestorage repository"
[ "$(uname -s)" = "Linux" ] || die "this installer is for Linux (Ubuntu)"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  command -v sudo >/dev/null 2>&1 || die "sudo is needed to install system-wide"
  SUDO="sudo"
fi

if [ "${NS_SKIP_APT:-0}" != "1" ]; then
  say "Installing system packages (python3, python3-venv, git, openssl)"
  command -v apt-get >/dev/null 2>&1 || die "apt-get not found. Install python3, python3-venv, git and openssl yourself, then run: NS_SKIP_APT=1 ./install.sh"
  $SUDO apt-get update -y -qq
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv git openssl
fi

say "Creating the $SERVICE_USER user (it runs the programs with no login and no shell)"
if id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "already exists"
else
  $SUDO useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

say "Copying the program to $INSTALL_DIR"
$SUDO mkdir -p "$INSTALL_DIR"
if [ "$(cd "$INSTALL_DIR" && pwd -P)" != "$(cd "$SRC" && pwd -P)" ]; then
  # Your local venv and settings are not copied. .git is, so `namestorage update` can pull.
  tar -C "$SRC" \
      --exclude=./venv --exclude=./.venv --exclude=./signaling.env \
      --exclude=__pycache__ --exclude=.pytest_cache -cf - . \
    | $SUDO tar -C "$INSTALL_DIR" --no-same-owner -xf -
else
  echo "already running from $INSTALL_DIR"
fi

say "Setting up Python (this downloads a few packages)"
$SUDO python3 -m venv "$INSTALL_DIR/venv"
$SUDO "$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements-agent.txt" -r "$INSTALL_DIR/requirements-signaling.txt"

say "Installing the namestorage command into $BIN_DIR"
$SUDO mkdir -p "$BIN_DIR"
$SUDO tee "$BIN_DIR/namestorage" >/dev/null <<EOF
#!/bin/sh
exec env PYTHONPATH="$INSTALL_DIR" NAMESTORAGE_INSTALL_DIR="$INSTALL_DIR" "$INSTALL_DIR/venv/bin/python" -m namestorage.cli "\$@"
EOF
$SUDO chmod 755 "$BIN_DIR/namestorage"

if [ "${NS_SKIP_SETUP:-0}" = "1" ]; then
  say "Installed. Next, run: namestorage setup"
  exit 0
fi

say "Starting the setup wizard"
echo "If you started the signaling server or the Tailscale Funnel by hand in a terminal,"
echo "stop them first (Ctrl+C there): the services started here need the same port."
exec "$BIN_DIR/namestorage" setup

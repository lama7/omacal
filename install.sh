#!/usr/bin/env bash
# omacal — backend installer for the omacal Omarchy plugin.
#
# Run this from the plugin folder (the cloned repo), i.e.:
#   omarchy plugin add <repo-url> --enable
#   cd ~/.config/omarchy/plugins/omacal && ./install.sh
#
# It installs the Python backend for the plugin and leaves the QML frontend in
# place (that is this repo — the shell loads it from here). Run it again to
# reinstall/repair; pass --uninstall to remove the backend service.
set -euo pipefail

# This script lives at the repo root, which IS the plugin folder when cloned by
# `omarchy plugin add`. ${BASH_SOURCE[0]} (not $0) survives a PATH symlink.
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$PLUGIN_DIR/backend"

# Backend install locations — outside the plugin folder, because the plugin
# folder is owned by `omarchy plugin update` and any venv inside it would be
# wiped or left stale on the next update.
VENV_DIR="$HOME/.local/omacal/venv"
DEST="$HOME/.local/bin"
SYSTEMD_USER="$HOME/.config/systemd/user"
SERVICE="omacal.service"

UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    --uninstall|-u) UNINSTALL=1 ;;
    -h|--help)
      echo "Usage: install.sh [--uninstall]"
      echo "Installs (default) or removes the omacal Python backend."
      exit 0
      ;;
    *) echo "install.sh: unknown option: $arg" >&2; exit 1 ;;
  esac
done

uninstall_backend() {
  echo "==> Stopping and disabling omacal.service..."
  systemctl --user stop omacal.service 2>/dev/null || true
  systemctl --user disable omacal.service 2>/dev/null || true

  echo "==> Removing symlinks..."
  for cmd in omacal omacal-api; do
    rm -f "$DEST/$cmd"
  done

  echo "==> Removing systemd unit $SYSTEMD_USER/$SERVICE..."
  rm -f "$SYSTEMD_USER/$SERVICE"

  echo "==> Removing venv $VENV_DIR..."
  rm -rf "$VENV_DIR"

  systemctl --user daemon-reload
  echo "==> Backend uninstalled."
  echo "     Remove the plugin itself with: omarchy plugin remove omacal"
  exit 0
}

if (( UNINSTALL )); then
  uninstall_backend
fi

echo "==> Ensuring backend source is present..."
[[ -f "$BACKEND_DIR/pyproject.toml" ]] || {
  echo "install.sh: backend/pyproject.toml not found — run this from the omacal plugin folder" >&2
  exit 1
}

echo "==> Creating venv at $VENV_DIR..."
python3 -m venv "$VENV_DIR"

echo "==> Installing backend and dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "$BACKEND_DIR"

echo "==> Linking CLI tools into $DEST..."
mkdir -p "$DEST"
for cmd in omacal omacal-api; do
  ln -sf "$VENV_DIR/bin/$cmd" "$DEST/$cmd"
  echo "  linked $DEST/$cmd"
done

echo "==> Installing systemd user service..."
mkdir -p "$SYSTEMD_USER"
cp "$PLUGIN_DIR/systemd/omacal.service" "$SYSTEMD_USER/$SERVICE"
systemctl --user daemon-reload
systemctl --user enable omacal.service

echo "==> Done."
echo "     QML frontend is already in place at $PLUGIN_DIR (the shell loads it)."
echo "     Start the daemon with: systemctl --user start omacal"
echo "     First run shows a 'Setup needed' popup — enter your CalDAV server once."
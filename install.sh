#!/usr/bin/env bash
# MIMIK installer — double-click "Install MIMIK.desktop", or run:
#     bash install.sh
# Creates a venv under ~/.local/share/mimik, installs MIMIK + requests +
# PySide6 + mitmproxy, and puts `mimik` / `mimik-gui` on your PATH.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$HOME/.local/share/mimik"
VENV="$PREFIX/venv"
BIN="$HOME/.local/bin"

echo "Installing MIMIK…"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is not installed. Install it first, then re-run this."
  exit 1
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "python3-venv is missing. Run this once, then re-run the installer:"
  echo "  sudo apt install python3.12-venv python3-pip"
  exit 1
fi

mkdir -p "$PREFIX" "$BIN"

if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating virtualenv at $VENV …"
  python3 -m venv "$VENV"
fi

echo "Installing packages (requests, PySide6, mitmproxy)…"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "$DIR"

ln -sf "$VENV/bin/mimik" "$BIN/mimik"
ln -sf "$VENV/bin/mimik-gui" "$BIN/mimik-gui"

# Verify the install actually worked before claiming success — v0.1.x
# installed without PySide6, so the menu icon silently did nothing
# (mimik-gui crashed on import, with no terminal to show the error).
# Two checks: the packages must be present (fatal if not — that is the
# installer's job), and the GUI must import (warning if not — that
# depends on the machine's system Qt libraries, which the installer
# cannot fix, so the user gets the reason instead of silence).
echo "Verifying the install…"
if ! "$VENV/bin/python" -c "import importlib.metadata as m; [m.version(p) for p in ('mimik', 'PySide6', 'mitmproxy')]; import mitmproxy" 2>/dev/null; then
  echo
  echo "Install FAILED: MIMIK, PySide6 or mitmproxy did not install."
  echo "Check your network connection and re-run this installer."
  exit 1
fi
if [ ! -x "$BIN/mimik-gui" ]; then
  echo
  echo "Install FAILED: mimik-gui is missing from $BIN."
  exit 1
fi
if ! GUI_ERR="$("$VENV/bin/python" -c "import PySide6.QtWidgets, mimik.gui" 2>&1)"; then
  echo
  echo "WARNING: the GUI could not start on this machine, so the menu"
  echo "icon may not open yet. This usually means system Qt libraries"
  echo "are missing on this machine. The error was:"
  echo "  $(echo "$GUI_ERR" | tail -1)"
  echo "Install the missing system library, then re-run this installer."
  echo
fi

# Icons
for spec in "48 mimik-48.png" "128 mimik-128.png" "256 mimik-256.png"; do
  set -- $spec
  dest="$HOME/.local/share/icons/hicolor/${1}x${1}/apps"
  mkdir -p "$dest"
  cp "$DIR/icons/$2" "$dest/mimik.png"
done
mkdir -p "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "$DIR/icons/mimik.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/mimik.svg"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi

# Menu launcher
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/mimik.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MIMIK
Comment=Turn any app into a Python library
Exec=$BIN/mimik-gui
Icon=mimik
Terminal=false
Categories=Development;
StartupNotify=true
EOF
chmod 644 "$HOME/.local/share/applications/mimik.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
fi

echo
echo "MIMIK is installed."
echo "  GUI:  open MIMIK from the menu, or run:  mimik-gui"
echo "  CLI:  mimik doctor   — then   mimik record"
echo "  Captures live in: ~/.local/share/mimik/captures/"
echo
if [ -t 0 ]; then
  read -rp "Press Enter to close." _dummy
fi

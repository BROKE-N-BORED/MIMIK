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

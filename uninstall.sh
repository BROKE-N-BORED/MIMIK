#!/usr/bin/env bash
# MIMIK uninstaller — run:  bash uninstall.sh
# Removes the venv, PATH links, menu launcher and icons.
# Your captures and generated clients are KEPT (in ~/.local/share/mimik).
# Pass --purge to delete those too.
set -euo pipefail

PREFIX="$HOME/.local/share/mimik"

rm -rf "$PREFIX/venv"
rm -f "$HOME/.local/bin/mimik" "$HOME/.local/bin/mimik-gui"
rm -f "$HOME/.local/share/applications/mimik.desktop"
rm -f "$HOME/.local/share/icons/hicolor/48x48/apps/mimik.png"
rm -f "$HOME/.local/share/icons/hicolor/128x128/apps/mimik.png"
rm -f "$HOME/.local/share/icons/hicolor/256x256/apps/mimik.png"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/mimik.svg"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$PREFIX"
  echo "MIMIK removed completely, including captures and clients."
else
  echo "MIMIK removed. Captures + clients kept in $PREFIX"
  echo "(run  bash uninstall.sh --purge  to delete those too)"
fi

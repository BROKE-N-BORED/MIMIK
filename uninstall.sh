#!/usr/bin/env bash
# MIMIK uninstaller — double-click "Uninstall MIMIK.desktop", use the
# "Uninstall MIMIK" menu entry, or run:  bash uninstall.sh
# Removes the venv, PATH links, menu launchers and icons.
# Your captures and generated clients are KEPT (in ~/.local/share/mimik)
# unless you say otherwise: pass --purge, or answer y at the prompt
# when run in a terminal.
set -euo pipefail

PREFIX="$HOME/.local/share/mimik"

PURGE=0
if [ "${1:-}" = "--purge" ]; then
  PURGE=1
elif [ -t 0 ]; then
  echo "This removes MIMIK (the program, its menu entries and icons)."
  printf "Also delete your captures and generated clients in %s? [y/N] " "$PREFIX"
  read -r ANSWER || ANSWER=""
  case "$ANSWER" in
    y|Y|yes|YES) PURGE=1 ;;
  esac
fi

rm -rf "$PREFIX/venv"
rm -f "$HOME/.local/bin/mimik" "$HOME/.local/bin/mimik-gui"
rm -f "$HOME/.local/share/applications/mimik.desktop"
rm -f "$HOME/.local/share/applications/mimik-uninstall.desktop"
rm -f "$HOME/.local/share/icons/hicolor/48x48/apps/mimik.png"
rm -f "$HOME/.local/share/icons/hicolor/128x128/apps/mimik.png"
rm -f "$HOME/.local/share/icons/hicolor/256x256/apps/mimik.png"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/mimik.svg"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi

if [ "$PURGE" = "1" ]; then
  rm -rf "$PREFIX"
  echo "MIMIK removed completely, including captures and clients."
else
  rm -f "$PREFIX/uninstall.sh"
  echo "MIMIK removed. Captures + clients kept in $PREFIX"
  echo "(to delete those too, delete that folder, or re-run this"
  echo " uninstaller from the downloaded folder with: --purge)"
fi

if [ -t 0 ]; then
  printf "Press Enter to close this window. "
  read -r _ || true
fi

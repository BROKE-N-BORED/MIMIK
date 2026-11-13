#!/usr/bin/env python3
"""Render MIMIK's icon PNGs (48/128/256) from icons/mimik.svg.

The repo holds only the SVG — the GitHub push path can't carry binary
files, so the PNGs can't be committed. install.sh runs this when the
PNGs aren't in the tree (fresh clone); the Release zip already carries
prebuilt PNGs, so it skips this step there.

Renderers, in order: Qt QSvgRenderer (PySide6 is a MIMIK dependency, so
no new dependency), then rsvg-convert when present. Renders offscreen —
no display needed. Exits 0 when every requested PNG was written, 1
otherwise; the installer treats failure as warn-and-continue, never
fatal.

Usage: render_icons.py [icons-dir] [size ...]
  icons-dir defaults to the tree's icons/; sizes default to 48 128 256.
"""
import os
import shutil
import subprocess
import sys

SIZES = (48, 128, 256)


def render_qt(svg_path, out_path, size):
    """Render one PNG via Qt. Raises on any failure."""
    # Offscreen before any Qt import: this only paints to an image,
    # it never shows a window, so it must work with no display.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication

    global _APP
    try:
        _APP
    except NameError:
        _APP = QApplication([])
    renderer = QSvgRenderer(svg_path)
    if not renderer.isValid():
        raise RuntimeError(f"QSvgRenderer could not read {svg_path}")
    img = QImage(QSize(size, size), QImage.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    renderer.render(painter)
    painter.end()
    if not img.save(out_path):
        raise RuntimeError(f"QImage could not write {out_path}")


def render_rsvg(svg_path, out_path, size):
    """Render one PNG via rsvg-convert. Raises on any failure."""
    exe = shutil.which("rsvg-convert")
    if not exe:
        raise RuntimeError("rsvg-convert not found")
    subprocess.run([exe, "-w", str(size), "-h", str(size),
                    svg_path, "-o", out_path],
                   check=True, capture_output=True)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"rsvg-convert wrote no output for {out_path}")


def render_all(icons_dir, sizes):
    """Render every size. Returns (rendered, errors)."""
    svg_path = os.path.join(icons_dir, "mimik.svg")
    if not os.path.exists(svg_path):
        return [], [f"SVG not found: {svg_path}"]
    rendered, errors = [], []
    for size in sizes:
        out_path = os.path.join(icons_dir, f"mimik-{size}.png")
        last_err = None
        for renderer in (render_qt, render_rsvg):
            try:
                renderer(svg_path, out_path, size)
                break
            except Exception as exc:  # noqa: BLE001 — fall through to next
                last_err = exc
        else:
            errors.append(f"{size}px: {last_err}")
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            rendered.append(out_path)
        else:
            errors.append(f"{size}px: no output written")
    return rendered, errors


def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icons_dir = argv[1] if len(argv) > 1 else os.path.join(root, "icons")
    try:
        sizes = tuple(int(x) for x in argv[2:]) or SIZES
    except ValueError:
        print("render_icons.py: sizes must be integers", file=sys.stderr)
        return 1
    rendered, errors = render_all(icons_dir, sizes)
    for path in rendered:
        print(f"rendered {path} ({os.path.getsize(path)} bytes)")
    for err in errors:
        print(f"render_icons.py: {err}", file=sys.stderr)
    if errors:
        print("render_icons.py: no renderer available (need PySide6 "
              "with QtSvg, or rsvg-convert)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

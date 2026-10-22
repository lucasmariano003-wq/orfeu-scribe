#!/usr/bin/python3
"""Renders the real window with synthetic data into PNG files, light and dark, in English and Portuguese.

Run it only through scripts/prove_ui.sh, which starts a private Xvfb display
and a private D-Bus session. The script refuses to run on a real desktop
session (Wayland present, or the wrapper's marker missing).

Usage: scripts/prove_ui.sh [OUTPUT_DIR]   (default: docs/screenshots)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from ui_fixture import build, isolated_environment  # noqa: E402

PAGES = ("dictation", "history", "vocabulary", "settings")
LANGUAGES = ("en", "pt_BR")


def child(language, output):
    import gi
    gi.require_version("Adw", "1")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Adw, GLib, Graphene, Gtk
    app, window, ids = build(language)
    if window is None:
        print("FAILED: no display")
        return 1
    window.set_default_size(1100, 840)
    window.present()
    frames = [(page, theme, None) for theme in ("light", "dark") for page in PAGES]
    frames += [("dictation", "light", "listening"), ("history", "dark", "expanded")]
    saved = []

    def snapshot(name):
        width, height = window.get_width(), window.get_height()
        paintable = Gtk.WidgetPaintable.new(window)
        snap = Gtk.Snapshot.new()
        paintable.snapshot(snap, width, height)
        node = snap.to_node()
        if node is None:
            raise RuntimeError("the window produced no content")
        texture = window.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, width, height))
        path = output / f"{name}.png"
        texture.save_to_png(str(path))
        saved.append(path)
        print(f"saved {path.relative_to(ROOT) if ROOT in path.parents else path} {width}x{height}", flush=True)

    def next_frame():
        if not frames:
            window.destroy()
            app.quit()
            loop.quit()
            return GLib.SOURCE_REMOVE
        page, theme, detail = frames.pop(0)
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if theme == "dark" else Adw.ColorScheme.FORCE_LIGHT)
        app.state = "listening" if detail == "listening" else "idle"
        app.live_text = ("…and the meeting moves to Wednesday at three" if language == "en"
                         else "…e a reunião fica para quarta às três") if detail == "listening" else ""
        window.edit_drafts.clear()
        window.stack.set_visible_child_name(page)
        window.refresh()
        if detail == "listening":
            window.set_level(.6)
        if detail == "expanded":
            window.refresh_history()
            first = window.history_box.get_first_child()
            row = first.get_row(0) if first else None
            if row:
                row.set_expanded(True)
        name = f"{language}-{page}-{theme}" + (f"-{detail}" if detail else "")
        GLib.timeout_add(600, lambda: (snapshot(name), GLib.idle_add(next_frame), GLib.SOURCE_REMOVE)[-1])
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(900, next_frame)
    loop = GLib.MainLoop()
    app.connect("shutdown", lambda *_args: loop.quit())
    window.connect("destroy", lambda *_args: loop.quit())
    loop.run()
    return 0 if len(saved) == len(PAGES) * 2 + 2 else 1


def main(argv):
    if "--child" in argv:
        index = argv.index("--child")
        return child(argv[index + 1], Path(argv[index + 2]))
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("ORFEUSCRIBE_UI_PROOF") != "1" or not os.environ.get("DISPLAY"):
        print("Refusing to run outside scripts/prove_ui.sh (isolated Xvfb and D-Bus).", file=sys.stderr)
        return 2
    output = Path(argv[0] if argv else ROOT / "docs/screenshots").resolve()
    output.mkdir(parents=True, exist_ok=True)
    status = 0
    for language in LANGUAGES:
        with tempfile.TemporaryDirectory(prefix="orfeuscribe-ui-") as name:
            env = isolated_environment(Path(name), language, {"DISPLAY": os.environ["DISPLAY"], "GDK_BACKEND": "x11"})
            # Keep the private bus started by dbus-run-session.
            env["DBUS_SESSION_BUS_ADDRESS"] = os.environ.get("DBUS_SESSION_BUS_ADDRESS", env["DBUS_SESSION_BUS_ADDRESS"])
            status |= subprocess.run([sys.executable, __file__, "--child", language, str(output)], cwd=ROOT,
                                     env=env).returncode
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

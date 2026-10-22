#!/usr/bin/python3
"""Per-user installer and uninstaller for Orfeu Scribe.

    install.py install   [--home DIR] [--dry-run] [--enable-extension]
    install.py uninstall [--home DIR] [--dry-run] [--purge] [--disable-extension]

Installs only under HOME (default: the real one; --home points everything at
another directory, which is how the tests run it). It never starts the service
and never enables the extension unless --enable-extension is given. Every
installed path is recorded in a manifest, and uninstall removes only those
paths plus directories carrying Orfeu Scribe's marker. Settings, history,
vocabulary and keyring entries are kept unless --purge is given (keyring
entries are never removed automatically; the command is printed instead).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from orfeuscribe.ids import APP_ID, EXTENSION_UUID, SCHEMA_ID, SECRET_SCHEMA, SLUG  # noqa: E402

MARKER = ".orfeuscribe-install.json"
EXTENSION_FILES = ("extension.js", "prefs.js", "metadata.json", "stylesheet.css", "orfeuscribe-symbolic.svg")
GST_ELEMENTS = ("autoaudiosrc", "audioconvert", "audioresample", "appsink", "level", "uridecodebin", "appsrc",
                "autoaudiosink")


def paths(home: Path, use_xdg=False) -> dict:
    def location(key, fallback):
        value = os.environ.get(key, "") if use_xdg else ""
        return Path(value) if value and Path(value).is_absolute() else fallback
    data = location("XDG_DATA_HOME", home / ".local/share")
    config = location("XDG_CONFIG_HOME", home / ".config")
    return {"app": home / ".local/lib" / SLUG, "launcher": home / ".local/bin" / SLUG,
            "desktop": data / "applications" / f"{APP_ID}.desktop",
            "icon": data / "icons/hicolor/scalable/apps" / f"{APP_ID}.svg",
            "symbolic": data / "icons/hicolor/symbolic/apps" / f"{APP_ID}-symbolic.svg",
            "extension": data / "gnome-shell/extensions" / EXTENSION_UUID,
            "autostart": config / "autostart" / f"{APP_ID}.desktop",
            "config": config / SLUG, "data": data / SLUG}


def check_dependencies():
    problems = []
    try:
        import gi
        for namespace, version in (("Gtk", "4.0"), ("Adw", "1"), ("Gst", "1.0")):
            gi.require_version(namespace, version)
        from gi.repository import Adw, Gst  # noqa: F401
        Gst.init(None)
        missing = [name for name in GST_ELEMENTS if not Gst.ElementFactory.find(name)]
        if missing:
            problems.append("missing GStreamer elements: " + ", ".join(missing))
        if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 6):
            problems.append("libadwaita 1.6 or newer is required")
    except (ImportError, ValueError) as exc:
        problems.append(f"PyGObject with GTK 4, libadwaita and GStreamer is required ({exc})")
    try:
        import gi
        gi.require_version("Secret", "1")
    except (ImportError, ValueError):
        print("note: libsecret is not available; API keys must come from environment variables")
    if not shutil.which("glib-compile-schemas"):
        problems.append("glib-compile-schemas is required")
    return problems


def owned(directory: Path) -> bool:
    marker = directory / MARKER
    if directory.is_symlink() or marker.is_symlink():
        return False
    try:
        return json.loads(marker.read_text()).get("installed_by") == "orfeuscribe scripts/install.py"
    except (OSError, ValueError, AttributeError):
        return False


def manifest_for(directory: Path) -> dict:
    if not owned(directory):
        return {}
    try:
        value = json.loads((directory / "manifest.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owns_file(path: Path, manifest: dict) -> bool:
    try:
        return (not path.is_symlink() and path.is_file()
                and manifest.get("file_hashes", {}).get(str(path)) == file_digest(path))
    except OSError:
        return False


def copy_tree(source: Path, target: Path, ignore=None):
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "gschemas.compiled",
                                                                  "locale", *(ignore or ())))


def stage_app(staging: Path):
    copy_tree(ROOT / "orfeuscribe", staging / "orfeuscribe")
    copy_tree(ROOT / "data", staging / "data")
    for name in ("LICENSE", "NOTICE", "ATTRIBUTION.md"):
        shutil.copy2(ROOT / name, staging / name)
    (staging / "extension").mkdir()
    copy_tree(ROOT / "extension" / EXTENSION_UUID, staging / "extension" / EXTENSION_UUID)
    sys.path.insert(0, str(ROOT / "scripts"))
    from compile_translations import parse_po, write_mo
    for po in sorted((ROOT / "po").glob("*.po")):
        messages = parse_po(po)
        write_mo(messages, staging / "locale" / po.stem / "LC_MESSAGES" / "orfeuscribe.mo")
        write_mo(messages, staging / "extension" / EXTENSION_UUID / "locale" / po.stem / "LC_MESSAGES" / "orfeuscribe.mo")
    subprocess.run(["glib-compile-schemas", "--strict", str(staging / "extension" / EXTENSION_UUID / "schemas")], check=True)


def launcher_script(app_dir: Path) -> str:
    # The path is written as a Python literal: no shell interpolation in the launcher.
    return ("#!/usr/bin/python3\nimport sys\nsys.path.insert(0, " + repr(str(app_dir)) + ")\n"
            "from orfeuscribe.__main__ import main\nraise SystemExit(main())\n")


def write(path: Path, text: str, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def install(home: Path, dry_run=False, enable_extension=False, use_xdg=False):
    target = paths(home, use_xdg)
    previous = manifest_for(target["app"])
    plan = [("copy app", target["app"]), ("launcher", target["launcher"]), ("desktop entry", target["desktop"]),
            ("icon", target["icon"]), ("symbolic icon", target["symbolic"]), ("extension", target["extension"])]
    for name in ("app", "extension"):
        if (target[name].exists() or target[name].is_symlink()) and not owned(target[name]):
            raise SystemExit(f"Refusing to replace {target[name]}: it was not installed by this installer.")
    for name in ("launcher", "desktop", "icon", "symbolic"):
        path = target[name]
        if (path.exists() or path.is_symlink()) and not owns_file(path, previous):
            raise SystemExit(f"Refusing to replace {path}: it is unowned or has been modified.")
    if dry_run:
        print("Dry run; nothing will be written. Planned:")
        for label, path in plan:
            print(f"  {label}: {path}")
        print("  extension: not enabled (pass --enable-extension); service: not started")
        return []
    problems = check_dependencies()
    if problems:
        raise SystemExit("Cannot install:\n  " + "\n  ".join(problems))
    with tempfile.TemporaryDirectory(prefix="orfeuscribe-stage-") as stage_name:
        staging = Path(stage_name) / "app"
        stage_app(staging)
        extension_stage = Path(stage_name) / "extension"
        extension_stage.mkdir()
        for name in EXTENSION_FILES:
            shutil.copy2(staging / "extension" / EXTENSION_UUID / name, extension_stage / name)
        for name in ("LICENSE", "NOTICE", "ATTRIBUTION.md"):
            shutil.copy2(staging / name, extension_stage / name)
        shutil.copytree(staging / "extension" / EXTENSION_UUID / "schemas", extension_stage / "schemas")
        if (staging / "extension" / EXTENSION_UUID / "locale").exists():
            shutil.copytree(staging / "extension" / EXTENSION_UUID / "locale", extension_stage / "locale")
        for name, source in (("app", staging), ("extension", extension_stage)):
            if target[name].exists():
                shutil.rmtree(target[name])
            target[name].parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target[name])
            write(target[name] / MARKER, json.dumps({"installed_by": "orfeuscribe scripts/install.py"}) + "\n")
    write(target["launcher"], launcher_script(target["app"]), 0o755)
    from orfeuscribe.desktop import desktop_entry
    write(target["desktop"], desktop_entry(launcher=target["launcher"]))
    icons = ROOT / "data/icons/hicolor"
    target["icon"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(icons / "scalable/apps" / f"{APP_ID}.svg", target["icon"])
    target["symbolic"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(icons / "symbolic/apps" / f"{APP_ID}-symbolic.svg", target["symbolic"])
    files = [str(target[key]) for key in ("launcher", "desktop", "icon", "symbolic")]
    write(target["app"] / "manifest.json", json.dumps({"files": files,
        "file_hashes": {name: file_digest(Path(name)) for name in files}, "directories": [
        str(target["app"]), str(target["extension"])]}, indent=2) + "\n")
    print(f"Orfeu Scribe installed under {home}.")
    if enable_extension:
        result = subprocess.run(["gnome-extensions", "enable", EXTENSION_UUID], capture_output=True, text=True)
        print("Extension enabled." if result.returncode == 0 else
              "GNOME has not discovered the extension yet: log out and back in, then run "
              f"gnome-extensions enable {EXTENSION_UUID}")
    else:
        print(f"Next: log out and back in, then enable the extension with: gnome-extensions enable {EXTENSION_UUID}")
    print(f"Open Orfeu Scribe from the app grid or run {target['launcher']}, then set up a service in Settings.")
    return files


def uninstall(home: Path, dry_run=False, purge=False, disable_extension=False, use_xdg=False):
    target = paths(home, use_xdg)
    manifest = manifest_for(target["app"])
    files = [str(target[key]) for key in ("launcher", "desktop", "icon", "symbolic")
             if owns_file(target[key], manifest)]
    retained = [str(target[key]) for key in ("launcher", "desktop", "icon", "symbolic")
                if target[key].exists() and str(target[key]) not in files]
    autostart = target["autostart"]
    from orfeuscribe.desktop import desktop_entry
    if (not autostart.is_symlink() and autostart.is_file()
            and autostart.read_text(errors="replace") == desktop_entry(True, target["launcher"])):
        files.append(str(autostart))
    directories = [target[name] for name in ("extension", "app") if owned(target[name])]
    if purge:
        directories += [target["config"], target["data"]]
    if dry_run:
        print("Dry run; nothing will be removed. Planned:")
        for path in files + [str(d) for d in directories]:
            print(f"  remove {path}")
        return
    if disable_extension:
        subprocess.run(["gnome-extensions", "disable", EXTENSION_UUID], capture_output=True)
    for name in files:
        path = Path(name)
        path.unlink(missing_ok=True)
    for directory in directories:
        if directory.is_symlink():
            print(f"Kept symlink and its target: {directory}")
        elif directory.exists():
            shutil.rmtree(directory)
    print("Orfeu Scribe removed." + ("" if purge else " Settings, history and vocabulary were kept "
                                   f"({target['config']}, {target['data']})."))
    print("API keys stay in the keyring. To remove them: "
          f"secret-tool clear xdg:schema {SECRET_SCHEMA}")
    print(f"GSettings values ({SCHEMA_ID}) stay until you reset them with dconf or gsettings.")
    for path in retained:
        print(f"Kept unowned or modified file: {path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install or remove Orfeu Scribe for one user.")
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--home", type=Path, help="install under this directory instead of the real HOME")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    parser.add_argument("--enable-extension", action="store_true", help="run gnome-extensions enable after install")
    parser.add_argument("--disable-extension", action="store_true", help="run gnome-extensions disable on uninstall")
    parser.add_argument("--purge", action="store_true", help="also delete settings, history and vocabulary")
    args = parser.parse_args(argv)
    home = (args.home or Path.home()).expanduser().resolve()
    if args.action == "install":
        install(home, args.dry_run, args.enable_extension, use_xdg=args.home is None)
    else:
        uninstall(home, args.dry_run, args.purge, args.disable_extension, use_xdg=args.home is None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

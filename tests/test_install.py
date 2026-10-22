"""Installer and uninstaller in a temporary HOME, next to a fake Calamus install that must stay intact."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orfeuscribe.ids import APP_ID, EXTENSION_UUID
from orfeuscribe import __version__

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts/install.py"
# Paths of the app Orfeu Scribe was derived from; their content here is synthetic.
CALAMUS = (".local/share/gnome-shell/extensions/calamus@padrelucas/extension.js", ".config/calamus/preferencias.json",
           ".local/share/calamus/historico.db", ".local/bin/calamus",
           ".local/share/applications/org.calamus.Calamus.desktop")


def snapshot(root: Path):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
            for p in sorted(root.rglob("*"))}


class InstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = self.base / "home"
        for name in CALAMUS:
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic placeholder for " + name)
        self.calamus = {name: (self.home / name).read_bytes() for name in CALAMUS}

    def run_installer(self, *args, check=True):
        env = dict(os.environ, HOME=str(self.home))
        result = subprocess.run([sys.executable, str(INSTALLER), *args, "--home", str(self.home)], env=env,
                                capture_output=True, text=True, timeout=120)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def assert_calamus_intact(self):
        for name, content in self.calamus.items():
            self.assertEqual((self.home / name).read_bytes(), content, name)

    def test_dry_run_writes_nothing(self):
        before = snapshot(self.base)
        output = self.run_installer("install", "--dry-run").stdout
        self.assertIn("Dry run", output)
        self.assertIn("not enabled", output)
        self.assertEqual(snapshot(self.base), before)

    def test_install_layout_permissions_and_coexistence(self):
        before = set(snapshot(self.base))
        output = self.run_installer("install").stdout
        self.assertIn("gnome-extensions enable " + EXTENSION_UUID, output)
        created = set(snapshot(self.base)) - before
        self.assertTrue(all(name.startswith("home/") for name in created), "nothing outside the given HOME")
        app = self.home / ".local/lib/orfeuscribe"
        extension = self.home / ".local/share/gnome-shell/extensions" / EXTENSION_UUID
        launcher = self.home / ".local/bin/orfeuscribe"
        for path in (app / "orfeuscribe/service.py", app / "data/style.css", app / "locale/pt_BR/LC_MESSAGES/orfeuscribe.mo",
                     extension / "extension.js", extension / "metadata.json", extension / "schemas/gschemas.compiled",
                     extension / "locale/pt_BR/LC_MESSAGES/orfeuscribe.mo", launcher,
                     self.home / f".local/share/applications/{APP_ID}.desktop",
                     self.home / f".local/share/icons/hicolor/scalable/apps/{APP_ID}.svg"):
            self.assertTrue(path.is_file(), path)
        self.assertFalse(list(app.rglob("__pycache__")))
        self.assertFalse((app / "tests").exists() or (app / ".work").exists() or (app / ".git").exists())
        for folder in (app, extension):
            for name in ("LICENSE", "NOTICE", "ATTRIBUTION.md"):
                self.assertEqual((folder / name).read_bytes(), (ROOT / name).read_bytes())
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
        self.assertIn(repr(str(app)), launcher.read_text())
        desktop = (self.home / f".local/share/applications/{APP_ID}.desktop").read_text()
        self.assertIn(f'Exec="{launcher}"', desktop)
        self.assertNotIn("Autostart", desktop)
        self.assertFalse((self.home / ".config/autostart").exists(), "autostart is not turned on by the installer")
        metadata = json.loads((extension / "metadata.json").read_text())
        self.assertEqual(metadata["uuid"], EXTENSION_UUID)
        version = subprocess.run([sys.executable, str(launcher), "--version"], capture_output=True, text=True,
                                 env=dict(os.environ, HOME=str(self.home)), cwd=self.base)
        self.assertEqual(version.stdout.strip(), f"Orfeu Scribe {__version__}", version.stderr)
        self.assert_calamus_intact()

    def test_reinstall_uninstall_and_purge(self):
        self.run_installer("install")
        self.run_installer("install")
        config = self.home / ".config/orfeuscribe/settings.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}")
        history = self.home / ".local/share/orfeuscribe/history.db"
        history.parent.mkdir(parents=True)
        history.write_text("synthetic")
        plan = self.run_installer("uninstall", "--dry-run").stdout
        self.assertIn(".local/bin/orfeuscribe", plan)
        self.assertTrue((self.home / ".local/bin/orfeuscribe").exists())
        self.run_installer("uninstall")
        for gone in (".local/bin/orfeuscribe", ".local/lib/orfeuscribe", f".local/share/applications/{APP_ID}.desktop",
                     f".local/share/gnome-shell/extensions/{EXTENSION_UUID}"):
            self.assertFalse((self.home / gone).exists(), gone)
        self.assertTrue(config.exists() and history.exists(), "user data is kept without --purge")
        self.assert_calamus_intact()
        self.run_installer("install")
        self.run_installer("uninstall", "--purge")
        self.assertFalse(config.exists() or history.exists())
        self.assert_calamus_intact()

    def test_refuses_to_replace_a_directory_it_did_not_install(self):
        foreign = self.home / ".local/lib/orfeuscribe"
        foreign.mkdir(parents=True)
        (foreign / "something.txt").write_text("not ours")
        result = self.run_installer("install", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to replace", result.stderr + result.stdout)
        self.assertEqual((foreign / "something.txt").read_text(), "not ours")
        self.run_installer("uninstall")
        self.assertTrue((foreign / "something.txt").exists(), "uninstall leaves unowned directories alone")

    def test_refuses_each_unowned_file_before_installing_anything(self):
        files = (".local/bin/orfeuscribe", f".local/share/applications/{APP_ID}.desktop",
                 f".local/share/icons/hicolor/scalable/apps/{APP_ID}.svg",
                 f".local/share/icons/hicolor/symbolic/apps/{APP_ID}-symbolic.svg")
        for name in files:
            with self.subTest(path=name):
                path = self.home / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("unrelated application")
                result = self.run_installer("install", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(path.read_text(), "unrelated application")
                self.assertFalse((self.home / ".local/lib/orfeuscribe").exists())
                path.unlink()

    def test_modified_launcher_is_preserved_on_uninstall(self):
        self.run_installer("install")
        launcher = self.home / ".local/bin/orfeuscribe"
        launcher.write_text("replacement belonging to the user")
        result = self.run_installer("install", check=False)
        self.assertNotEqual(result.returncode, 0)
        result = self.run_installer("uninstall")
        self.assertIn("Kept unowned or modified file", result.stdout)
        self.assertEqual(launcher.read_text(), "replacement belonging to the user")
        self.assert_calamus_intact()

    def test_uninstall_does_not_follow_arbitrary_manifest_paths(self):
        self.run_installer("install")
        unrelated = self.home / "important.txt"
        unrelated.write_text("keep this")
        manifest_path = self.home / ".local/lib/orfeuscribe/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"].append(str(unrelated))
        manifest["file_hashes"][str(unrelated)] = hashlib.sha256(unrelated.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        self.run_installer("uninstall")
        self.assertEqual(unrelated.read_text(), "keep this")

    def test_install_refuses_launcher_symlink(self):
        target = self.base / "unrelated.txt"
        target.write_text("keep this")
        launcher = self.home / ".local/bin/orfeuscribe"
        launcher.symlink_to(target)
        self.assertNotEqual(self.run_installer("install", check=False).returncode, 0)
        self.assertTrue(launcher.is_symlink())
        self.assertEqual(target.read_text(), "keep this")

    def test_default_install_and_purge_honor_custom_xdg_locations(self):
        config = self.base / "custom-config"
        data = self.base / "custom-data"
        env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(config), XDG_DATA_HOME=str(data))
        def run(*args):
            result = subprocess.run([sys.executable, str(INSTALLER), *args], env=env,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result
        run("install")
        self.assertTrue((data / "applications" / f"{APP_ID}.desktop").is_file())
        self.assertTrue((data / "gnome-shell/extensions" / EXTENSION_UUID).is_dir())
        (config / "orfeuscribe").mkdir(parents=True)
        (config / "orfeuscribe/settings.json").write_text("{}")
        (data / "orfeuscribe").mkdir()
        (data / "orfeuscribe/history.db").write_text("synthetic")
        run("uninstall", "--purge")
        self.assertFalse((config / "orfeuscribe").exists())
        self.assertFalse((data / "orfeuscribe").exists())
        self.assert_calamus_intact()


if __name__ == "__main__":
    unittest.main()

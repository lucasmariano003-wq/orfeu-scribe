"""Real window handlers with synthetic data, never shown and never on the real desktop.

GTK 4 needs a display. Under scripts/run_tests.py there is none, so the GTK
tests below skip and ``PrivateDisplayTests`` runs them again in a child process
inside a private Xvfb (``xvfb-run -a``), with the same isolated HOME, in-memory
settings and keyring, and no session bus. Without xvfb-run they are skipped.
No HTTP leaves the process: model lists are answered by a local fake server.
"""
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
CHILD = "ORFEUSCRIBE_WINDOW_CHILD"


def has_display():
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


@unittest.skipUnless(has_display() and os.environ.get(CHILD) == "1", "runs inside a private Xvfb")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts"))
        from ui_fixture import build
        cls.build = staticmethod(build)

    def setUp(self):
        from gi.repository import GLib
        self.GLib = GLib
        self.app, self.window, _ids = self.build("en")
        if self.window is None:
            self.skipTest("no display connection")
        self.toasts = []
        self.window.toast = self.toasts.append
        self.addCleanup(self.window.destroy)

    def pump(self, until, timeout=3.0):
        end = time.monotonic() + timeout
        while not until() and time.monotonic() < end:
            self.GLib.MainContext.default().iteration(False)
            time.sleep(.01)
        return until()

    # --- 3. A key belongs to the server it was saved for -----------------------------
    def test_new_server_asks_before_reusing_the_saved_key(self):
        config, store = self.app.config, self.app.credentials
        config.update(stt_base_url="https://speech.example.org/v1", stt_credential_server="https://speech.example.org/v1")
        store.store("stt", "SYNTHETIC-KEY")
        self.window.last_dialog = None
        self.window.stt_url.set_text("https://other.example.net/v1")
        self.window._save_endpoint("stt_base_url", self.window.stt_url)
        self.assertEqual(config["stt_base_url"], "https://other.example.net/v1")
        self.assertEqual(config["stt_credential_server"], "https://speech.example.org/v1", "not sent before asking")
        self.assertTrue(self.pump(lambda: self.window.last_dialog is not None), "the user is asked")
        self.window.last_dialog.emit("response", "keep")
        self.assertEqual(config["stt_credential_server"], "https://speech.example.org/v1")
        self.window.last_dialog = None
        self.window._save_endpoint("stt_base_url", self.window.stt_url)
        self.assertTrue(self.pump(lambda: self.window.last_dialog is not None))
        self.window.last_dialog.emit("response", "reuse")
        self.assertEqual(config["stt_credential_server"], "https://other.example.net/v1")

    def test_without_a_saved_key_the_slot_follows_the_server(self):
        config = self.app.config
        config.update(stt_credential_server="https://speech.example.org/v1")
        self.window.last_dialog = None
        self.window.stt_url.set_text("https://other.example.net/v1")
        self.window._save_endpoint("stt_base_url", self.window.stt_url)
        self.assertTrue(self.pump(lambda: config["stt_credential_server"] == "https://other.example.net/v1"))
        self.assertIsNone(self.window.last_dialog)

    def test_saving_a_key_links_it_to_the_server_shown(self):
        config = self.app.config
        config.update(stt_base_url="https://other.example.net/v1", stt_credential_server="https://speech.example.org/v1")
        self.window.stt_key.set_text("SYNTHETIC-NEW-KEY")
        self.window._save_key("stt", self.window.stt_key)
        self.assertEqual(config["stt_credential_server"], "", "unlinked while the keyring is written")
        self.assertTrue(self.pump(lambda: config["stt_credential_server"] == "https://other.example.net/v1"))
        self.assertEqual(self.app.credentials.lookup("stt"), "SYNTHETIC-NEW-KEY")
        self.assertNotIn("SYNTHETIC-NEW-KEY", config.path.read_text())

    # --- 4. Deleting an entry deletes the audio kept for it ----------------------------
    def test_delete_dialog_removes_the_linked_rescue(self):
        from tests.support import wav
        row = self.app.history.search()[0]
        rescue = self.app.rescue.save(wav(2), reason="error", history_id=row["id"])
        self.window._delete_dialog(row)
        self.window.last_dialog.emit("response", "delete")
        self.assertIsNone(self.app.history.get(row["id"]))
        self.assertIsNone(self.app.rescue.get(rescue))
        self.assertIn("Entry deleted.", self.toasts)


class PrivateDisplayTests(unittest.TestCase):
    """Runs WindowTests in a private Xvfb so the default suite exercises the real widgets."""

    @unittest.skipIf(os.environ.get(CHILD) == "1", "already inside the private display")
    @unittest.skipUnless(shutil.which("xvfb-run"), "xvfb-run is not installed")
    def test_window_handlers_in_a_private_display(self):
        env = {key: value for key, value in os.environ.items() if key not in ("DISPLAY", "WAYLAND_DISPLAY")}
        env.update({CHILD: "1", "GDK_BACKEND": "x11", "GSK_RENDERER": "cairo", "GTK_A11Y": "none",
                    "NO_AT_BRIDGE": "1", "GTK_IM_MODULE": "simple", "GSETTINGS_BACKEND": "memory",
                    "ORFEUSCRIBE_SECRET_BACKEND": "memory", "GIO_USE_VFS": "local", "GTK_USE_PORTAL": "0",
                    "LANGUAGE": "en", "PYTHONPATH": str(ROOT)})
        run = subprocess.run(["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24 -nolisten tcp", sys.executable, "-m",
                              "unittest", "-v", "tests.test_window"], cwd=ROOT, env=env, capture_output=True,
                             text=True, timeout=180)
        report = run.stdout + run.stderr
        self.assertEqual(run.returncode, 0, report[-6000:])
        self.assertNotIn("skipped 'runs inside", report)
        self.assertRegex(report, r"Ran \d+ tests")


if __name__ == "__main__":
    unittest.main()

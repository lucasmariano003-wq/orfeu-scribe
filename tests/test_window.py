"""Real window handlers with synthetic data, never shown and never on the real desktop.

GTK 4 needs a display. Under scripts/run_tests.py there is none, so the GTK
tests below skip and ``PrivateDisplayTests`` runs them again in a child process
inside a private Xvfb (``xvfb-run -a``), in the same restricted environment
scripts/run_tests.py builds: its own temporary HOME, in-memory settings and
keyring, no session bus and nothing else from the caller's environment, also
when this module is run on its own. Without xvfb-run they are skipped.
No HTTP leaves the process: model lists are answered by a local fake server.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
CHILD = "ORFEUSCRIBE_WINDOW_CHILD"
SENTINEL = "ORFEUSCRIBE_TEST_SENTINEL"
# What GTK needs inside the private Xvfb, on top of scripts/run_tests.py's restricted environment.
PRIVATE_DISPLAY = {CHILD: "1", "GDK_BACKEND": "x11", "GSK_RENDERER": "cairo", "GTK_IM_MODULE": "simple",
                   "GIO_USE_VFS": "local", "GTK_USE_PORTAL": "0", "LANGUAGE": "en", "PYTHONPATH": str(ROOT)}
# xvfb-run is a shell script: it sets the display and its cookie, and the shell exports these.
ADDED_BY_XVFB_RUN = {"DISPLAY", "XAUTHORITY", "PWD", "OLDPWD", "SHLVL", "_"}


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
        # A HOME per test: each window starts from the fixture's own history and settings, whatever ran before.
        home = tempfile.mkdtemp(prefix="test-", dir=os.environ["HOME"])
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        environment = patch.dict(os.environ, HOME=home)
        environment.start()
        self.addCleanup(environment.stop)
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

    def test_about_names_the_author(self):
        from gi.repository import Adw
        made = []
        # Replace only present(), and on the class: the dialog is the real one, never shown.
        with patch.object(Adw.AboutDialog, "present", lambda dialog, _parent: made.append(dialog)):
            self.window._about()
        self.assertEqual(made[0].get_developer_name(), "Lucas Mariano")
        self.assertEqual(made[0].get_copyright(), "© 2026 Lucas Mariano")

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

    # --- 10. Listing models needs only the server URL -----------------------------------
    def test_list_models_works_before_any_model_is_chosen(self):
        from orfeuscribe.providers.openai_compat import HttpClient
        self.app.config.update(stt_base_url="http://127.0.0.1:9/v1", stt_model="", revision_model="",
                               stt_credential_server="http://127.0.0.1:9/v1")
        self.app.reload_provider()
        ready = []
        self.window._models_ready = lambda which, models, error: ready.append((which, models, error))
        with patch.object(HttpClient, "list_models", return_value=["synthetic-model"]) as listing:
            for which in ("stt", "revision"):
                self.window._fetch_models(which)
            self.assertTrue(self.pump(lambda: len(ready) == 2))
        self.assertEqual(listing.call_count, 2)
        self.assertEqual(sorted(ready), [("revision", ["synthetic-model"], ""), ("stt", ["synthetic-model"], "")])
        self.assertNotIn("Save the server URL and a model first.", self.toasts)

    # --- 12. The revision switch updates the rows that depend on it -------------------
    def test_revision_switch_updates_related_rows(self):
        self.app.config.update(revision_enabled=True, revision_same_server=False)
        self.window._sync_revision()
        self.window.revision_switch.set_active(False)
        self.assertFalse(self.app.config["revision_enabled"])
        self.assertFalse(self.window.revision_model.get_sensitive())
        self.assertFalse(self.window.same_switch.get_sensitive())
        self.assertFalse(self.window.revision_url.get_visible())
        self.window.revision_switch.set_active(True)
        self.assertTrue(self.window.revision_model.get_sensitive())
        self.assertTrue(self.window.same_switch.get_sensitive())
        self.assertTrue(self.window.revision_url.get_visible(), "a separate server shows its own address")


def runner():
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import run_tests
    return run_tests


@unittest.skipUnless(os.environ.get(CHILD) == "1", "runs inside a private Xvfb")
class ChildEnvironmentTests(unittest.TestCase):
    """Looks at the environment the Xvfb child really received, not at the code that built it."""

    def test_only_the_restricted_environment_reaches_the_child(self):
        # Names only: a failure report must never print the values of a variable that leaked.
        received = set(os.environ)
        self.assertNotIn(SENTINEL, received, "a variable outside the allowlist reached the child")
        with tempfile.TemporaryDirectory() as name:
            allowed = set(runner().KEEP) | set(runner().isolated_environment(Path(name)))
        allowed |= set(PRIVATE_DISPLAY) | ADDED_BY_XVFB_RUN
        self.assertEqual(sorted(received - allowed), [])


class PrivateDisplayTests(unittest.TestCase):
    """Runs WindowTests in a private Xvfb so the default suite exercises the real widgets."""

    @unittest.skipIf(os.environ.get(CHILD) == "1", "already inside the private display")
    @unittest.skipUnless(shutil.which("xvfb-run"), "xvfb-run is not installed")
    def test_window_handlers_in_a_private_display(self):
        # The runner's own restricted environment, also when this test is run on its own: the fixture
        # writes its synthetic history and settings under HOME, on a live session bus every application
        # after the first one fails to export its object path, and nothing else from the caller's
        # environment belongs in the child. The synthetic sentinel proves it; the child checks for it.
        with tempfile.TemporaryDirectory(prefix="orfeuscribe-window-") as name, \
                patch.dict(os.environ, {SENTINEL: "synthetic-sentinel-not-a-secret"}):
            env = runner().prepared_environment(Path(name))
            env.update(PRIVATE_DISPLAY)
            run = subprocess.run(["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24 -nolisten tcp", sys.executable,
                                  "-m", "unittest", "-v", "tests.test_window"], cwd=ROOT, env=env,
                                 capture_output=True, text=True, timeout=180)
        report = run.stdout + run.stderr
        self.assertEqual(run.returncode, 0, report[-6000:])
        self.assertNotIn("skipped 'runs inside", report)
        self.assertRegex(report, r"Ran \d+ tests")
        # Only this test skips itself inside the child; a window that could not be built is a failure.
        self.assertIn("OK (skipped=1)", report)


if __name__ == "__main__":
    unittest.main()

"""Project-wide guarantees: separation from Calamus, opt-in defaults, consistent names, audit and translations."""
import gettext
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from orfeuscribe import config, ids
from orfeuscribe.config import Config
from orfeuscribe.desktop import desktop_entry

ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "extension" / ids.EXTENSION_UUID
SCHEMA = EXTENSION / "schemas" / f"{ids.SCHEMA_ID}.gschema.xml"


def schema_keys():
    root = ElementTree.parse(SCHEMA).getroot()
    schema = root.find("schema")
    return schema, {key.get("name"): key.findtext("default") for key in schema.findall("key")}


class SeparationTests(unittest.TestCase):
    def test_no_identifier_collides_with_calamus(self):
        for value in (ids.APP_ID, ids.BUS_NAME, ids.BUS_PATH, ids.EXTENSION_UUID, ids.SCHEMA_ID, ids.SLUG,
                      ids.SECRET_SCHEMA, str(config.CONFIG_DIR), str(config.DATA_DIR)):
            self.assertNotIn("calamus", value.lower())
        self.assertTrue(str(config.CONFIG_DIR).endswith("/orfeuscribe"))
        schema, _keys = schema_keys()
        self.assertEqual((schema.get("id"), schema.get("path")), (ids.SCHEMA_ID, "/org/gnome/shell/extensions/orfeuscribe/"))

    def test_extension_matches_the_service(self):
        source = (EXTENSION / "extension.js").read_text()
        self.assertIn(f"const BUS = '{ids.BUS_NAME}';", source)
        self.assertIn(f"const PATH = '{ids.BUS_PATH}';", source)
        self.assertIn("/.local/bin/orfeuscribe", source)
        metadata = json.loads((EXTENSION / "metadata.json").read_text())
        self.assertEqual((metadata["uuid"], metadata["settings-schema"], metadata["gettext-domain"]),
                         (ids.EXTENSION_UUID, ids.SCHEMA_ID, ids.GETTEXT_DOMAIN))
        _schema, keys = schema_keys()
        for name in re.findall(r"addKeybinding\('([a-z-]+)'", source):
            self.assertIn(name, keys)
        for name, _operation in re.findall(r"\['([a-z-]+)', '([a-z]+)'\]", source.split("const SHORTCUTS = [", 1)[1].split("];", 1)[0]):
            self.assertIn(name, keys)
        for name in re.findall(r"this\._settings\.(?:get|set)_(?:boolean|string|strv)\('([a-z-]+)'", source):
            self.assertIn(name, keys)

    def test_service_xml_and_extension_xml_agree(self):
        from orfeuscribe.service import BUS_XML
        source = (EXTENSION / "extension.js").read_text()
        for kind, name in re.findall(r'<(method|signal) name="(\w+)"', source):
            self.assertIn(f'<{kind} name="{name}"', BUS_XML)


class DefaultTests(unittest.TestCase):
    def test_every_shortcut_starts_empty_and_watcher_off(self):
        _schema, keys = schema_keys()
        shortcuts = [name for name in keys if name.endswith(("-shortcut", "-accelerator")) and name != "card-close-shortcut"]
        self.assertEqual(len(shortcuts), 7)
        for name in shortcuts:
            self.assertEqual(keys[name], "[]", name)
        self.assertEqual(keys["watcher-enabled"], "false")

    def test_opt_in_features_are_off_and_nothing_personal_is_seeded(self):
        with tempfile.TemporaryDirectory() as name:
            settings = Config(Path(name))
            for key in ("learn_from_history", "reply_context", "live_text", "autostart", "notes_wikilinks"):
                self.assertFalse(settings[key], key)
            self.assertEqual((settings["stt_base_url"], settings["stt_model"], settings["revision_model"]), ("", "", ""))
            self.assertEqual((settings["notes_folder"], settings["audio_keep"]), ("", 0))
            self.assertEqual(settings["specialized_modes"], [])
            self.assertFalse(settings.stt_configured or settings.revision_configured)
            self.assertFalse(list(Path(name).iterdir()), "reading settings creates no file")

    def test_desktop_entry(self):
        entry = desktop_entry(launcher="/tmp/a b/orfeuscribe")
        self.assertIn('Exec="/tmp/a b/orfeuscribe"', entry)
        self.assertIn(f"Icon={ids.APP_ID}", entry)
        self.assertIn("Comment[pt_BR]=", entry)
        self.assertNotIn("Autostart", entry)


class AuditTests(unittest.TestCase):
    def test_no_findings_outside_the_allowlist(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import audit
        blocking = audit.blocking(audit.audit())
        self.assertEqual(blocking, [], "\n".join(f"{p}:{l} {r}: {t!r}" for r, p, l, t in blocking))

    def test_prompts_and_interface_carry_no_personal_style(self):
        from orfeuscribe import text
        joined = " ".join(str(value) for value in vars(text).values() if isinstance(value, str))
        for word in ("Padre", "homilia", "secretário", "Livro", "Grok", "Hermes"):
            self.assertNotIn(word, joined)


class TranslationTests(unittest.TestCase):
    def test_template_is_current_and_pt_br_is_complete(self):
        run = subprocess.run([sys.executable, str(ROOT / "scripts/extract_strings.py"), "--check"], capture_output=True,
                             text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_compiled_catalog_loads_with_plurals(self):
        with tempfile.TemporaryDirectory() as name:
            subprocess.run([sys.executable, str(ROOT / "scripts/compile_translations.py"), name], check=True,
                           capture_output=True)
            for base in (Path(name) / "locale", Path(name) / "extension" / ids.EXTENSION_UUID / "locale"):
                catalog = gettext.translation(ids.GETTEXT_DOMAIN, str(base), languages=["pt_BR"])
                self.assertEqual(catalog.gettext("Settings"), "Configurações")
                self.assertEqual(catalog.gettext("Translate into: %s"), "Traduzir para: %s")
                self.assertEqual(catalog.ngettext("{count} term saved.", "{count} terms saved.", 1), "{count} termo salvo.")
                self.assertEqual(catalog.ngettext("{count} term saved.", "{count} terms saved.", 3), "{count} termos salvos.")
            from orfeuscribe import i18n
            try:
                i18n.reload(Path(name) / "locale", ["pt_BR"])
                from orfeuscribe import modes
                self.assertEqual(modes.title("prompt"), "Instrução para IA")
                self.assertEqual(i18n.ngettext("{count} entry added.", "{count} entries added.", 2), "{count} entradas adicionadas.")
            finally:
                i18n.reload(languages=["en"])

    def test_interface_is_english_without_a_catalog(self):
        from orfeuscribe import i18n, modes
        i18n.reload(Path("/nonexistent"), ["pt_BR"])
        try:
            self.assertEqual(modes.title("prompt"), "AI instruction")
        finally:
            i18n.reload(languages=["en"])


class ExtensionScriptTests(unittest.TestCase):
    @unittest.skipUnless(os.path.exists("/usr/bin/gjs"), "gjs is not installed")
    def test_extension_modules_parse(self):
        run = subprocess.run(["gjs", "-m", "scripts/check_extension.js", str(EXTENSION / "extension.js"),
                              str(EXTENSION / "prefs.js")], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()

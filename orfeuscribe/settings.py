"""One GSettings schema serves the service, the window and the GNOME Shell extension."""
import os
from pathlib import Path

from gi.repository import Gio

from .config import ROOT
from .i18n import _
from .ids import EXTENSION_UUID, SCHEMA_ID


def schema_dirs():
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return [Path(os.environ["ORFEUSCRIBE_SCHEMA_DIR"])] if os.environ.get("ORFEUSCRIBE_SCHEMA_DIR") else [
        ROOT / "extension" / EXTENSION_UUID / "schemas",
        data / "gnome-shell/extensions" / EXTENSION_UUID / "schemas"]


def shell_settings():
    for path in schema_dirs():
        if (path / "gschemas.compiled").exists():
            source = Gio.SettingsSchemaSource.new_from_directory(str(path), Gio.SettingsSchemaSource.get_default(), False)
            schema = source.lookup(SCHEMA_ID, False)
            if schema:
                return Gio.Settings.new_full(schema, None, None)
    raise RuntimeError(_("The settings schema is not compiled. Run ./install.sh or scripts/compile_schemas.sh."))

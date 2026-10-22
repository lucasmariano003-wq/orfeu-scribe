"""Desktop launcher entry and the optional login autostart entry."""
import os
from pathlib import Path

from .config import write_private
from .ids import APP_ID, APP_NAME, SLUG


def home_paths():
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return {"launcher": home / ".local/bin" / SLUG,
            "desktop": data / "applications" / f"{APP_ID}.desktop",
            "autostart": config / "autostart" / f"{APP_ID}.desktop"}


def desktop_entry(background=False, launcher=None):
    path = str(launcher or home_paths()["launcher"])
    escaped = path.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
    lines = ["[Desktop Entry]", "Type=Application", f"Name={APP_NAME}",
             "Comment=Voice dictation with a speech service you choose",
             "Comment[pt_BR]=Ditado por voz com o serviço de transcrição que você escolher",
             "Keywords=dictation;speech;voice;transcription;",
             f'Exec="{escaped}"' + (" --background" if background else ""),
             f"Icon={APP_ID}", "Terminal=false", "Categories=Utility;Accessibility;",
             f"StartupNotify={'false' if background else 'true'}", f"StartupWMClass={APP_ID}"]
    if background:
        lines.append("X-GNOME-Autostart-enabled=true")
    return "\n".join(lines) + "\n"


def set_autostart(enabled):
    path = home_paths()["autostart"]
    if enabled:
        write_private(path, desktop_entry(True))
    else:
        path.unlink(missing_ok=True)

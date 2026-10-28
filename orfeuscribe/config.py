"""Local preferences. API keys never enter this module or its file (see secrets.py)."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path

from .ids import SLUG

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / SLUG
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / SLUG

# Window class or title (regular expression) → mode. Visible and editable in Settings.
DEFAULT_RULES = [
    {"pattern": r"chatgpt|claude|gemini|copilot|perplexity|\bcodex\b", "mode": "prompt"},
    {"pattern": r"discord|slack|telegram|signal|whatsapp|element|teams", "mode": "message"},
    {"pattern": r"thunderbird|geary|evolution|gmail|outlook|\bmail\b", "mode": "email"},
]
HINTS = ("auto", "keywords", "prompt", "none")


def _default_language():
    from .languages import system_default
    return system_default()


DEFAULTS = {
    # Speech-to-text service (OpenAI-compatible /audio/transcriptions).
    "stt_base_url": "", "stt_model": "", "stt_hints": "auto",
    # Text revision service (OpenAI-compatible /chat/completions).
    "revision_enabled": True, "revision_same_server": True, "revision_base_url": "", "revision_model": "",
    # The server each saved API key belongs to (an address, never the key). A key
    # is only sent to that server; "" means the key is not linked to any server yet.
    "stt_credential_server": "", "revision_credential_server": "",
    "understand_intent": True,
    # Opt-in features that send more data or cost more calls: off by default.
    "learn_from_history": False, "reply_context": False, "live_text": False,
    # Recording.
    "microphone": "", "sounds": True, "silence_stop": False, "silence_seconds": 3.0, "pause_delivery": False,
    # Modes and languages.
    "rules": DEFAULT_RULES, "specialized_modes": [], "input_language": None, "language_rules": [],
    # Retention: 0 keeps history forever; audio is kept only for the last N entries (0 = none).
    "history_days": 0, "audio_keep": 0,
    # Optional Markdown notes folder for "note: …" dictations. Empty = off.
    "notes_folder": "", "note_subfolders": ["Inbox"], "notes_wikilinks": False,
    "autostart": False,
}
BOOLEANS = ("revision_enabled", "revision_same_server", "understand_intent", "learn_from_history",
            "reply_context", "live_text", "sounds", "silence_stop", "pause_delivery", "notes_wikilinks", "autostart")
STRINGS = ("stt_base_url", "stt_model", "revision_base_url", "revision_model", "microphone", "notes_folder",
           "stt_credential_server", "revision_credential_server")
# Key slot → (setting with the server the key belongs to, setting with the server URL).
CREDENTIAL_SERVERS = {"stt": ("stt_credential_server", "stt_base_url"),
                      "revision": ("revision_credential_server", "revision_base_url")}


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_private(path: Path, text: str) -> None:
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def valid_endpoint(url: str) -> bool:
    """HTTPS anywhere; plain HTTP only to this computer (local servers)."""
    from urllib import parse
    try:
        parts = parse.urlsplit(url.strip())
    except ValueError:
        return False
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")


def endpoint_identity(url: str) -> str:
    """The server an address designates, in one spelling; "" for an empty or invalid address.

    Scheme and host are case-insensitive, the default port and a trailing slash do
    not matter; the path does (two paths can be two services on the same host).
    """
    if not url or not valid_endpoint(url):
        return ""
    from urllib import parse
    parts = parse.urlsplit(url.strip())
    scheme, host = parts.scheme.lower(), parts.hostname.lower()
    try:
        port = parts.port
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    if port and port != {"https": 443, "http": 80}[scheme]:
        host = f"{host}:{port}"
    return f"{scheme}://{host}{parts.path.rstrip('/')}"


class Config:
    def __init__(self, directory: Path = CONFIG_DIR):
        self.directory = directory
        self.path = directory / "settings.json"
        self.data = copy.deepcopy(DEFAULTS)
        stored = None
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (FileNotFoundError, ValueError, AttributeError):
            stored = None
        self._validate()
        if isinstance(stored, dict) and any(binding not in stored for binding, _url in CREDENTIAL_SERVERS.values()):
            # Settings written before keys were linked to a server: until now the saved key
            # was being sent to the configured URL, so it stays linked to that URL only.
            for binding, url in CREDENTIAL_SERVERS.values():
                if binding not in stored:
                    self.data[binding] = endpoint_identity(self.data[url])
            try:
                self.save()
            except OSError:
                pass

    def _validate(self):
        data = self.data
        for key in BOOLEANS:
            if not isinstance(data[key], bool):
                data[key] = DEFAULTS[key]
        for key in STRINGS:
            if not isinstance(data[key], str):
                data[key] = ""
        for key in ("stt_base_url", "revision_base_url"):
            if data[key] and not valid_endpoint(data[key]):
                data[key] = ""
        for binding, _url in CREDENTIAL_SERVERS.values():
            data[binding] = endpoint_identity(data[binding])
        if data["stt_hints"] not in HINTS:
            data["stt_hints"] = "auto"
        if not isinstance(data["rules"], list):
            data["rules"] = copy.deepcopy(DEFAULT_RULES)
        from .modes import SPECIALIZED
        if not isinstance(data["specialized_modes"], list):
            data["specialized_modes"] = []
        data["specialized_modes"] = [mode for mode in SPECIALIZED if mode in data["specialized_modes"]]
        try:
            data["audio_keep"] = max(0, min(100, int(data["audio_keep"])))
        except (ValueError, TypeError):
            data["audio_keep"] = 0
        try:
            data["history_days"] = max(0, min(36500, int(data["history_days"])))
            data["silence_seconds"] = max(1.0, min(30.0, float(data["silence_seconds"])))
        except (TypeError, ValueError, OverflowError):
            data["history_days"], data["silence_seconds"] = 0, 3.0
        from .languages import LANGUAGES
        if data["input_language"] not in LANGUAGES:
            data["input_language"] = _default_language()
        if not isinstance(data["language_rules"], list):
            data["language_rules"] = []
        from .notes import valid_folder
        folders = data["note_subfolders"]
        if not isinstance(folders, list) or not folders or not all(valid_folder(x) for x in folders):
            data["note_subfolders"] = ["Inbox"]

    @property
    def stt_configured(self) -> bool:
        return bool(self.data["stt_base_url"] and self.data["stt_model"])

    @property
    def revision_configured(self) -> bool:
        base = self.data["stt_base_url"] if self.data["revision_same_server"] else self.data["revision_base_url"]
        return bool(self.data["revision_enabled"] and base and self.data["revision_model"])

    def get(self, key, default=None):
        return self.data.get(key, default)

    def save(self):
        write_private(self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()

    def update(self, **values):
        self.data.update(values)
        self._validate()
        self.save()


def valid_pattern(text: str) -> bool:
    try:
        re.compile(text)
        return bool(text.strip())
    except re.error:
        return False

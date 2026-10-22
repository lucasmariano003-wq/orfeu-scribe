"""API keys: the desktop keyring (Secret Service) or environment variables.

Keys are never written to the settings file, the history, logs or error
messages. Environment variables take precedence, which is how tests, scripts
and headless setups pass a key without touching the keyring:

    ORFEUSCRIBE_STT_API_KEY        key for the speech-to-text service
    ORFEUSCRIBE_REVISION_API_KEY   key for the revision service (when it differs)

``ORFEUSCRIBE_SECRET_BACKEND=memory`` replaces the keyring with an in-memory
store (tests and screenshots must never touch the real keyring).
"""
from __future__ import annotations

import os

from .i18n import _
from .ids import APP_NAME, SECRET_SCHEMA

SLOTS = ("stt", "revision")
ENVIRONMENT = {"stt": "ORFEUSCRIBE_STT_API_KEY", "revision": "ORFEUSCRIBE_REVISION_API_KEY"}


class SecretError(Exception):
    """A message that is safe to show; never includes the key."""


class MemoryStore:
    name = "memory"

    def __init__(self, values=None):
        self.values = dict(values or {})

    def lookup(self, slot):
        return self.values.get(slot) or None

    def store(self, slot, value):
        self.values[slot] = value

    def clear(self, slot):
        self.values.pop(slot, None)


class SecretServiceStore:
    """GNOME Keyring, KWallet or any other Secret Service provider, via libsecret."""
    name = "keyring"

    def __init__(self):
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        self.Secret = Secret
        self.schema = Secret.Schema.new(SECRET_SCHEMA, Secret.SchemaFlags.NONE,
                                        {"slot": Secret.SchemaAttributeType.STRING})

    def lookup(self, slot):
        try:
            return self.Secret.password_lookup_sync(self.schema, {"slot": slot}, None) or None
        except Exception:  # noqa: BLE001 — GLib.Error text may name the collection; never show it
            raise SecretError(_("The keyring is locked or unavailable.")) from None

    def store(self, slot, value):
        label = f"{APP_NAME}: " + (_("speech-to-text API key") if slot == "stt" else _("text revision API key"))
        try:
            if not self.Secret.password_store_sync(self.schema, {"slot": slot}, self.Secret.COLLECTION_DEFAULT,
                                                   label, value, None):
                raise SecretError(_("The keyring did not accept the key."))
        except SecretError:
            raise
        except Exception:  # noqa: BLE001
            raise SecretError(_("The keyring is locked or unavailable.")) from None

    def clear(self, slot):
        try:
            self.Secret.password_clear_sync(self.schema, {"slot": slot}, None)
        except Exception:  # noqa: BLE001
            raise SecretError(_("The keyring is locked or unavailable.")) from None


class UnavailableStore:
    name = "unavailable"

    def lookup(self, slot):
        return None

    def store(self, slot, value):
        raise SecretError(_("No keyring is available. Set the key with an environment variable instead."))

    def clear(self, slot):
        pass


def default_store():
    if os.environ.get("ORFEUSCRIBE_SECRET_BACKEND") == "memory":
        return MemoryStore()
    try:
        return SecretServiceStore()
    except (ImportError, ValueError):
        return UnavailableStore()


def api_key(store, slot):
    """The key for a slot, or None. Environment first, then the store."""
    if slot not in SLOTS:
        raise ValueError(slot)
    value = os.environ.get(ENVIRONMENT[slot], "").strip()
    if value:
        return value
    value = store.lookup(slot) if store else None
    return value.strip() if value else None


def from_environment(slot) -> bool:
    return bool(os.environ.get(ENVIRONMENT[slot], "").strip())

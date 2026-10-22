"""Interface translations (gettext). English is the source language.

Compiled catalogs live in ``<root>/locale/<lang>/LC_MESSAGES/orfeuscribe.mo``.
They are generated from ``po/*.po`` by ``scripts/compile_translations.py`` and
by the installer; without them the interface is shown in English.
"""
from __future__ import annotations

import gettext
import os
from pathlib import Path

from .ids import GETTEXT_DOMAIN

ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIR = Path(os.environ.get("ORFEUSCRIBE_LOCALE_DIR", ROOT / "locale"))

_translation = gettext.translation(GETTEXT_DOMAIN, str(LOCALE_DIR), fallback=True)


def _(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, count: int) -> str:
    return _translation.ngettext(singular, plural, count)


def N_(message: str) -> str:
    """Marks a string for extraction; it is translated later, where it is shown."""
    return message


def reload(locale_dir: Path | None = None, languages=None) -> None:
    """Used by tests and screenshots to switch the catalog without restarting."""
    global _translation
    directory = str(locale_dir or LOCALE_DIR)
    _translation = gettext.translation(GETTEXT_DOMAIN, directory, languages=languages, fallback=True)

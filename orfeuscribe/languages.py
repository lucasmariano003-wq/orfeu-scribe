"""Spoken language: default, spoken prefix ("in Spanish: …") and per-window rules."""
from __future__ import annotations

import locale
import os
import re

from .i18n import N_, _

# ISO 639-1 codes sent to the speech service as a hint. The names below are the
# English names used inside model prompts; the interface shows the translated label.
LANGUAGES = {"en": "English", "pt": "Portuguese", "es": "Spanish", "fr": "French",
             "de": "German", "it": "Italian", "la": "Latin"}
_LABELS = {"en": N_("English"), "pt": N_("Portuguese"), "es": N_("Spanish"), "fr": N_("French"),
           "de": N_("German"), "it": N_("Italian"), "la": N_("Latin")}

# Spoken prefixes that switch the language of one dictation. Only languages with
# a reviewed command vocabulary have prefixes (see commands.py).
_PREFIX_NAMES = {
    "en": {"english": "en", "portuguese": "pt", "spanish": "es", "french": "fr", "german": "de",
           "italian": "it", "latin": "la"},
    "pt": {"inglês": "en", "ingles": "en", "português": "pt", "portugues": "pt", "espanhol": "es",
           "francês": "fr", "frances": "fr", "alemão": "de", "alemao": "de", "italiano": "it", "latim": "la"},
    "es": {"inglés": "en", "ingles": "en", "portugués": "pt", "portugues": "pt", "español": "es",
           "espanol": "es", "francés": "fr", "frances": "fr", "alemán": "de", "aleman": "de",
           "italiano": "it", "latín": "la", "latin": "la"},
}
_PREFIX_WORD = {"en": "in", "pt": "em", "es": "en"}


def label(code: str) -> str:
    return _(_LABELS[code]) if code in _LABELS else code


def system_default() -> str:
    """The interface locale's language when supported, otherwise English."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "")
        if value:
            code = value.split("_", 1)[0].split(".", 1)[0].lower()
            return code if code in LANGUAGES else "en"
    try:
        code = (locale.getlocale()[0] or "en").split("_", 1)[0].lower()
    except ValueError:
        code = "en"
    return code if code in LANGUAGES else "en"


def language_prefix(text: str):
    """("es", rest) when the dictation starts with "in Spanish:" (or em/en …); else (None, text)."""
    for spoken, names in _PREFIX_NAMES.items():
        pattern = re.compile(r"^\s*" + _PREFIX_WORD[spoken] + r"\s+(" + "|".join(map(re.escape, names)) +
                             r")\s*[:.,;!?—-]\s*", re.I)
        match = pattern.match(text)
        if match:
            return names[match[1].casefold()], text[match.end():].strip()
    return None, text


def choose_language(context, rules=(), default="en"):
    for rule in rules:
        try:
            if rule["language"] in LANGUAGES and re.search(rule["pattern"],
                    (context.get("wm_class", "") + "\n" + context.get("title", ""))[:4096], re.I):
                return rule["language"]
        except (re.error, KeyError, TypeError):
            continue
    return default if default in LANGUAGES else "en"

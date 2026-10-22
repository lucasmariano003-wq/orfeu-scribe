"""Writing modes: five general ones and optional specialized ones."""
from __future__ import annotations

from .i18n import N_, _

GENERAL = ("dictation", "literal", "message", "email", "prompt")
# Off until the user turns them on in Settings → Modes.
SPECIALIZED = ("outline", "script")
MODES = GENERAL + SPECIALIZED

_TITLES = {"dictation": N_("Revised text"), "literal": N_("Literal dictation"), "message": N_("Message"),
           "email": N_("Email"), "prompt": N_("AI instruction"), "outline": N_("Talk outline"),
           "script": N_("Video script")}
_DESCRIPTIONS = {
    "dictation": N_("Clean prose with punctuation and paragraphs."),
    "literal": N_("Your words in your order; only punctuation and spelling."),
    "message": N_("Short chat messages, without added formalities."),
    "email": N_("Clear paragraphs. Greeting and sign-off only when dictated."),
    "prompt": N_("Requirements organized for an AI assistant, never answered."),
    "outline": N_("“New point” and “quotation” become structure; nothing is invented."),
    "script": N_("“New scene”, “hard cut” and “on screen” become structure, in spoken order."),
}
# Operations that are not modes but still appear in the history.
_OTHER = {"question": N_("Question"), "translation": N_("Translation"), "note": N_("Note"),
          "edit": N_("Edit"), "write": N_("Draft"), "file": N_("File")}


def title(mode: str) -> str:
    return _(_TITLES.get(mode) or _OTHER.get(mode) or mode)


def description(mode: str) -> str:
    return _(_DESCRIPTIONS.get(mode, ""))


def available(config) -> tuple[str, ...]:
    enabled = set(config.get("specialized_modes", []) if config else [])
    return GENERAL + tuple(mode for mode in SPECIALIZED if mode in enabled)

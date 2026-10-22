"""Spoken formatting commands, recognized locally before any model sees the text.

Each language has a small, reviewed vocabulary. Phrases were chosen to be
unlikely in ordinary speech: English uses "full stop" but not "period", and
"cancel dictation" rather than "delete everything". A command only counts at
the end of a sentence (or of the whole dictation); a quotation in the middle of
a sentence stays intact. Languages without a table simply have no commands;
the speaker's default language still applies when a prefix such as
"in Latin:" switches the language of one dictation.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

SPOKEN = {
    "en": {"period": ("full stop",), "line": ("new line",), "paragraph": ("new paragraph",),
           "cancel": ("cancel dictation",)},
    "pt": {"period": ("ponto final",), "line": ("nova linha",), "paragraph": ("novo parágrafo",),
           "cancel": ("apaga tudo",)},
    "es": {"period": ("punto final",), "line": ("nueva línea",), "paragraph": ("nuevo párrafo",),
           "cancel": ("cancelar dictado",)},
}
SIGNS = {"period": ".", "line": "\n", "paragraph": "\n\n"}

# Structure for the specialized modes: phrase → kind, per language.
STRUCTURE = {
    "en": {"new point": "point", "quotation": "quote", "new scene": "scene", "hard cut": "cut",
           "on screen": "screen"},
    "pt": {"novo ponto": "point", "citação": "quote", "cena": "scene", "corte": "cut", "na tela": "screen"},
    "es": {"nuevo punto": "point", "cita": "quote", "nueva escena": "scene", "corte": "cut",
           "en pantalla": "screen"},
}
RENDERED = {
    "en": {"point": "Point {}", "quote": "Quotation", "scene": "Scene {}", "cut": "[Cut]", "screen": "On screen:"},
    "pt": {"point": "Ponto {}", "quote": "Citação", "scene": "Cena {}", "cut": "[Corte]", "screen": "Na tela:"},
    "es": {"point": "Punto {}", "quote": "Cita", "scene": "Escena {}", "cut": "[Corte]", "screen": "En pantalla:"},
}
MODE_KINDS = {"outline": ("point", "quote"), "script": ("scene", "cut", "screen")}
STRUCTURE_MARK = re.compile(r"⟦VD_STRUCTURE_\d+⟧")


@dataclass(frozen=True)
class SpokenText:
    text: str
    cancelled: bool = False
    suffix: str = ""


def _languages(languages):
    seen = []
    for language in languages:
        if language in SPOKEN and language not in seen:
            seen.append(language)
    return seen


def structure_phrases(mode, languages=("en",)):
    """Phrases worth passing to the recognizer as keywords in a specialized mode."""
    kinds = MODE_KINDS.get(mode, ())
    return tuple(phrase for language in _languages(languages)
                 for phrase, kind in STRUCTURE[language].items() if kind in kinds)


def capitalize_line_starts(text: str) -> str:
    """Capital letter after a line break, without rewriting words.

    Quotes, dashes and digits before the first letter are allowed, and accented
    letters work. The first line and the dictated spacing are left untouched.
    """
    lines = text.split("\n")
    for index in range(1, len(lines)):
        for offset, char in enumerate(lines[index]):
            if char.isalpha():
                lines[index] = lines[index][:offset] + char.upper() + lines[index][offset + 1:]
                break
    return "\n".join(lines)


def spoken_commands(text: str, languages=("en",)) -> SpokenText:
    text = text.strip()
    tables = [SPOKEN[language] for language in _languages(languages)]
    if not tables:
        return SpokenText(text)
    cancel = "|".join(re.escape(p) for table in tables for p in table["cancel"])
    if re.search(r"(?:^|\s)(?:" + cancel + r")[.!?,;:\s]*$", text, re.I):
        return SpokenText("", True)
    kinds = {}
    for table in tables:
        for kind in SIGNS:
            for phrase in table[kind]:
                kinds[phrase.casefold()] = kind
    alternatives = "|".join(re.escape(p) for p in sorted(kinds, key=len, reverse=True))
    command = re.compile(r"\b(" + alternatives + r")\b(?=[.!?,;:\s]*(?:$|(?:" + alternatives +
                         r")\b)|[.!?,;:])", re.I)
    tokens = {kind: chr(0xE000 + n) for n, kind in enumerate(SIGNS)}
    text = command.sub(lambda m: tokens[kinds[m.group().casefold()]], text)
    for kind, token in tokens.items():
        before = r"[.!?,;: \t]*" if kind == "period" else r"[,;: \t]*"
        text = re.sub(before + re.escape(token) + r"[.!?,;: \t]*", SIGNS[kind], text)
    text = re.sub(r"\.([^\W\d_])", r". \1", text)
    suffix = "\n\n" if text.endswith("\n\n") else "\n" if text.endswith("\n") else ""
    return SpokenText(text, suffix=suffix)


def structural_commands(text, mode, languages=("en",)):
    """Markers only at the start or after punctuation; casual mentions stay intact."""
    kinds = MODE_KINDS.get(mode, ())
    phrases = {phrase.casefold(): (kind, language) for language in _languages(languages)
               for phrase, kind in STRUCTURE[language].items() if kind in kinds}
    if not phrases:
        return text, {}
    alternatives = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    pattern = re.compile(r'(^|(?<=[.!?;,\n])\s*)(' + alternatives + r')\b[\s:,.!?;—-]*', re.I)
    markers, counts = {}, {'point': 0, 'scene': 0}

    def replace(match):
        kind, language = phrases[match[2].casefold()]
        if kind in counts:
            counts[kind] += 1
        label = RENDERED[language][kind].format(counts.get(kind, ""))
        rendered = {'point': f"\n\n{label}\n", 'quote': f'\n\n{label}\n> ', 'scene': f"\n\n{label}\n",
                    'cut': f'\n\n{label}\n', 'screen': f'\n\n{label} '}[kind]
        marker = f'⟦VD_STRUCTURE_{len(markers)}⟧'
        markers[marker] = rendered
        return marker
    return pattern.sub(replace, text), markers


def render_structure(text, structure):
    """Structure markers become the spoken structure, as the revision would."""
    for marker, rendered in structure.items():
        text = re.sub(r"\s*" + re.escape(marker) + r"\s*", rendered, text)
    return STRUCTURE_MARK.sub(" ", text).strip()

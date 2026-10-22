"""Preferred spellings, terms for speech recognition and explicit corrections.

The vocabulary starts empty. One term per line; ``wrong => right`` fixes a
recurring misspelling; lines starting with ``#`` are comments.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path

from .config import CONFIG_DIR

FILENAME = "vocabulary.txt"


@dataclass(frozen=True)
class Vocabulary:
    terms: tuple[str, ...] = ()
    corrections: tuple[tuple[str, str], ...] = ()

    def correct(self, text: str) -> str:
        # A single pass, so rules cannot chain into each other and change the meaning.
        mapping = {wrong.casefold(): right for wrong, right in self.corrections}
        if not mapping:
            return text
        alternatives = "|".join(re.escape(s) for s in sorted(mapping, key=len, reverse=True))
        return re.sub(r"(?<!\w)(?:" + alternatives + r")(?!\w)",
                      lambda m: mapping[m.group().casefold()], text, flags=re.I)

    def glossary(self) -> str:
        return "\n".join([*self.terms, *(f"{a} => {b}" for a, b in self.corrections)])


def parse_vocabulary(text: str) -> Vocabulary:
    terms, corrections = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" in line:
            wrong, right = (s.strip() for s in line.split("=>", 1))
            if not wrong or not right:
                continue
            corrections.append((wrong, right))
            line = right
        if line.casefold() not in {term.casefold() for term in terms}:
            terms.append(line)
    return Vocabulary(tuple(terms), tuple(corrections))


def load_vocabulary(path: Path = CONFIG_DIR / FILENAME) -> Vocabulary:
    try:
        return parse_vocabulary(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Vocabulary()

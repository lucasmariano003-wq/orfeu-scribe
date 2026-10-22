"""Guard against foreign text in the revision.

With short speech, or speech cut by the recognizer, a fast model can mistake
the supporting material for content and return something else: earlier
dictations sent as style references, or a list of vocabulary terms. The guard
only looks at what the revision ADDED and that came from that support;
reorganizing, shortening and fixing one or two names from the glossary stay free.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .vocabulary import Vocabulary

# Below this, speech is too short to need style examples.
SHORT_WORDS = 12
SHINGLE = 6
EXAMPLE_SHINGLES = 2
VOCAB_TERMS = 3


@dataclass(frozen=True)
class Verdict:
    leaked: bool
    reason: str = ""


def words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFD", text.lower())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return [token for token in re.split(r"[^a-z0-9]+", folded) if token]


def is_short(raw: str) -> bool:
    return len(words(raw)) < SHORT_WORDS


def _shingles(tokens: list[str]) -> set[str]:
    if len(tokens) < SHINGLE:
        return set()
    return {" ".join(tokens[i:i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1)}


def _heard(term: list[str], raw: list[str], raw_joined: str) -> bool:
    """The term is in the transcript, even if misspelled by the recognizer (4-letter prefix)."""
    if f" {' '.join(term)} " in raw_joined:
        return True
    return all(any(token.startswith(word[:4]) or (len(token) >= 4 and word.startswith(token[:4]))
                   for token in raw) for word in term)


def entries(vocab: Vocabulary) -> list[tuple[str, tuple[str, ...]]]:
    """(right spelling, known wrong spellings), one entry per term."""
    result = []
    for term in vocab.terms:
        wrongs = tuple(wrong for wrong, right in vocab.corrections if right.casefold() == term.casefold())
        result.append((term, wrongs))
    return result


def vocabulary_heard(raw: str, vocab: Vocabulary) -> Vocabulary:
    """Only the terms the speaker actually said (right, wrong or similar spelling)."""
    tokens = words(raw)
    joined = f" {' '.join(tokens)} "
    kept = set()
    for right, wrongs in entries(vocab):
        right_words = words(right)
        if right_words and _heard(right_words, tokens, joined):
            kept.add(right.casefold())
            continue
        for wrong in wrongs:
            wrong_words = words(wrong)
            if wrong_words and f" {' '.join(wrong_words)} " in joined:
                kept.add(right.casefold())
                break
    return Vocabulary(tuple(term for term in vocab.terms if term.casefold() in kept),
                      tuple(pair for pair in vocab.corrections if pair[1].casefold() in kept))


def check(raw: str, revised: str, examples=(), vocab: Vocabulary | None = None) -> Verdict:
    r, v = words(raw), words(revised)
    # 1. Six-word sequences the revision brought in that only exist in the support.
    added = _shingles(v) - _shingles(r)
    sources = [text for text in examples if text]
    if added and sources:
        from_examples = set().union(*(_shingles(words(text)) for text in sources))
        if sum(1 for item in added if item in from_examples) >= EXAMPLE_SHINGLES:
            return Verdict(True, "history example")
    # 2. A flood of vocabulary: three or more terms the speaker did not say.
    if vocab is not None:
        raw_joined = f" {' '.join(r)} "
        revised_joined = f" {' '.join(v)} "
        heard = {term.casefold() for term in vocabulary_heard(raw, vocab).terms}
        novel = set()
        for right, _wrongs in entries(vocab):
            if right.casefold() in heard:
                continue
            term = words(right)
            if term and f" {' '.join(term)} " in revised_joined and not _heard(term, r, raw_joined):
                novel.add(" ".join(term))
        if len(novel) >= VOCAB_TERMS:
            return Verdict(True, "vocabulary")
    # 3. Safety net: short speech that comes back inflated.
    if len(r) < SHORT_WORDS and len(v) > len(r) * 2 + 6:
        return Verdict(True, "short speech inflated")
    return Verdict(False)

"""Style references and suggested spellings; nothing reaches the vocabulary without consent."""
from difflib import SequenceMatcher
import re
import unicodedata

MAX_EXAMPLES = 5
MAX_EXAMPLE_CHARS = 2500
WORD = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


def bounded_examples(examples):
    """Keeps priority order and whole texts, within the character budget."""
    result, used, seen = [], 0, set()
    for value in examples:
        text = value.strip()
        if not text or text in seen or len(text) > MAX_EXAMPLE_CHARS - used:
            continue
        result.append(text)
        seen.add(text)
        used += len(text)
        if len(result) == MAX_EXAMPLES:
            break
    return tuple(result)


def pair_key(wrong, right):
    # Spellings that differ only in case (abc => ABC) are still valid suggestions.
    return wrong.casefold(), right if wrong.casefold() == right.casefold() else right.casefold()


def extract_swaps(original, edited):
    """Swaps of 1–4 words; punctuation, insertions and removals are not terms."""
    original, edited = (unicodedata.normalize('NFC', text) for text in (original, edited))
    before, after = list(WORD.finditer(original)), list(WORD.finditer(edited))
    matcher = SequenceMatcher(None, [m.group().casefold() for m in before],
                              [m.group().casefold() for m in after], autojunk=False)
    pairs = {}

    def add(i, j, k, l):
        if not (1 <= j - i <= 4 and 1 <= l - k <= 4):
            return
        # Never crosses sentences or inner punctuation to fabricate an expression.
        for text, tokens, start, end in ((original, before, i, j), (edited, after, k, l)):
            if any(text[tokens[n].end():tokens[n + 1].start()].strip()
                   for n in range(start, end - 1)):
                return
        wrong = ' '.join(m.group() for m in before[i:j])
        right = ' '.join(m.group() for m in after[k:l])
        if wrong == right or max(len(wrong), len(right)) > 120:
            return
        pairs[pair_key(wrong, right)] = (wrong, right)

    for tag, i, j, k, l in matcher.get_opcodes():
        if tag == 'replace':
            add(i, j, k, l)
        elif tag == 'equal':
            for a, b in zip(range(i, j), range(k, l)):
                left, right = before[a].group(), after[b].group()
                prefix_a = original[:before[a].start()].rstrip(' \t\r\"“”«»—–(')
                prefix_b = edited[:after[b].start()].rstrip(' \t\r\"“”«»—–(')
                initial = any(not p or p[-1] in '.!?\n' for p in (prefix_a, prefix_b))
                if initial and left[1:] == right[1:]:
                    continue
                add(a, a + 1, b, b + 1)
    return tuple(pairs.values())

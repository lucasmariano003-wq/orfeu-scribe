"""Context through AT-SPI: the text before the cursor and the visible conversation.

On Wayland the extension cannot read the text field. AT-SPI (GNOME's
accessibility bus) can, when the application exports it: GTK, LibreOffice,
Firefox and Chromium with accessibility on. The read runs in a separate process
with a short deadline, in parallel with listening; without AT-SPI, dictation
works as before.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from .config import ROOT

CONVERSATION_LIMIT = 3000
BEFORE = 200
AFTER = 40
TIMEOUT = 2.5
SENTENCE_END = ".!?…"
OPENERS = "([{\"“‘'/-—«¿¡"
# Mid-sentence, only common words lose their capital: a proper name ("Peter",
# "Acme Labs") never loses it by mistake. Languages without a list keep capitals.
COMMON = {
    "en": frozenset("""a an the and or but nor so yet if then than that this these those there here it its
i you he she we they me him her us them my your his our their mine yours what which who whom whose when
where why how not no yes also just only even still already now today tomorrow yesterday maybe always never
very really quite more most less some any all each every both either neither other another such same much
many few lot of in on at by for to from with without about into onto over under after before between
during since until as like is are was were be been being have has had do does did will would can could
should may might must shall i'm it's that's let's think guess said""".split()),
    "pt": frozenset("""a o as os um uma uns umas e é ou mas nem que se porque pois porém como quando onde
quanto quem qual quais cujo cuja de do da dos das em no na nos nas num numa por pelo pela pelos pelas para pra
pro com sem sob sobre até após ante entre contra desde ao aos à às eu tu ele ela nós vós eles elas você vocês
me te lhe nos vos lhes meu minha meus minhas teu tua seu sua seus suas nosso nossa nossos nossas isso isto
aquilo esse essa este esta aquele aquela esses essas estes estas aqueles aquelas não sim também já ainda
só apenas muito muita muitos muitas pouco mais menos bem mal então depois antes agora hoje ontem amanhã
logo talvez sempre nunca aqui ali lá cá assim tudo nada algo alguém ninguém todo toda todos todas cada
outro outra outros outras mesmo mesma tão tanto tanta inclusive principalmente porque enquanto embora
caso conforme segundo tipo foi era é são está estão estava vai vou vamos tem têm tinha há houve ser estar
ter fazer fiz fez acho creio parece disse falei""".split()),
    "es": frozenset("""a al el la los las un una unos unas y e o u pero ni que si porque pues como cuando donde
quien cual cuales de del en con sin sobre entre hasta desde para por según yo tú él ella nosotros ellos
ellas usted ustedes me te le nos les mi mis tu tus su sus nuestro nuestra esto eso aquello este esta ese
esa no sí también ya todavía solo muy mucho mucha muchos muchas poco más menos bien mal entonces después
antes ahora hoy ayer mañana luego quizás siempre nunca aquí allí así todo nada algo alguien nadie cada
otro otra mismo misma tan tanto es son está están era fue va voy vamos tiene hay ser estar creo dije""".split()),
}


def read(pid, want="caret", timeout=TIMEOUT):
    """{'before','after'} of the focused field or {'conversation'}; None without AT-SPI."""
    if not pid or want not in ("caret", "conversation"):
        return None
    try:
        result = subprocess.run(["/usr/bin/python3", "-m", "orfeuscribe.context"],
                                input=json.dumps({"pid": pid, "want": want}), capture_output=True, text=True,
                                timeout=timeout, cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        value = json.loads(result.stdout) if result.returncode == 0 else None
        return value if isinstance(value, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _walk(node, limit=4000):
    queue, count = [node], 0
    while queue and count < limit:
        current = queue.pop(0)
        count += 1
        yield current
        try:
            total = min(current.get_child_count(), 300)
        except Exception:  # noqa: BLE001 — an application that disappears mid-read
            continue
        for index in range(total):
            try:
                child = current.get_child_at_index(index)
            except Exception:  # noqa: BLE001
                child = None
            if child is not None:
                queue.append(child)


def inspect(pid, want):
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    Atspi.set_timeout(150, 400)
    desktop = Atspi.get_desktop(0)
    apps = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
    apps = [app for app in apps if app is not None and app.get_process_id() == pid]
    if not apps:
        return None
    if want == "caret":
        for app in apps:
            for node in _walk(app):
                states = node.get_state_set()
                if states.contains(Atspi.StateType.FOCUSED) and states.contains(Atspi.StateType.EDITABLE):
                    text = node.get_text_iface()
                    if not text:
                        continue
                    caret = Atspi.Text.get_caret_offset(text)
                    count = Atspi.Text.get_character_count(text)
                    if caret < 0:
                        return None
                    return {"before": Atspi.Text.get_text(text, max(0, caret - BEFORE), caret),
                            "after": Atspi.Text.get_text(text, caret, min(count, caret + AFTER))}
        return None
    # Visible conversation: the text of the shown parts of the active window, minus the field being typed in.
    for app in apps:
        frames = [app.get_child_at_index(i) for i in range(app.get_child_count())]
        frames = [f for f in frames if f is not None and f.get_state_set().contains(Atspi.StateType.ACTIVE)] or frames
        for frame in frames[:1]:
            pieces = []
            for node in _walk(frame):
                states = node.get_state_set()
                if not states.contains(Atspi.StateType.SHOWING):
                    continue
                if states.contains(Atspi.StateType.FOCUSED) and states.contains(Atspi.StateType.EDITABLE):
                    continue
                text = node.get_text_iface()
                if not text:
                    continue
                if node.get_child_count() and node.get_role() not in (Atspi.Role.LABEL, Atspi.Role.PARAGRAPH,
                                                                     Atspi.Role.TEXT, Atspi.Role.STATIC):
                    continue
                value = Atspi.Text.get_text(text, 0, min(Atspi.Text.get_character_count(text), 4000)).strip()
                if value and (not pieces or pieces[-1] != value):
                    pieces.append(value)
            joined = "\n".join(pieces)
            return {"conversation": joined[-CONVERSATION_LIMIT:]}
    return None


def fit_to_cursor(text, before, after="", terms=(), language="en"):
    """Spacing and capitals to fit the dictation in the middle of a sentence.

    ``before``/``after`` are the text around the cursor (AT-SPI). Without them nothing changes.
    """
    if before is None or not text:
        return text
    common_words = COMMON.get(language, frozenset())
    stripped = before.rstrip(" \t ")
    last = stripped[-1:] if stripped else ""
    mid_sentence = bool(last) and last not in SENTENCE_END and last != "\n"
    first_word = re.match(r"[^\W\d_][\w'-]*", text)
    if mid_sentence and first_word:
        word = first_word[0]
        # Common word: in the language's list, or written in lowercase later in the same text.
        common = word.casefold() in common_words or re.search(r"(?<![\w])" + re.escape(word.casefold()) + r"\b",
                                                             text[len(word):])
        proper = any(word == term.split()[0] for term in terms if term) or (word.isupper() and len(word) > 1)
        if word == "I" and language == "en":
            proper = True
        if common and not proper and word[0].isupper() and (len(word) == 1 or word[1:].islower()):
            text = word[0].lower() + text[1:]
    elif not last or last in SENTENCE_END or last == "\n":
        text = text[:1].upper() + text[1:]
    char = before[-1:]
    if char and not char.isspace() and char not in OPENERS and text[0] not in ",.;:!?)]}…»":
        text = " " + text
    following = (after or "")[:1]
    if following and (following.isalnum() or following in "([“\"«"):
        if mid_sentence and following.islower() and re.search(r"[^.]\.$", text):
            text = text[:-1]
        if not text.endswith((" ", "\n")):
            text += " "
    return text


if __name__ == "__main__":
    try:
        args = json.load(sys.stdin)
        print(json.dumps(inspect(int(args["pid"]), args.get("want", "caret"))))
    except Exception:  # noqa: BLE001 — no accessibility: dictation continues as before
        print("null")

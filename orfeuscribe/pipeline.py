"""Transcription, commands, revision and timings, with no GTK dependency."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import re
import time

from .commands import (capitalize_line_starts, render_structure, spoken_commands, structural_commands,
                       structure_phrases)
from .i18n import _
from .text import LINE, PARAGRAPH


class Cancelled(Exception):
    pass


class VoiceCancelled(Cancelled):
    """The spoken cancel command at the end: the audio goes to rescue, with no offer in the pill."""


LOCAL_CLEANUP = "local cleanup"


def capitalize_sentences(text):
    return re.sub(r"(^|[.!?]\s+|\n+)([^\W\d_])", lambda m: m[1] + m[2].upper(), text)


def literal_local(raw, mode, vocab, languages=("en",), notes=False):
    """The raw speech with local cleanup only: commands, vocabulary and capitals.

    No model takes part. This is what the guard delivers when the revision
    leaks, what "Revert to transcript" pastes, and what every dictation gets
    when no revision service is configured.
    """
    from .languages import language_prefix
    from .routing import NOTE_FOLDER, route
    _hint, content = language_prefix(raw)
    content = vocab.correct(content)
    if notes and NOTE_FOLDER.match(content):
        content = content[NOTE_FOLDER.match(content).end():]
    content = route(content, notes=notes)[1]
    content, structure = structural_commands(content, mode, languages)
    commands = spoken_commands(content, languages)
    text = render_structure(commands.text if not commands.cancelled else content, structure)
    text = capitalize_sentences(text.strip())
    return capitalize_line_starts(vocab.correct(text) + commands.suffix)


def guarded_revision(provider, marked, mode, context, vocab, examples, spoken, restore):
    """Revision with a guard against foreign text.

    Speech under 12 words goes without style examples. If the revision brings in
    text from the support (history examples, the end of the previous part, the
    visible conversation) or a flood of vocabulary that was not said, it is redone
    once without examples, without the previous part and with only the vocabulary
    that was heard; if it still leaks or fails, the speech is delivered with local
    cleanup. Returns (text, note for the model field of the history).
    """
    from . import guard
    from .providers import ProviderError

    def polish(examples, context, vocab):
        return restore(provider.polish(marked, mode, context, vocab, examples=examples) if examples
                       else provider.polish(marked, mode, context, vocab))

    if mode == "literal":
        return polish((), context, vocab), ""
    sent = () if guard.is_short(spoken) else tuple(examples)
    # The visible conversation (Reply with context) is watched support, like the examples.
    support = (*sent, context.get("previous_tail", ""), context.get("visible_conversation", ""))
    first = polish(sent, context, vocab)
    verdict = guard.check(spoken, first, support, vocab)
    if not verdict.leaked:
        return first, ""
    bare = {key: value for key, value in context.items() if key not in ("previous_tail", "visible_conversation")}
    try:
        second = polish((), bare, guard.vocabulary_heard(spoken, vocab))
    except ProviderError:
        second = None
    if second is not None and not guard.check(spoken, second, support, vocab).leaked:
        return second, f"guard: {verdict.reason} leaked; redone without support"
    return capitalize_sentences(spoken), f"{LOCAL_CLEANUP} (guard: {verdict.reason} leaked in the revision)"


@dataclass
class Result:
    raw: str
    final: str
    mode: str
    provider: str
    model: str
    recording: float
    stt: float
    polishing: float
    total: float
    error: str = ""
    action: str = "deliver"
    language: str = ""
    web: str = ""
    input_language: str = "en"
    note_folder: str = ""

    def as_dict(self):
        return asdict(self)


def run_pipeline(provider, audio, mode, context, vocab, recording=0.0,
                 state=lambda value: None, cancelled=lambda: False, raw=None, *,
                 history=None, learn_from_history=False, understand_intent=True, exclude_history_id=None,
                 operation="dictation", language="", selection="", editable=False, input_language="en",
                 default_language=None, allow_destinations=True, allow_voice_cancel=True, conversation="",
                 notes=False):
    from .languages import language_prefix
    from .providers import ProviderError
    from .routing import NOTE_FOLDER, route, selection_action
    context = dict(context)
    context['understand_intent'] = understand_intent
    default_language = default_language or input_language
    with_context = operation == "reply" and bool(conversation.strip())
    if operation == "reply":
        # Reply with context is a dictation; the conversation only guides the revision.
        operation, allow_destinations = "dictation", False
        if with_context:
            context["visible_conversation"] = conversation[-3000:]
    start = time.monotonic()
    stt = 0.0
    if cancelled():
        raise Cancelled()
    if raw is None:
        state("transcribing")
        command_terms = structure_phrases(mode, (input_language, default_language))
        raw = provider.transcribe(audio, input_language, (*vocab.terms, *command_terms))
        stt = time.monotonic() - start
    if cancelled():
        raise Cancelled()
    hinted, content = language_prefix(raw)
    if hinted and hinted != input_language and audio:
        # The prefix is only known after the first pass. Transcribe again with the
        # chosen language so the recognizer gets it too, not just the revision.
        input_language = hinted
        second = provider.transcribe(audio, input_language, vocab.terms)
        _hint, content = language_prefix(second)
        raw = second
        stt = time.monotonic() - start
    elif hinted:
        input_language = hinted
    if cancelled():
        raise Cancelled()
    languages = (input_language, default_language)
    context['input_language'] = input_language
    routed = vocab.correct(content)
    notes = notes and allow_destinations
    folder_match = NOTE_FOLDER.match(routed) if notes else None
    note_folder = folder_match[1].strip() if folder_match else ''
    if operation in ("edit", "write"):
        # Edit/Write: the speech is the instruction; there are no voice destinations.
        note_folder = ''
        destination, content = ("edit" if selection.strip() else "write"), routed
        instruction = routed.strip()
    else:
        destination, content = route(routed, operation, notes) if allow_destinations else (operation, routed)
    content, structure = structural_commands(content, mode, languages)
    commands = spoken_commands(content, languages)
    if commands.cancelled:
        if allow_voice_cancel:
            raise VoiceCancelled()
        from .commands import SpokenText
        commands = SpokenText(content)
    state("polishing")
    polishing_start = time.monotonic()
    action, web = "deliver", ""
    guard_note = ""
    result_mode = mode
    revise = getattr(provider, "can_revise", True)

    def finish(final, **extra):
        return Result(raw, final, result_mode, provider.name, getattr(provider, "last_model", provider.model),
                      recording, stt, time.monotonic() - polishing_start, recording + time.monotonic() - start,
                      input_language=input_language, **extra)
    try:
        if destination == "ask":
            result_mode = "question"
            if selection:
                reply = provider.assist(content, selection, context, vocab)
                final = vocab.correct(reply['text'])
                action = selection_action(reply, editable)
            else:
                final, web = provider.ask(content, context, vocab)
                action = "card"
            if cancelled():
                raise Cancelled()
            return finish(final, action=action, web=web)
        if destination in ("edit", "write"):
            result_mode = destination
            if destination == "edit":
                final, action = provider.edit(instruction, selection, context, vocab), "replace"
            else:
                final = provider.write(instruction, context, vocab)
            if cancelled():
                raise Cancelled()
            return finish(vocab.correct(final).strip(), action=action)
        if destination == "note":
            mode, result_mode, action = "dictation", "note", "note"
        marked = commands.text.rstrip().replace("\n\n", PARAGRAPH).replace("\n", LINE)
        examples = (history.style_examples(mode, language=input_language, exclude_id=exclude_history_id)
                    if history is not None and learn_from_history and mode != "literal" and revise else ())
        markers = {PARAGRAPH: "\n\n", LINE: "\n", **structure}
        expected = re.findall("|".join(map(re.escape, markers)), marked)

        def restore(final):
            if expected:
                found = re.findall("|".join(map(re.escape, markers)), final)
                if found == expected:
                    for marker, newline in markers.items():
                        final = re.sub(r"\s*" + re.escape(marker) + r"\s*", newline, final)
                elif not structure and not found and len(re.findall(r"\n+", final.strip())) == len(expected):
                    # Some models already turn the markers into breaks: restore the dictated amount.
                    breaks = iter(markers[item] for item in expected)
                    final = re.sub(r"[ \t]*\n+[ \t]*", lambda _: next(breaks), final.strip())
                else:
                    raise ProviderError(_("The model did not keep a formatting command. "
                                          "Your transcript is in the history."))
            return final

        if destination == "translation":
            result_mode = "translation"
            final = restore(provider.translate(marked, mode, context, vocab, language))
        elif not revise:
            # No revision service: the local cleanup is the result.
            final = render_structure(commands.text, structure)
            final = capitalize_sentences(final.strip())
            guard_note = LOCAL_CLEANUP
        else:
            final, guard_note = guarded_revision(provider, marked, mode, context, vocab, examples,
                                                 render_structure(commands.text, structure), restore)
        final = capitalize_line_starts(vocab.correct(final).strip() + commands.suffix)
        error = ""
    except ProviderError as exc:
        final, error = "", str(exc)
    if cancelled():
        raise Cancelled()
    model = getattr(provider, "last_model", provider.model)
    if guard_note == LOCAL_CLEANUP:
        transcriber = getattr(provider, "transcriber", None)
        model = f"{getattr(transcriber, 'model', '') or model} + {LOCAL_CLEANUP}"
    elif guard_note.startswith(LOCAL_CLEANUP):
        model = guard_note
    elif guard_note:
        model = f"{model} ({guard_note})"
    if with_context:
        # Only the mark goes to the history; the conversation is never stored.
        model = f"{model} (with context)"
    return Result(raw, final, result_mode, provider.name, model, recording, stt,
                  time.monotonic() - polishing_start, recording + time.monotonic() - start, error, action,
                  language if destination == "translation" else "", web, input_language, note_folder)

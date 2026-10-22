"""Service contracts: speech-to-text and text revision are separate pieces.

``Transcriber`` turns audio into text. ``Reviser`` edits text with a language
model. ``Service`` joins one of each (the reviser is optional) and is what the
pipeline talks to. Adapters live in their own modules; today the only real one
is ``openai_compat`` (any server that implements the OpenAI-style
``/audio/transcriptions`` and ``/chat/completions`` endpoints).
"""
from __future__ import annotations

from typing import Protocol

from ..i18n import _
from ..vocabulary import Vocabulary


class ProviderError(Exception):
    """A message safe for the interface and the history; never includes raw HTTP bodies or keys.

    ``status`` keeps only the HTTP status code (or None) so callers can tell a
    definitive refusal from a passing network problem.
    """

    def __init__(self, message="", status=None):
        super().__init__(message)
        self.status = status


class Transcriber(Protocol):
    label: str
    model: str

    def transcribe(self, audio: bytes, lang: str, keyterms: tuple[str, ...]) -> str: ...


class Reviser(Protocol):
    model: str

    def polish(self, raw: str, mode: str, context: dict, vocab: Vocabulary, examples=()) -> str: ...
    def translate(self, raw: str, mode: str, context: dict, vocab: Vocabulary, language: str) -> str: ...
    def edit(self, command: str, selection: str, context: dict, vocab: Vocabulary) -> str: ...
    def write(self, command: str, context: dict, vocab: Vocabulary) -> str: ...
    def assist(self, command: str, selection: str, context: dict, vocab: Vocabulary) -> dict: ...
    def ask(self, question: str, context: dict, vocab: Vocabulary) -> tuple[str, str]: ...


class Unconfigured:
    """Stands in until the user configures a speech service; never sends anything."""
    label = "—"
    model = ""

    def transcribe(self, audio, lang, keyterms):
        raise ProviderError(_("No speech service is configured. Open Settings → Service."))


def _needs_revision(*_args, **_kwargs):
    raise ProviderError(_("This needs a text revision service. Turn it on in Settings → Service."))


class Service:
    """What the pipeline calls: one transcriber and, optionally, one reviser."""

    def __init__(self, transcriber, reviser=None):
        self.transcriber = transcriber
        self.reviser = reviser
        self.name = getattr(transcriber, "label", None) or getattr(transcriber, "name", "")
        self.model = reviser.model if reviser else transcriber.model
        self.last_model = self.model

    @property
    def can_revise(self) -> bool:
        return self.reviser is not None

    @property
    def configured(self) -> bool:
        return not isinstance(self.transcriber, Unconfigured)

    def transcribe(self, audio, lang, keyterms=()):
        return self.transcriber.transcribe(audio, lang, keyterms)

    def _revise(self, method, *args):
        if self.reviser is None:
            _needs_revision()
        try:
            return getattr(self.reviser, method)(*args)
        finally:
            self.last_model = getattr(self.reviser, "last_model", self.reviser.model)

    def polish(self, raw, mode, context, vocab, examples=()):
        return self._revise("polish", raw, mode, context, vocab, examples)

    def translate(self, raw, mode, context, vocab, language):
        return self._revise("translate", raw, mode, context, vocab, language)

    def edit(self, command, selection, context, vocab):
        return self._revise("edit", command, selection, context, vocab)

    def write(self, command, context, vocab):
        return self._revise("write", command, context, vocab)

    def assist(self, command, selection, context, vocab):
        return self._revise("assist", command, selection, context, vocab)

    def ask(self, question, context, vocab):
        return self._revise("ask", question, context, vocab)


def build(config, store=None, prompt_observer=None) -> Service:
    """A Service from the settings. Keys are looked up at request time, never stored here."""
    from ..credentials import api_key, default_store
    from .openai_compat import OpenAIReviser, OpenAITranscriber
    store = store if store is not None else default_store()
    if not config.stt_configured:
        return Service(Unconfigured())
    transcriber = OpenAITranscriber(config["stt_base_url"], config["stt_model"],
                                    key=lambda: api_key(store, "stt"), hints=config["stt_hints"])
    reviser = None
    if config.revision_configured:
        same = config["revision_same_server"]
        base = config["stt_base_url"] if same else config["revision_base_url"]
        slot = "stt" if same else "revision"
        reviser = OpenAIReviser(base, config["revision_model"], key=lambda: api_key(store, slot),
                                prompt_observer=prompt_observer)
    return Service(transcriber, reviser)

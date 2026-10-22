"""OpenAI-compatible HTTP adapters: speech-to-text and chat-based revision.

Endpoints, checked against the OpenAI documentation in October 2026:

* ``POST {base}/audio/transcriptions`` — multipart form with ``file`` and
  ``model``. ``gpt-transcribe`` takes ``languages[]`` and ``keywords[]``;
  older models and most compatible servers take a single ``language`` and a
  free-text ``prompt``. Never send both language fields. Uploads are limited to
  25 MB; Orfeu Scribe sends 16 kHz mono WAV of at most 10 minutes (~19 MB).
* ``POST {base}/chat/completions`` — ``model`` and ``messages``. Orfeu Scribe
  sends neither ``max_tokens`` nor ``temperature``: both are rejected by some
  newer models and servers, and the defaults are fine for editing.
* ``GET {base}/models`` — used only when the user asks for the list.

Compatibility with other servers is by contract, not by test: only a local
fake server has been exercised (tests/fake_openai.py).
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from urllib import error, parse, request

from .. import __version__
from ..config import valid_endpoint
from ..i18n import _
from ..text import (ASSIST_SCHEMA, assist_messages, ask_messages, build_messages, operation_messages,
                    plain_text, translation_messages)
from . import ProviderError

USER_AGENT = f"Orfeu Scribe/{__version__}"
MAX_UPLOAD = 25 * 1024 * 1024
MAX_KEYWORDS = 100
MAX_PROMPT_CHARS = 600  # whisper-1 accepts about 224 tokens of prompt


class NoRedirect(request.HTTPRedirectHandler):
    """A redirect could carry the Authorization header to another host: refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _messages_for(status):
    return {
        400: _("The service rejected the request (HTTP 400). Check the model name."),
        401: _("The service rejected the API key. Check it in Settings → Service."),
        403: _("The service refused this request. Check the account's permissions."),
        404: _("Endpoint or model not found. Check the server URL and the model name."),
        413: _("The audio is too large for the service."),
        429: _("Rate limit or quota reached. Wait a moment and try again."),
    }.get(status) or _("The service returned an error (HTTP {status}). Try again.").format(status=status)


class HttpClient:
    def __init__(self, base_url, key=lambda: None, timeout=120):
        if not valid_endpoint(base_url):
            raise ProviderError(_("Use an https:// server URL (http:// is accepted only for this computer)."))
        self.base = base_url.strip().rstrip("/")
        self.host = parse.urlsplit(self.base).hostname or ""
        self._key = key
        self._timeout = timeout
        # A local endpoint must remain local even when the desktop environment
        # defines a proxy and forgets to exclude loopback in no_proxy.
        handlers = [NoRedirect]
        if self.host in ("127.0.0.1", "localhost", "::1"):
            handlers.insert(0, request.ProxyHandler({}))
        self._opener = request.build_opener(*handlers)

    def _headers(self, content_type, accept="application/json"):
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            key = self._key()
        except Exception as exc:  # noqa: BLE001 — keyring errors carry their own safe message
            raise ProviderError(str(exc) or _("The API key could not be read.")) from None
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers

    def request(self, path, data=None, content_type="application/json"):
        req = request.Request(self.base + path, data=data, headers=self._headers(content_type if data else None),
                              method="POST" if data is not None else "GET")
        try:
            with self._opener.open(req, timeout=self._timeout) as response:
                result = json.loads(response.read(8 * 1024 * 1024))
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            # Never include the body: it may echo the request, including the dictation.
            raise ProviderError(_messages_for(status), status) from None
        except (error.URLError, TimeoutError, OSError):
            raise ProviderError(_("Could not reach the service. Check the connection and the server URL.")) from None
        except (ValueError, UnicodeError):
            raise ProviderError(_("The service returned an invalid response.")) from None

    def list_models(self):
        result = self.request("/models")
        models = sorted(item["id"] for item in result.get("data", [])
                        if isinstance(item, dict) and isinstance(item.get("id"), str))
        if not models:
            raise ProviderError(_("The service did not list any models."))
        return models


def multipart(fields, file_field, filename, content_type, payload):
    boundary = "orfeuscribe-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields:
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                       f"{value}\r\n").encode())
    # The file goes last in the form.
    chunks.extend([(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                    f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n").encode(), payload,
                   f"\r\n--{boundary}--\r\n".encode()])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def hint_style(hints, model):
    if hints != "auto":
        return hints
    return "keywords" if model.lower().startswith("gpt-transcribe") else "prompt"


def clean_terms(keyterms):
    """Keywords must be single-line and may not contain < or >."""
    terms = []
    for term in keyterms:
        term = " ".join(str(term).split())
        if term and len(term) <= 50 and not any(c in term for c in "<>") and term not in terms:
            terms.append(term)
    return terms[:MAX_KEYWORDS]


class OpenAITranscriber:
    def __init__(self, base_url, model, key=lambda: None, hints="auto", timeout=120):
        self.client = HttpClient(base_url, key, timeout)
        self.model = model.strip()
        self.hints = hints
        self.label = self.client.host

    def form_fields(self, lang, keyterms):
        fields = [("model", self.model), ("response_format", "json")]
        style = hint_style(self.hints, self.model)
        terms = clean_terms(keyterms)
        if style == "keywords":
            if lang:
                fields.append(("languages[]", lang))
            fields.extend(("keywords[]", term) for term in terms)
        elif style == "prompt":
            if lang:
                fields.append(("language", lang))
            kept = []
            for term in terms:
                if len("Vocabulary: " + ", ".join([*kept, term]) + ".") > MAX_PROMPT_CHARS:
                    break
                kept.append(term)
            if kept:
                fields.append(("prompt", "Vocabulary: " + ", ".join(kept) + "."))
        return fields

    def transcribe(self, audio, lang="en", keyterms=()):
        if not audio:
            raise ProviderError(_("The audio is empty."))
        if len(audio) > MAX_UPLOAD:
            raise ProviderError(_("The audio is too large for the service."))
        body, content_type = multipart(self.form_fields(lang, keyterms), "file", "dictation.wav", "audio/wav", audio)
        result = self.client.request("/audio/transcriptions", body, content_type)
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError(_("No speech was found in this audio. Check the microphone and try again."))
        return text.strip()

    def list_models(self):
        return self.client.list_models()


class OpenAIReviser:
    def __init__(self, base_url, model, key=lambda: None, timeout=120, prompt_observer=None):
        self.client = HttpClient(base_url, key, timeout)
        self.model = model.strip()
        self.last_model = self.model
        self._prompt_observer = prompt_observer
        self._schema_supported = None  # unknown until the first structured request
        self._lock = threading.Lock()

    def list_models(self):
        return self.client.list_models()

    def _completion(self, messages, schema=None):
        payload = {"model": self.model, "messages": messages}
        if schema and self._schema_supported is not False:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "orfeuscribe_reply", "strict": True, "schema": schema}}
        if self._prompt_observer:
            # Messages only: never headers, address or key.
            self._prompt_observer(messages)
        try:
            result = self.client.request("/chat/completions", json.dumps(payload, ensure_ascii=False).encode())
        except ProviderError as exc:
            if "response_format" in payload and exc.status == 400:
                # Many compatible servers do not implement structured output: ask again without it.
                with self._lock:
                    self._schema_supported = False
                return self._completion(messages)
            raise
        if "response_format" in payload:
            self._schema_supported = True
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ProviderError(_("The model reached its length limit. Your transcript was kept."))
            text = choice["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError()
            return text.strip()
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError(_("The model returned no text. Your transcript was kept.")) from None

    def polish(self, raw, mode, context, vocab, examples=()):
        text = self._completion(build_messages(raw, mode, context, vocab, examples))
        if mode == "literal" and re.findall(r"\w+", text.casefold()) != re.findall(r"\w+", raw.casefold()):
            # Literal fidelity does not depend on the model obeying the instruction.
            return raw.strip()
        return text

    def translate(self, raw, mode, context, vocab, language):
        return self._completion(translation_messages(raw, mode, context, vocab, language))

    def edit(self, command, selection, context, vocab):
        return self._completion(operation_messages("edit", command, context, vocab, selection))

    def write(self, command, context, vocab):
        return plain_text(self._completion(operation_messages("write", command, context, vocab)))

    def assist(self, command, selection, context, vocab):
        value = self._completion(assist_messages(command, selection, context, vocab), ASSIST_SCHEMA)
        match = re.search(r"\{.*\}", value, re.S)
        try:
            reply = json.loads(match[0] if match else value)
            if not isinstance(reply, dict) or not isinstance(reply.get("text"), str) or not reply["text"].strip():
                raise ValueError()
            return reply
        except (ValueError, TypeError):
            raise ProviderError(_("The answer did not come in a safe format. The selection was kept.")) from None

    def ask(self, question, context, vocab):
        return self._completion(ask_messages(question, context, vocab)), ""

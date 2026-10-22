# Speech and revision services

Orfeu Scribe separates two contracts (`orfeuscribe/providers/__init__.py`):

- **Transcriber** — `transcribe(audio, language, keyterms) -> str`
- **Reviser** — `polish`, `translate`, `edit`, `write`, `assist`, `ask`

A `Service` joins one transcriber and an optional reviser. Without a reviser,
dictation still works with local cleanup (spoken commands, vocabulary,
capitalization); translation, Ask and Edit need a reviser.

## OpenAI-compatible adapter (available)

`orfeuscribe/providers/openai_compat.py`. Checked against OpenAI's public
documentation on 2026-10-21:

- Speech-to-text guide: <https://developers.openai.com/api/docs/guides/speech-to-text>
- Transcription reference: <https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create>

### Transcription — `POST {base}/audio/transcriptions`

Multipart form: `model`, `response_format=json`, then hints, then `file`
(`dictation.wav`, 16 kHz mono PCM). The documentation lists a 25 MB upload
limit; Orfeu Scribe records at most 10 minutes (about 19 MB) and splits files
into 5-minute parts.

| Hint style | Fields sent | When |
| --- | --- | --- |
| keywords | `languages[]`, `keywords[]` (≤ 100 terms, single-line, no `<` `>`) | Automatic for models starting with `gpt-transcribe` |
| prompt | `language`, `prompt` ("Vocabulary: a, b." ≤ 600 characters) | Automatic for other models (Whisper-style servers) |
| none | only `model`, `response_format`, `file` | Servers that reject hints |

The guide says `gpt-transcribe` replaces `language` with `languages` and that
both must not be sent; Orfeu Scribe never sends both. The same guide lists
`whisper-1`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe` and
`gpt-4o-transcribe-diarize` as deprecated, shutting down on 2027-02-26.

### Revision — `POST {base}/chat/completions`

Payload: `model` and `messages` only. `max_tokens` and `temperature` are not
sent, because newer reasoning models reject them and editing works with the
defaults. For Ask-about-selection, Orfeu Scribe first asks for structured output
(`response_format: json_schema`); if the server answers HTTP 400, it retries
without it for the rest of the session and parses the JSON from the text.

### Models list — `GET {base}/models`

Used only when you press *List models*.

### Safety

- `https://` required; `http://` only for `127.0.0.1`, `localhost`, `::1`.
  URLs with user info, query or fragment are rejected.
- HTTP redirects are refused, so the `Authorization` header never reaches
  another host.
- Error messages are fixed, translatable texts plus the HTTP status. Response
  bodies are never shown or stored (they may echo the dictation).
- The key is read from the environment or the keyring at request time and is
  never written to settings, history or logs.

### Verified

Only against `tests/fake_openai.py`, a local fake server. **No real provider
has been called**; compatibility with OpenAI or any other server is by
contract, not by test.

## Not available (possible future adapters)

| Feature | Status |
| --- | --- |
| Streaming transcription (live partial text over WebSocket) | Not implemented. `live.py` keeps a streaming path for providers that offer `open_stream`; with the OpenAI-compatible adapter, live text uses periodic transcription of new audio. |
| Read-back (text-to-speech) | Removed. OpenAI's guide lists `tts-1`, `tts-1-hd` and `gpt-4o-mini-tts` on `/v1/audio/speech` as shutting down on 2027-01-06, with no replacement on that endpoint. |
| Web search in Ask | Removed; answers come from the model only and never claim a search. |
| xAI (Grok) speech API with an API key | Not implemented. The previous app used xAI through a personal OAuth session, which Orfeu Scribe does not support. |
| Local engines without HTTP (whisper.cpp library, Vosk) | Not implemented; run them behind an OpenAI-compatible local server instead. |

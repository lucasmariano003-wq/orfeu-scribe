# Architecture

Orfeu Scribe has two processes: a **GApplication service** in Python (records,
calls the services, keeps the history, shows the window) and a **GNOME Shell
extension** in GJS (shortcuts, the on-screen pill, pasting). They talk over the
session D-Bus.

```
 GNOME Shell extension                     Orfeu Scribe service (Python, GTK 4)
 ─────────────────────                     ──────────────────────────────────
 shortcut ──SetContext/SetRequest/Start──▶  Recorder (GStreamer, PCM in memory)
 pill ◀──StateChanged/Level/Partial───────        │ stop
                                                   ▼
                                            pipeline.run_pipeline
                                              ├─ Transcriber ─▶ /audio/transcriptions
                                              ├─ commands, vocabulary, routing
                                              ├─ Reviser ─────▶ /chat/completions
                                              └─ guard (redo or local cleanup)
                                                   │
 paste via virtual keyboard ◀──Revertible/Deliver──┤──▶ History (SQLite) / Rescue (24 h)
 answer card ◀──Card / Replace─────────────────────┘
```

## Names

All identifiers live in `orfeuscribe/ids.py`:

| What | Value |
| --- | --- |
| Application ID | `io.github.lucasmariano003_wq.OrfeuScribe` |
| D-Bus service | `io.github.lucasmariano003_wq.OrfeuScribe.Service` at `/io/github/lucasmariano003_wq/OrfeuScribe/Service` |
| Extension UUID | `orfeuscribe@lucasmariano003-wq.github.io` |
| GSettings schema | `org.gnome.shell.extensions.orfeuscribe` (shared by app and extension) |
| Config / data | `~/.config/orfeuscribe/`, `~/.local/share/orfeuscribe/` |
| Keyring schema | `io.github.lucasmariano003_wq.OrfeuScribe.ApiKey` (attribute `slot` = `stt` or `revision`) |

## Python modules

| Module | Role |
| --- | --- |
| `service.py` | Single-instance app, D-Bus interface, recording state machine, delivery, rescue, parts |
| `window.py` | libadwaita window: Dictation, History, Vocabulary, Settings |
| `pipeline.py` | Transcribe → commands → route → revise (guarded) → result; no GTK |
| `providers/__init__.py` | `Transcriber`/`Reviser` contracts, `Service`, `build()` from settings |
| `providers/openai_compat.py` | HTTP client (no redirects, no response bodies in errors), adapters |
| `credentials.py` | Keyring (libsecret) or environment variables; memory store for tests |
| `text.py` | Mode choice and the messages sent for revision (English instructions) |
| `commands.py` | Spoken commands and structure markers per language |
| `guard.py`, `learning.py` | Foreign-text guard; style examples and spelling suggestions |
| `history.py`, `rescue.py`, `exchange.py` | SQLite history with migrations; 24 h audio rescue; export/import |
| `audio.py`, `sounds.py`, `media.py`, `live.py` | Recording, microphone checks, cues, files in parts, live text |
| `context.py`, `selection.py` | AT-SPI readers run in short-lived subprocesses |
| `keywatch.py`, `gestures.py`, `watcher.py` | Optional physical-gesture watcher (separate process, closed vocabulary) |
| `config.py`, `settings.py`, `modes.py`, `languages.py`, `i18n.py` | Settings file, GSettings, modes, languages, gettext |

## Flow of one dictation

1. The extension records the focused window and, for Edit/Ask, the PRIMARY
   selection; it calls `SetContext`, `SetRequest`, `Start`.
2. The service refuses to start if no speech service is configured. Otherwise
   it chooses the mode (fixed or by app rules) and language, starts the
   recorder and, for dictation, reads the text around the cursor through
   AT-SPI in parallel.
3. On `Stop`, `_work` runs the pipeline in a thread. A newer generation
   (cancel, new dictation) discards late results.
4. The pipeline transcribes, applies the language prefix, vocabulary
   corrections, voice routing and spoken commands, marks line breaks with
   `⟦VD_LINE⟧`/`⟦VD_PARAGRAPH⟧`, and revises. If the revision copies support
   text, it is redone without support; if it still leaks, local cleanup is
   delivered. Missing markers refuse delivery.
5. The result goes to the history; the service emits `Revertible` (raw
   transcript) and `Deliver`. The extension pastes only if the same window is
   still focused, then restores the clipboard.
6. On any failure or cancel, the audio goes to the rescue folder for 24 hours.

## Isolation in tests

`scripts/run_tests.py` gives every run a temporary HOME/XDG tree, in-memory
GSettings (`GSETTINGS_BACKEND=memory`) and keyring
(`ORFEUSCRIBE_SECRET_BACKEND=memory`), a compiled schema in a temporary
directory (`ORFEUSCRIBE_SCHEMA_DIR`), no display and an unreachable D-Bus
address. Service tests replace the D-Bus connection, settings and recorder
with doubles from `tests/support.py`.

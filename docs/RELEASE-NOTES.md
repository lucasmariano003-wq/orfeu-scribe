# 0.1.0a1 — first public alpha

First public Orfeu Scribe preview for Linux with GNOME Shell 51.

Included: configurable OpenAI-compatible transcription and optional text
revision, per-user credentials, recovery/history, vocabulary, English and
Brazilian Portuguese UI, and GNOME text delivery.

Validation includes synthetic recording, a local fake provider, isolated
installation/removal, UI captures and delivery through a nested GNOME Shell.
This does not establish real-world speech accuracy or support for every app.

Local HTTP providers bypass environment proxies. The installer refuses to
replace unowned or modified standalone files and honors custom XDG locations
when no explicit --home override is supplied. It preserves data by default.

Known limits: Linux/GNOME 51 only; no streaming provider integration or speech
read-back; real microphone, real provider, accessibility and physical shortcut
coverage still need broader testing. Windows and macOS are future work.

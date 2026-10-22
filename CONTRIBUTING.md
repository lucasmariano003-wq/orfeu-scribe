# Contributing

Orfeu Scribe is an early alpha and is licensed under GPL-3.0-or-later
([LICENSE](LICENSE), [NOTICE](NOTICE)). Contributions and reproducible bug reports are welcome.
Contributed material must be compatible with that license, and existing
attribution and license notices must be preserved. No separate contributor
agreement is currently in place. These notes describe how the project is worked on.

## Ground rules

- **Synthetic data only.** Tests, fixtures, screenshots and examples never use
  real recordings, real histories, personal names, keys or account settings.
- **Nothing runs against a real service in tests.** Use `tests/fake_openai.py`.
- **Isolation.** Tests must not touch the real HOME, keyring, GSettings, D-Bus
  session or microphone. `scripts/run_tests.py` enforces this and fails if
  Orfeu Scribe files appear in the real HOME.
- **Keys and responses stay out of messages.** A `ProviderError` carries a
  short, translatable message and an HTTP status, never a response body.
- **Opt-in for anything that sends more data** (history examples, visible
  conversation, live text) or needs broad permissions (key watcher).

## Running checks

```sh
scripts/run_tests.py                         # full suite, isolated
scripts/run_tests.py tests.test_pipeline     # one module
scripts/smoke_window.py --language pt_BR     # window construction, never shown
scripts/prove_ui.sh                          # screenshots in a private Xvfb
gjs -m scripts/check_extension.js extension/orfeuscribe@lucasmariano003-wq.github.io/extension.js
glib-compile-schemas --strict --dry-run extension/orfeuscribe@lucasmariano003-wq.github.io/schemas
scripts/audit.py
```

## Code style

- Python: standard library and PyGObject only; no new runtime dependencies
  without discussion. Follow the surrounding code; comments explain *why*.
- User-visible strings go through `_()` (or `N_()` for tables translated
  later, `ngettext()` for plurals). Use named placeholders: `{count}`.
- Extension: `_()` from the extension module; `%s` placeholders filled with
  the local `fill()` helper.

## Translations

```sh
scripts/extract_strings.py          # update po/orfeuscribe.pot and merge into po/*.po
scripts/extract_strings.py --check  # every string translated, placeholders preserved
```

Edit `po/<language>.po` with any PO editor. Catalogs are compiled by the
installer and by `scripts/compile_translations.py`; GNU gettext tools are not
required.

## Commits

Small, focused commits with an imperative summary. Do not commit compiled
schemas, compiled catalogs, `.work/`, recordings, databases or keys (see
`.gitignore`).

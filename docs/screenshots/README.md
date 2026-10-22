# Interface screenshots

Twenty screenshots of the real GTK/libadwaita window were generated during
independent review on 2026-10-22, using only synthetic settings and history.
They cover English and Brazilian Portuguese, light and dark modes, all four
pages, the listening state and expanded history.

```sh
bash scripts/prove_ui.sh
```

The proof starts a private Xvfb display and D-Bus session with temporary HOME
and XDG directories. The child uses an in-memory keyring and settings; audio
and provider data are synthetic. The loop exits after all frames. These
images demonstrate layout and translation, not real microphone quality,
service accuracy or GNOME Shell keyboard delivery.

The English error text shown in the Portuguese history fixture is stored
synthetic historical data, not a new error produced by the localized app.

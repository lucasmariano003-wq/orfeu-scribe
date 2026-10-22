"""Every name that identifies Orfeu Scribe on the desktop, in one place.

Orfeu Scribe was selected by the owner on 2026-10-22. The application namespace
uses the owner's existing GitHub handle, with its hyphen normalized to an
underscore for D-Bus. No new GitHub organization or domain is implied.
See docs/ARCHITECTURE.md. None of these identifiers may collide with the private
Calamus app this project was derived from.
"""

APP_NAME = "Orfeu Scribe"
SLUG = "orfeuscribe"
APP_ID = "io.github.lucasmariano003_wq.OrfeuScribe"
BUS_NAME = APP_ID + ".Service"
BUS_PATH = "/io/github/lucasmariano003_wq/OrfeuScribe/Service"
EXTENSION_UUID = "orfeuscribe@lucasmariano003-wq.github.io"
SCHEMA_ID = "org.gnome.shell.extensions.orfeuscribe"
GETTEXT_DOMAIN = "orfeuscribe"
SECRET_SCHEMA = APP_ID + ".ApiKey"

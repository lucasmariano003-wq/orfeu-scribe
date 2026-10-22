#!/bin/bash
# Installs Orfeu Scribe for the current user. See scripts/install.py --help.
# Examples:
#   ./install.sh --dry-run                 show what would be written
#   ./install.sh --home /tmp/orfeuscribe    install into a throwaway home
#   ./install.sh --enable-extension        install and enable the GNOME Shell extension
set -euo pipefail
umask 077
exec /usr/bin/python3 "$(dirname -- "${BASH_SOURCE[0]}")/scripts/install.py" install "$@"

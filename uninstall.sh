#!/bin/bash
# Removes what install.sh installed. Settings, history and vocabulary are kept unless --purge.
# Examples:
#   ./uninstall.sh --dry-run
#   ./uninstall.sh --purge
set -euo pipefail
exec /usr/bin/python3 "$(dirname -- "${BASH_SOURCE[0]}")/scripts/install.py" uninstall "$@"

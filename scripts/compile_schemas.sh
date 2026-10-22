#!/bin/bash
# Compiles the GSettings schema next to the extension (the compiled file is not committed).
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
glib-compile-schemas --strict "$root/extension/orfeuscribe@lucasmariano003-wq.github.io/schemas"
echo "Schema compiled: extension/orfeuscribe@lucasmariano003-wq.github.io/schemas/gschemas.compiled"

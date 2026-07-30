#!/bin/bash
set -e
cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
exec "$PYTHON_BIN" bfxps_ai/bfxps_plain_language_selftest.py "$@"

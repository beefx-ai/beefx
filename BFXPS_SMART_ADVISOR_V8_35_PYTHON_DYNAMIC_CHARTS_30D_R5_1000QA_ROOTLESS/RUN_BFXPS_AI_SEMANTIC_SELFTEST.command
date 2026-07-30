#!/bin/bash
set -e
cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
export PYTHONPATH="$PWD/bfxps_ai${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON_BIN" bfxps_ai/bfxps_semantic_selftest.py

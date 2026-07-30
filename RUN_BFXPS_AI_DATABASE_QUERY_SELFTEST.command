#!/bin/sh
cd "$(dirname "$0")"
PYTHONPATH="$PWD/bfxps_ai" python3 bfxps_ai/bfxps_database_query_selftest.py

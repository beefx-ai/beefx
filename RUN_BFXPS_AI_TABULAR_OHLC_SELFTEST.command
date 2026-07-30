#!/bin/bash
set -e
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/bfxps_ai"
python3 bfxps_ai/bfxps_tabular_ohlc_selftest.py

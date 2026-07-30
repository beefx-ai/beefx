#!/bin/sh
cd "$(dirname "$0")"
python3 bfxps_ai/backtest_bridge_long_to_short.py --root .

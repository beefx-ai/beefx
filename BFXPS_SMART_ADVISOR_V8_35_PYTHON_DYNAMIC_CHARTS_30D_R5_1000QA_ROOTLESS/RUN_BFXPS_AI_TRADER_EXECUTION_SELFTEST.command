#!/bin/bash
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/bfxps_ai"
python3 bfxps_ai/bfxps_trader_execution_conversation_selftest.py

#!/bin/bash
cd "$(dirname "$0")"
PYTHONPATH=bfxps_ai python3 bfxps_ai/bfxps_conversation_truth_selftest.py

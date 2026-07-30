#!/bin/bash
set -e
cd "$(dirname "$0")"
PORT=8765
EXPECTED_VERSION="V8.35"
URL="http://127.0.0.1:${PORT}/?v=8.35"
mkdir -p outputs runtime_logs runtime_logs/.matplotlib
export MPLCONFIGDIR="$PWD/runtime_logs/.matplotlib"
if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti tcp:${PORT} || true)
  [ -z "$PIDS" ] || kill -9 $PIDS || true
fi
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
"$PY" -c "import pandas, matplotlib" >/dev/null 2>&1 || { echo "Missing pandas/matplotlib. Run: $PY -m pip install pandas matplotlib"; exit 1; }
"$PY" bfxps_ai/bfxps_server.py --port "$PORT" > runtime_logs/BFXPS_AI_WEB_SERVER.log 2>&1 &
READY=0
for i in $(seq 1 60); do
  VERSION=$(curl -fsS "http://127.0.0.1:${PORT}/health" 2>/dev/null | "$PY" -c "import sys,json; print(json.load(sys.stdin).get('version',''))" 2>/dev/null || true)
  if [ "$VERSION" = "$EXPECTED_VERSION" ]; then READY=1; break; fi
  sleep 1
done
if [ "$READY" != "1" ]; then echo "ERROR: $EXPECTED_VERSION strict-rootless server did not start. See runtime_logs/BFXPS_AI_WEB_SERVER.log"; exit 1; fi
open "$URL" 2>/dev/null || xdg-open "$URL" 2>/dev/null || true
echo "BFXPS Smart Advisor $EXPECTED_VERSION+++ strict rootless is running at $URL"

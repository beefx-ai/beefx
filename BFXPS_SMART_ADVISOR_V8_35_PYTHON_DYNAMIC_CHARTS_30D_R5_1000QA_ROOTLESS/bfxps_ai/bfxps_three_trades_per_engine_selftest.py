from pathlib import Path
from bfxps_smart_advisor import SmartAdvisor

ROOT = Path(__file__).resolve().parents[1]
MEM = ROOT / "bfxps_ai/runtime/three_trades_selftest_memory.json"
if MEM.exists(): MEM.unlink()
a = SmartAdvisor(
    ROOT / "outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv",
    None,
    ROOT / "bfxps_ai/config/backtested_warning_catalog.json",
    MEM,
    ROOT / "bfxps_ai/config/advisor_policy.json",
)
r = a.ask("3 trades mỗi engine", session_id="three-trades-selftest")
t = r.text
required = [
    "SIMCARRRY6 (3 trades)", "29/07/2026 t+2", "28/07/2026 t+1", "27/07/2026 t+1",
    "AllDaysLadder_CAP0.3 (3 trades)", "12K_AllDay (3 trades)",
    "FUTURE 28/07/2026", "HISTORY 27/07/2026", "HISTORY 24/07/2026",
]
missing = [x for x in required if x not in t]
assert r.intent == "TOP_ENGINES", (r.intent, t)
assert not missing, (missing, t)
assert len([x for x in t.splitlines() if x.startswith("- ")]) == 9, t
print("THREE_TRADES_PER_ENGINE_SELFTEST PASS")
print(t)

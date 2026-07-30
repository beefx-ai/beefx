from __future__ import annotations
from pathlib import Path
import tempfile
from bfxps_smart_advisor import SmartAdvisor

ROOT=Path(__file__).resolve().parents[1]
TRADES=ROOT/'outputs'/'BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv'
CASES={
    'history':['Ba kèo lịch sử gần nhất','27/07/2026','SIMCARRRY6 t+1','AllDaysLadder_CAP0.3','12K_AllDay'],
    'lịch sử':['Ba kèo lịch sử gần nhất','HIT_TARGET','R5 CANCEL'],
    '3 kèo gần nhất':['1.','2.','3.'],
    'date':['Ngày kèo forward mới nhất trong database: 29/07/2026','SHORT 1.844,3','TAKE_HALF_IF_FILL'],
    'KH: date':['29/07/2026','TAKE_HALF_IF_FILL'],
    'list top engines':['Top engines hiện hành','FUTURE 29/07/2026 t+2','AllDaysLadder_CAP0.3','12K_AllDay'],
    'các engine hiện hành':['29/07/2026','28/07/2026','SHORT 1.844,3'],
}
with tempfile.TemporaryDirectory() as td:
    advisor=SmartAdvisor(TRADES,memory_path=Path(td)/'memory.json')
    # Seed noisy OHLC/trade memory; database navigation must still win.
    advisor.ask('O 1807,8 H 1812 L 1804 P 1809, giờ làm gì?',session_id='qa')
    for q,required in CASES.items():
        text=advisor.ask(q,session_id='qa').text
        missing=[x for x in required if x not in text]
        if missing:
            raise AssertionError(f'{q!r} missing {missing}:\n{text}')
print('DATABASE_QUERY_SELFTEST PASS')

from pathlib import Path
import tempfile
from bfxps_smart_advisor import SmartAdvisor
ROOT=Path(__file__).resolve().parents[1]
TRADES=ROOT/'outputs'/'BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv'
CASES={
'Kèo hôm nay là gì?':['28/07/2026','3 row','AllDaysLadder_CAP0.3','12K_AllDay','SIMCARRRY6 t+1'],
'ngày mai có kèo gì?':['29/07/2026','SIMCARRRY6 t+2','1.844,3','1.835,0'],
'kèo forward xa nhất là ngày nào?':['29/07/2026','TAKE_HALF_IF_FILL'],
'3 trades mỗi engine':['SIMCARRRY6 (3 trades)','AllDaysLadder_CAP0.3 (3 trades)','12K_AllDay (3 trades)'],
'3 kèo lịch sử của 3 engine top':['Ba kèo lịch sử gần nhất','27/07/2026','HIT_TARGET','R5 CANCEL'],
'Biên dự kiến của từng engine và từng horizon là gì?':['SIMCARRRY6 t+1','SIMCARRRY6 t+2','AllDaysLadder_CAP0.3','12K_AllDay'],
'biên SIMCARRRY6 t+2':['29/07/2026','1.835,0 – 1.844,3'],
'biên engine5 hôm nay':['AllDaysLadder_CAP0.3','12K_AllDay','1.835,0 – 1.844,3'],
'Có bao nhiêu forward rows?':['4 row FORWARD','28/07/2026: 3','29/07/2026: 1'],
'database đang đọc file nào?':['BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv','4 row FORWARD','29/07/2026'],
}
with tempfile.TemporaryDirectory() as td:
 a=SmartAdvisor(TRADES,memory_path=Path(td)/'m.json')
 a.ask('O 1807,8 H 1812 L 1804 P 1809, giờ làm gì?',session_id='qa')
 for q,required in CASES.items():
  text=a.ask(q,session_id='qa').text
  missing=[x for x in required if x not in text]
  if missing: raise AssertionError(f'{q}: missing {missing}\n{text}')
 fill=a.ask('Với OHLC hiện tại, kèo nào fill, kèo nào WAIT_ENTRY, target nào đi qua trước entry?',session_id='qa').text
 for x in ['không engine nào','KHÔNG FILL','23,0 điểm','32,3 điểm','không được tính là kèo thắng']:
  if x not in fill: raise AssertionError(fill)
 reclaim=a.ask('Giá 1815 thì có được long reclaim không?',session_id='qa').text
 for x in ['1.812,1','có thể LONG scalp','SL 1.811,1','TP 1.835,0']:
  if x not in reclaim: raise AssertionError(reclaim)
print('MASS_DATABASE_QA_SELFTEST PASS')

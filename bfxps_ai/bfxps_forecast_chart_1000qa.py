from __future__ import annotations
from pathlib import Path
import json, itertools, re, sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor
from dynamic_charts import build_charts_for_question

ROOT=Path(__file__).resolve().parents[1]
paths=resolve_runtime_paths()
advisor=SmartAdvisor(paths.trades, paths.ohlc if paths.ohlc.exists() else None, paths.history_trades, paths.warning_catalog, ROOT/'outputs/.qa_v833_memory.json', paths.policy)
advisor.trades_file=paths.trades
last3=paths.trades.parent/'BEST_ENGINE_CHART_LAST3_TRADES.tsv'
outdir=paths.trades.parent/'dynamic_charts'

trade_stems=[
 'đặt toàn bộ kèo lên chart forecast', 'vẽ chart kèo hiện hành', 'chart forecast từng engine', 'đưa kèo hôm nay lên chart',
 'chart kèo ngày mai', 'vẽ đồ thị entry target', 'biểu đồ biên forecast', 'chart có center expected low expected high',
 'vẽ nến OHLC zoom cùng kèo', 'đặt các horizon lên chart', 'chart mức chung giữa engines', 'chart forecast để nhìn trading ngay',
 'vẽ chart không tư vấn lan man', 'chart từng engine và từng horizon', 'chart kèo theo đúng database',
]
perf_stems=['chart hiệu suất','biểu đồ PnL 30 trades','đồ thị equity recent 30','chart WR và PnL','chart hiệu suất 7 trades','chart performance không nói kèo']
combined=['chart kèo và hiệu suất','vẽ OHLC forecast cùng chart PnL','chart toàn bộ kèo cộng equity 30 trades','biểu đồ forecast và performance']
qualifiers=['ghi rõ nguồn','không bịa','chỉ dùng ba database','ưu tiên số liệu base','tách engine','tách horizon','show center low high','mọi kèo phải nhìn thấy','không dùng app ảnh','vẽ bằng Python']
ohlcs=[(1808,1816,1804,1815),(1822,1852,1814.2,1850),(1832,1845,1828,1837),(1846,1855,1834,1841),(1810,1836,1805,1835),(1835,1844.3,1811.1,1825),(1844.4,1851,1835,1848),(1811.1,1820,1808,1812.2),(1850,1859,1840,1844),(1828,1846,1820,1844.3)]
questions=[]
# 600 trade, 200 perf, 200 combined = 1000
for i in range(600):
 s=trade_stems[i%len(trade_stems)]; q=qualifiers[(i//len(trade_stems))%len(qualifiers)]; o,h,l,p=ohlcs[i%len(ohlcs)]
 questions.append(f'O {o} H {h} L {l} P {p}, {s}; {q}.')
for i in range(200):
 s=perf_stems[i%len(perf_stems)]; q=qualifiers[(i//len(perf_stems))%len(qualifiers)]
 questions.append(f'{s}; {q}.')
for i in range(200):
 s=combined[i%len(combined)]; q=qualifiers[(i//len(combined))%len(qualifiers)]; o,h,l,p=ohlcs[i%len(ohlcs)]
 questions.append(f'O {o} H {h} L {l} P {p}, {s}; {q}.')

results=[]; passed=0; chart_cache={}
for i,q in enumerate(questions,1):
 m={k:float(v.replace(',','.')) for k,v in re.findall(r'\b([OHLP])\s*([0-9]+(?:[.,][0-9]+)?)',q,re.I)}
 kwargs={'session_open':m.get('O'),'session_high':m.get('H'),'session_low':m.get('L'),'live_price':m.get('P')}
 rep=advisor.ask(q,session_id='qa_shared',**kwargs)
 ql=q.lower(); expect_perf=any(x in ql for x in ['hiệu suất','pnl','equity','performance','wr'])
 expect_trade_hint=any(x in ql for x in ['kèo','forecast','entry','target','ohlc','biên','center','expected low','expected high']) and 'không nói kèo' not in ql and not (expect_perf and not any(x in ql for x in ['kèo và','forecast cùng','forecast và','toàn bộ kèo','show center','mọi kèo']))
 cache_key=(expect_perf,expect_trade_hint,kwargs.get('session_open'),kwargs.get('session_high'),kwargs.get('session_low'),kwargs.get('live_price'))
 if cache_key not in chart_cache:
  chart_cache[cache_key]=build_charts_for_question(q,paths.trades,paths.history_trades,outdir,last3_tsv=last3,ohlc_tsv=paths.ohlc if paths.ohlc.exists() else None,**kwargs)
 charts=chart_cache[cache_key]
 expect_trade=any(x in ql for x in ['kèo','forecast','entry','target','ohlc','biên','center','expected low','expected high']) and 'không nói kèo' not in ql and not (('hiệu suất' in ql or 'pnl' in ql or 'equity' in ql or 'performance' in ql or 'wr' in ql) and not any(x in ql for x in ['kèo và','forecast cùng','forecast và','toàn bộ kèo','show center','mọi kèo']))
 kinds={c['kind'] for c in charts}
 reasons=[]
 if expect_trade and 'forecast_ohlc_zoom' not in kinds: reasons.append('missing forecast chart')
 if expect_perf and 'performance' not in kinds: reasons.append('missing performance chart')
 if 'forecast_ohlc_zoom' in kinds:
  c=next(x for x in charts if x['kind']=='forecast_ohlc_zoom')
  if len(c.get('bands',[]))!=4: reasons.append(f"bands={len(c.get('bands',[]))}, expected 4")
  for b in c.get('bands',[]):
   if not (b['expected_low']<=b['center']<=b['expected_high']): reasons.append('invalid L/C/H order')
   if b['entry'] is None or b['target'] is None: reasons.append('missing entry/target')
  if not all(x in c.get('source','') for x in ['PLUS_FORWARD','LAST3_TRADES','RECENT_TRADES']): reasons.append('source not 3 DB')
 if expect_trade:
  low=rep.text.lower()
  if any(x in low for x in ['chưa short.', 'không short và không bình quân', 'failed breakout', 'retest không lấy lại']): reasons.append('execution advice leaked into chart-only answer')
  if 'expected low/center/expected high' not in low and 'expected low' not in low: reasons.append('answer missing forecast band explanation')
 if 'nguồn database' not in rep.text.lower(): reasons.append('missing database disclosure')
 ok=not reasons
 passed += int(ok)
 results.append({'id':i,'question':q,'intent':rep.structured.get('intent'),'answer':rep.text,'charts':[{'kind':c['kind'],'file':c['file'],'source':c['source'],'bands':c.get('bands')} for c in charts],'pass':ok,'reasons':reasons})

summary={'total':len(results),'passed':passed,'failed':len(results)-passed,'forward_rows_expected':4,'databases':[paths.trades.name,last3.name,paths.history_trades.name]}
(ROOT/'BFXPS_AI_V8_33_FORECAST_CHART_1000QA_RESULTS.json').write_text(json.dumps({'summary':summary,'results':results},ensure_ascii=False,indent=2,default=str),encoding='utf-8')
with (ROOT/'BFXPS_AI_V8_33_FORECAST_CHART_1000_QUESTIONS_AND_ANSWERS.md').open('w',encoding='utf-8') as f:
 f.write('# BFXPS AI V8.33 — 1000 câu hỏi forecast chart\n\n')
 f.write(f"- Tổng: {summary['total']}\n- PASS: {summary['passed']}\n- FAIL: {summary['failed']}\n- Database: {', '.join(summary['databases'])}\n\n")
 for r in results:
  f.write(f"## {r['id']:04d} — {'PASS' if r['pass'] else 'FAIL'}\n\n")
  f.write(f"**Câu hỏi:** {r['question']}\n\n**Intent:** `{r['intent']}`\n\n**Trả lời thực tế:**\n\n{r['answer']}\n\n")
  f.write('**Charts:**\n')
  for c in r['charts']: f.write(f"- `{c['kind']}` — `{c['file']}` — nguồn `{c['source']}`\n")
  if r['reasons']: f.write('\n**Lỗi:** '+ '; '.join(r['reasons'])+'\n')
  f.write('\n')
print(json.dumps(summary,ensure_ascii=False))

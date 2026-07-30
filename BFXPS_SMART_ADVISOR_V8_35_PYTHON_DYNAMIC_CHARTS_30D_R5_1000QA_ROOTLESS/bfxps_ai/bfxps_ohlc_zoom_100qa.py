from __future__ import annotations
from pathlib import Path
import json
from bfxps_smart_advisor import SmartAdvisor
from dynamic_charts import build_charts_for_question

ROOT=Path(__file__).resolve().parents[1]
FWD=ROOT/'outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv'
HIST=ROOT/'outputs/BEST_ENGINE_RECENT_TRADES.tsv'
OUT=ROOT/'outputs/dynamic_charts'
MEM=ROOT/'outputs/.qa_v830_memory.json'
if MEM.exists(): MEM.unlink()
advisor=SmartAdvisor(FWD, history_trades_path=HIST, memory_path=MEM)

bases=[
 'O 1822 H 1852 L 1814,2 P 1850, đặt kèo lên chart',
 'O 1822 H 1852 L 1814,2 P 1850, vẽ chart nến OHLC zoom và đặt kèo hiện hành',
 'chart kèo với O 1822 H 1852 L 1814,2 P 1850',
 'đưa các entry target lên chart nến O 1822 H 1852 L 1814,2 P 1850',
 'kèo trên đồ thị OHLC O 1822 H 1852 L 1814,2 P 1850',
 'biểu đồ nến zoom kèm kèo hôm nay O 1822 H 1852 L 1814,2 P 1850',
 'vẽ nến phiên này và các mức chung engine O 1822 H 1852 L 1814,2 P 1850 chart',
 'chart biên thực tế và entry target O 1822 H 1852 L 1814,2 P 1850',
 'chỉ đặt kèo lên chart, không tư vấn vào lệnh O 1822 H 1852 L 1814,2 P 1850',
 'cho tôi chart OHLC zoom dễ nhìn, không bịa rule O 1822 H 1852 L 1814,2 P 1850',
 'chart hiệu suất 30 trades gần nhất',
 'biểu đồ hiệu suất recent 30',
 'chart kèo hiện hành và chart hiệu suất 30 trades',
 'đồ thị kèo ngày mai kèm nến O 1822 H 1852 L 1814,2 P 1850',
 'chart các mức chung giữa engines với OHLC O 1822 H 1852 L 1814,2 P 1850',
 'vẽ chart kèo nhưng chưa cần kết luận fill O 1822 H 1852 L 1814,2 P 1850',
 'chart OHLC gap up O 1850 H 1855 L 1842 P 1853 và đặt kèo',
 'chart OHLC gap down O 1805 H 1812 L 1798 P 1808 và đặt kèo',
 'chart nến giá đang gãy lại O 1846 H 1852 L 1832 P 1834 và các kèo',
 'chart nến giá vượt vùng O 1830 H 1850 L 1828 P 1848 và kèo hệ',
]
questions=[]
for i in range(5):
    for b in bases:
        suffix=['',' nhé',' cho dễ nhìn',' theo đúng database',' ghi rõ nguồn'][i]
        questions.append(b+suffix)

bad_terms=['retest không lấy lại','kèo short bị vô hiệu','đang vi phạm đúng điều kiện kích hoạt','kèo hệ đang xét:','chưa short.']
results=[]
for idx,q in enumerate(questions,1):
    reply=advisor.ask(q,session_id=f'qa{idx}')
    charts=build_charts_for_question(q,FWD,HIST,OUT)
    is_perf='hiệu suất' in q.lower() or 'recent 30' in q.lower()
    wants_trade=any(x in q.lower() for x in ['kèo','entry','target','ohlc','nến','mức chung'])
    kinds={c['kind'] for c in charts}
    checks={
      'has_chart': bool(charts),
      'source_ok': all(c['source'] in {FWD.name,HIST.name} for c in charts),
      'trade_chart_ok': (not wants_trade) or ('forward_levels' in kinds),
      'performance_chart_ok': (not is_perf) or ('performance' in kinds),
      'no_invented_rule': not any(x in reply.text.lower() for x in bad_terms),
      'chart_first_text': (not wants_trade) or ('đã đặt' in reply.text.lower() or 'chart' in reply.text.lower()),
    }
    passed=all(checks.values())
    results.append({'id':idx,'question':q,'intent':reply.intent,'answer':reply.text,'charts':charts,'checks':checks,'pass':passed})

summary={'total':len(results),'pass':sum(r['pass'] for r in results),'fail':sum(not r['pass'] for r in results)}
(ROOT/'BFXPS_AI_V8_30_OHLC_ZOOM_100QA_RESULTS.json').write_text(json.dumps({'summary':summary,'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
md=['# BFXPS AI V8.30 - OHLC Zoom Chart 100QA','',f"- Total: {summary['total']}",f"- PASS: {summary['pass']}",f"- FAIL: {summary['fail']}",'','## Full questions and actual answers','']
for r in results:
    md += [f"### {r['id']:03d} - {'PASS' if r['pass'] else 'FAIL'}",'',f"**Question:** {r['question']}",'',f"**Intent:** `{r['intent']}`",'',f"**Charts:** `{', '.join(c['kind'] for c in r['charts'])}`",'',f"**Answer:**",'',r['answer'],'',f"**Checks:** `{json.dumps(r['checks'],ensure_ascii=False)}`",'']
(ROOT/'BFXPS_AI_V8_30_OHLC_ZOOM_100_QUESTIONS_AND_ANSWERS.md').write_text('\n'.join(md),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False))
raise SystemExit(0 if summary['fail']==0 else 1)

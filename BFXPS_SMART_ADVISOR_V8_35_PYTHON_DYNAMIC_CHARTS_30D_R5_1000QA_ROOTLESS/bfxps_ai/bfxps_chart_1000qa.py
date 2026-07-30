from __future__ import annotations
import json
from pathlib import Path
from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor
from dynamic_charts import build_charts_for_question, wants_chart

TEMPLATES = [
"Chart kèo hiện hành của từng engine và horizon",
"Vẽ chart entry target các kèo hôm nay",
"Cho tôi biểu đồ các mức chung giữa engines",
"Đồ thị biên dự kiến từng engine t+1 t+2",
"Chart kèo ngày mai ghi rõ entry target",
"Chart OHLC hiện tại O 1808 H 1816 L 1804 P 1815 và kèo đang chờ",
"Biểu đồ xem kèo nào đã fill và wait entry với O 1808 H 1840 L 1804 P 1836",
"Chart gap down O 1800 H 1810 L 1795 P 1805 so với các kèo",
"Chart gap up O 1846 H 1850 L 1842 P 1848 và ladder",
"Đồ thị vượt giả entry rồi gãy lại quanh biên",
"Chart hiệu suất 7 phiên gần đây từng engine",
"Biểu đồ PnL 10 phiên gần nhất",
"Chart performance 20 ngày của các engines",
"Đồ thị 30 trades gần nhất theo engine",
"Chart equity recent trades và nêu nguồn database",
"Chart lời lỗ nếu đánh thuận hệ gần đây",
"Biểu đồ hiệu suất và kèo hiện hành cùng lúc",
"Chart kèo hiện tại cộng hiệu suất 30 trades",
"Cho chart minh họa tư vấn đã kiểm định và kèo forward",
"Chart tổng hợp engine, horizon, entry, target, PnL",
]
SUFFIXES = [
"", " nhé", " cho tôi xem", " theo đúng base", " không được bịa", " ghi rõ nguồn",
" dùng database mới nhất", " tách từng engine", " đúng ngày", " có giá live 1824",
" và cho biết dữ liệu thiếu gì", " nếu thiếu liên hệ beefx.com", " trả lời ngắn", " phân tích kỹ",
" ưu tiên mức chung", " không cộng trùng profile", " chỉ lấy outputs", " live-safe", " minh bạch",
" kiểm tra lại số liệu", " trong phiên", " sau gap", " trước ATC", " theo horizon", " theo ngày",
" cho khách hàng", " dùng tiếng Việt", " kèm chú thích nguồn", " không nội suy", " hiện file nguồn",
" và R5", " và reclaim", " và size", " và ladder", " và biên thực tế", " và biên dự kiến",
" với O 1807.8 H 1812 L 1804 P 1809", " với O 1845 H 1850 L 1834 P 1836",
" với giá 1835", " với giá 1844.3", " cho 7 trades", " cho 10 trades", " cho 20 trades",
" cho 30 trades", " so với đánh ngược", " theo từng profile", " có legend", " dễ hiểu",
" chuẩn trader", " tuyệt đối trung thực"
]

def main():
    root=Path(__file__).resolve().parents[1]
    paths=resolve_runtime_paths(root=root)
    mem=root/'bfxps_ai/runtime/chart_1000qa_memory.json'
    if mem.exists(): mem.unlink()
    adv=SmartAdvisor(paths.trades, paths.ohlc if paths.ohlc.exists() else None, paths.history_trades, paths.warning_catalog, mem, paths.policy)
    outdir=paths.trades.parent/'dynamic_charts_qa'
    outdir.mkdir(exist_ok=True)
    results=[]
    n=0
    for ti,t in enumerate(TEMPLATES):
        for si,sfx in enumerate(SUFFIXES):
            n+=1
            q=t+sfx
            reply=adv.ask(q, session_id=f'chartqa-{n}')
            live=1824.0 if 'giá live 1824' in q else None
            charts=build_charts_for_question(q, paths.trades, paths.history_trades, outdir, live_price=live)
            expected_perf=any(x in q.lower() for x in ['hiệu suất','pnl','performance','equity','lời lỗ','30 trades','20 ngày','10 phiên','7 phiên','đánh thuận'])
            expected_trade=any(x in q.lower() for x in ['kèo','entry','target','biên','horizon','ohlc','fill','gap','ladder','engine'])
            kinds={c['kind'] for c in charts}
            checks={
                'chart_keyword': wants_chart(q),
                'chart_created': bool(charts),
                'files_exist': all((outdir/c['file']).exists() and (outdir/c['file']).stat().st_size>5000 for c in charts),
                'source_valid': all(c['source'] in {paths.trades.name, paths.history_trades.name} for c in charts),
                'performance_present': (not expected_perf) or ('performance' in kinds),
                'forward_present': (not expected_trade) or ('forward_levels' in kinds),
                'answer_nonempty': bool(reply.text.strip()),
                'no_internal_error': 'traceback' not in reply.text.lower() and 'internal error' not in reply.text.lower(),
            }
            passed=all(checks.values())
            results.append({'id':n,'question':q,'intent':reply.structured.get('intent'),'answer':reply.text,'charts':charts,'checks':checks,'pass':passed})
    assert n==1000
    js=root/'BFXPS_AI_V8_26_CHART_1000QA_RESULTS.json'
    js.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    md=root/'BFXPS_AI_V8_26_CHART_1000_QUESTIONS_AND_ANSWERS.md'
    lines=['# BFXPS AI V8.26 - 1000 câu QA về chart','',f'- Tổng: {len(results)}',f'- PASS: {sum(r["pass"] for r in results)}',f'- FAIL: {sum(not r["pass"] for r in results)}','']
    for r in results:
        lines += [f'## {r["id"]}. {r["question"]}',f'- Intent: `{r["intent"]}`',f'- PASS: `{r["pass"]}`',f'- Charts: `{json.dumps(r["charts"],ensure_ascii=False)}`','- Câu trả lời thực tế:','',r['answer'],'','- Checks:', '```json',json.dumps(r['checks'],ensure_ascii=False,indent=2),'```','']
    md.write_text('\n'.join(lines),encoding='utf-8')
    summary={'total':len(results),'pass':sum(r['pass'] for r in results),'fail':sum(not r['pass'] for r in results),'results_json':str(js),'qa_md':str(md)}
    (root/'BFXPS_AI_V8_26_CHART_1000QA_SUMMARY.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))
    if summary['fail']:
        raise SystemExit(1)

if __name__=='__main__': main()

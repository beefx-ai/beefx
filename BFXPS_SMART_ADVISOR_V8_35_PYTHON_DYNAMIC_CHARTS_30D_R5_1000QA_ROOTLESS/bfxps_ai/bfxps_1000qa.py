from __future__ import annotations
import json,re,sys
from pathlib import Path
from collections import Counter,defaultdict
sys.path.insert(0,str(Path(__file__).resolve().parent))
from bfxps_smart_advisor import SmartAdvisor

ROOT=Path(__file__).resolve().parents[1]
ADV=SmartAdvisor(ROOT/'outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv',history_trades_path=ROOT/'outputs/BEST_ENGINE_RECENT_TRADES.tsv')

# Each template has 10 lexical variants and 4 suffix variants = 40 cases; 25 groups = 1000.
groups=[]
def add(name,intent,templates,must=(),must_any=(),forbid=(),source=None):
    groups.append(dict(name=name,intent=intent,templates=templates,must=list(must),must_any=list(must_any),forbid=list(forbid),source=source))

add('today','TODAY_PLANS',['Kèo hôm nay là gì','Hôm nay hệ có kèo nào','Tách kèo từng engine hôm nay','Cho danh sách lệnh phiên hiện hành','Kèo hiện tại theo từng engine','Bot đang xét gì hôm nay','Các plan ngày hôm nay','Hôm nay entry target ra sao','Show kèo hôm nay','Tổng hợp kèo phiên này'],must_any=['28/07/2026','3 row'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('tomorrow','TOMORROW_PLAN',['Kèo ngày mai là gì','Ngày mai có kèo nào','Kèo phiên kế tiếp','Plan tomorrow','Cho lệnh ngày kế tiếp','Ngày sau hệ xét gì','Kèo t+2 ra sao','Kèo 29/7','Ngày mai entry target size','Show tomorrow trade'],must=['29/07/2026'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('latestdate','LATEST_DATE',['Kèo mới nhất ngày nào','Ngày forward xa nhất','Latest trade date','Date kèo mới nhất','Database có kèo tới ngày nào','Ngày kế hoạch mới nhất','Mốc forward cuối','Kèo tương lai xa nhất','Ngày cuối trong plan','Cho ngày mới nhất'],must=['29/07/2026'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('topengines','TOP_ENGINES',['List top engines','Các engine hiện hành','3 trades mỗi engine','Hệ nào đang chạy','Liệt kê engine','Top 3 engine','Mỗi engine ba kèo','Recent trades từng engine','Danh sách profile đang có','Show engines active'],must_any=['SIMCARRRY6','AllDaysLadder_CAP0.3'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('history','HISTORY',['Lịch sử','3 kèo gần nhất','History trades','Các kèo trước','Giao dịch gần đây','Last trades','Cho lịch sử từng engine','Ba lệnh settled gần nhất','Kèo cũ của hệ','Recent history'],source='BEST_ENGINE_RECENT_TRADES.tsv')
add('range','FORECAST_RANGE',['Biên dự kiến từng engine','Biên từng horizon','Expected high low','Vùng forecast các engine','Khung dao động theo plan','Range hôm nay','Biên t+1 t+2','Các vùng entry target','Biên dự báo hệ','High low dự kiến'],must_any=['1.835,0','1.844,3'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('performance7','ENGINE_PERFORMANCE',['Hiệu suất 7 phiên gần đây từng engine','PnL 7 ngày qua của engines','7 phiên settled lời lỗ','Bot giao dịch 7 ngày hiệu quả sao','WR PnL 7 phiên theo engine','Kết quả 7 ngày gần nhất','7 phiên qua engine nào lời','Hiệu suất tuần gần đây','Thống kê 7 phiên','Performance 7 sessions'],must=['7 phiên settled gần nhất','30 row'],source='BEST_ENGINE_RECENT_TRADES.tsv')
add('performance10','ENGINE_PERFORMANCE',['Hiệu suất 10 phiên gần đây từng engine','PnL 10 ngày qua của engines','10 phiên settled lời lỗ','Bot giao dịch 10 ngày hiệu quả sao','WR PnL 10 phiên theo engine','Kết quả 10 ngày gần nhất','10 phiên qua engine nào lời','Hiệu suất mười phiên','Thống kê 10 phiên','Performance 10 sessions'],must=['10 phiên settled gần nhất','30 row'],source='BEST_ENGINE_RECENT_TRADES.tsv')
add('performance20','ENGINE_PERFORMANCE',['Hiệu suất 20 phiên gần đây từng engine','PnL 20 ngày qua của engines','20 phiên settled lời lỗ','Bot giao dịch 20 ngày hiệu quả sao','WR PnL 20 phiên theo engine','Kết quả 20 ngày gần nhất','20 phiên qua engine nào lời','Hiệu suất hai mươi phiên','Thống kê 20 phiên','Performance 20 sessions'],must=['20 phiên settled gần nhất','30 row'],source='BEST_ENGINE_RECENT_TRADES.tsv')
add('trades30','ENGINE_PERFORMANCE',['Hiệu suất 30 trades gần nhất từng engine','PnL 30 lệnh gần đây','30 trades settled lời lỗ','Bot giao dịch 30 lệnh hiệu quả sao','WR PnL 30 trades','Kết quả 30 giao dịch gần nhất','30 lệnh qua engine nào lời','Hiệu suất ba mươi trades','Thống kê 30 trades','Performance last 30 trades'],must=['30 row'],source='BEST_ENGINE_RECENT_TRADES.tsv')
add('reverse','RECENT_PERFORMANCE_REVERSE',['Nếu đánh ngược 7 phiên thì lời lỗ sao','Đảo direction 10 trades kết quả gì','Reverse PnL 20 phiên','Long thay short có lời không','Short thay long hiệu suất sao','Đi ngược hệ 7 ngày','Đối dấu PnL gần đây','Đánh ngược từng engine','Reverse trades gần nhất','Nếu làm ngược bot'],must_any=['không phải backtest','đối dấu'],source='BEST_ENGINE_RECENT_TRADES.tsv')
add('validation','ADVICE_VALIDATION',['Tư vấn được kiểm định chưa','Số liệu này từ đâu','Có backtest không','Độ tin cậy tư vấn','Audit advice','Reclaim đã kiểm định chưa','Minh bạch bằng chứng','Nguồn PnL là gì','Có thể tin kết quả không','Phần nào chưa backtest'],must_any=['kiểm định','backtest','outputs'],source='BEST_ENGINE_RECENT_TRADES.tsv')
add('source','DATA_SOURCE',['Database đang đọc file nào','Nguồn database','File nào cho hiệu suất','File nào cho kèo tương lai','Cho biết nguồn dữ liệu','Data source hiện tại','SSOT là file nào','Các database đang dùng','Nguồn history và forward','Provenance database'],must_any=['BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv','BEST_ENGINE_RECENT_TRADES.tsv'])
add('missing','MISSING_DATA',['Nếu thiếu dữ liệu thì sao','Không đủ info phải làm gì','Thiếu database trả lời thế nào','Không có engine trong base thì sao','Thiếu expected high low','Thiếu R5 action','Không đủ lịch sử','Dữ liệu chưa cập nhật','Base không có số liệu','Không đủ row để kết luận'],must=['beefx.com'],must_any=['Chưa đủ dữ liệu','thiếu'])
add('fill','ENGINE_FILL_AUDIT',['Với OHLC hiện tại kèo nào fill','Engine nào đã khớp','Lệnh nào WAIT_ENTRY','Target có qua trước entry không','Kiểm tra fill từng engine','Hôm nay có lệnh nào vào được','Audit trình tự entry target','Kèo nào chưa chạm entry','Fill status các engines','Tổng hợp khớp lệnh'],must_any=['WAIT_ENTRY','fill','khớp'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('level','LEVEL_EXECUTION',['Bình quân giá từng nấc thế nào','Có nên tăng khối lượng','Ladder cụ thể ra sao','Add vị thế tại đâu','Nấc chung giữa engines','Gap up qua ladder làm gì','Gap down qua target xử lý sao','Vượt giả rồi gãy lại','High 1837.4 được size bao nhiêu','Có được bình quân khi đang lời'],must_any=['nấc','size','khối lượng','ladder'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('bridge_long','BRIDGE_LONG_TO_SHORT',['Long reclaim được không','Long lên điểm chờ short','Mua hồi tới entry short','Sao không long đến điểm short','Kèo long cầu nối','Giá 1809 long reclaim chưa','Giá 1815 long reclaim được chưa','Trigger long reclaim là gì','TP SL long reclaim','Long ngược short t+1'],must_any=['reclaim','LONG','1.812,1'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('bridge_short','BRIDGE_SHORT_TO_LONG',['Short reclaim được không','Short xuống điểm chờ long','Bán hồi tới entry long','Sao không short đến điểm long','Kèo short cầu nối','Giá trên upper short chưa','Trigger short reclaim là gì','TP SL short reclaim','Short ngược long t+1','Advisory short reclaim'],must_any=['reclaim','SHORT','advisory'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('r5','R5_GUIDANCE',['R5 đang cho phép gì','Quyền R5 hiện tại','R5 keep cancel hay flip','Có được đảo chiều do R5 không','R5 action forward','R5 có chính thức chưa','Corrective overlay làm gì','R5 pre open nghĩa gì','Units 0 có đảo lệnh không','R5 có cho long không'],must_any=['R5','PRE_OPEN','KEEP','CANCEL'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('scenario_low','SCENARIO',['O 1807,8 H 1812 L 1804 P 1809, giờ làm gì','Open 1808 high 1812 low 1804 giá 1809','O=1807.8 H=1812 L=1804 C=1809 xử lý sao','Giá 1809 với OHLC 1808 1812 1804','Phiên mở 1808 cao 1812 thấp 1804 hiện 1809','OHLC thấp hơn entry short làm gì','Gap down 1808 rồi hồi 1809','H 1812 chưa chạm 1835','P 1809 có short đuổi không','Range 1804-1812 price 1809'],must_any=['1.809,0','không SHORT đuổi','SHORT 1.835,0'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('scenario_fill','SCENARIO',['O 1834 H 1837.4 L 1828 P 1831 giờ làm gì','Open 1834 high 1837.4 low 1828 close 1831','O=1834 H=1837,4 L=1828 C=1831','Giá 1831 sau khi high 1837.4','OHLC chạm entry 1835 rồi giảm','High 1837.4 đã fill mấy nấc','Phiên 1834-1837.4-1828-1831','Đã vượt 1835 rồi gãy 1831','Kèo short fill chưa với H1837.4','P1831 H1837.4 quản trị sao'],must_any=['1.835,0','SHORT'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('gapup','LEVEL_EXECUTION',['Gap up mở 1846 thì làm gì','Open 1846 vượt toàn ladder','Nhảy gap qua entry 1844.3','Gap lên trên nấc cuối','Mở cửa 1848 có all in không','Gap up 1850 short sao','Bỏ qua nhiều nấc do gap','Catch up ladder khi gap up','Giá mở trên cap có tăng size','Gap vượt biên trên dứt khoát'],must_any=['không','cap','gap'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('gapdown','LEVEL_EXECUTION',['Gap down mở 1808 thì làm gì','Open dưới target trước entry','Nhảy gap xuyên 1811.1','Gap xuống qua target','Mở cửa thấp hơn mục tiêu','Target đi qua trước fill','Gap down có tính thắng không','Có short đuổi sau gap down','Kèo chưa fill mà giá qua target','Gap giảm dứt khoát xử lý'],must_any=['không','target','entry'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
add('freshness','FRESHNESS',['Dữ liệu mới tới ngày nào','Outputs có mới không','Freshness database','Cập nhật gần nhất','Nguồn có stale không','Kèo có đúng ngày không','Database forward tới đâu','History cập nhật đến ngày nào','Ngày dữ liệu cuối','Base có đồng bộ không'],must_any=['database','ngày','Nguồn'],source=None)
add('consensus','CONSENSUS',['Mức chung giữa các engines','Consensus levels','Các nấc trùng nhau','Engine đồng thuận điểm nào','Mốc 1835 có ý nghĩa gì','Mốc 1844.3 là gì','Ưu tiên nấc chung','Điểm giao giữa engine5 và simcarrry6','Các mức đồng thuận','Level overlap của hệ'],must_any=['1.835,0','1.844,3'],source='BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')

suffixes=['?',' nhé',' — trả lời rõ nguồn','; không được bịa']

cases=[]
for g in groups:
    for t in g['templates']:
        for s in suffixes:
            cases.append((g,t+s))
assert len(cases)==1000,len(cases)

results=[]; counts=Counter(); failures=[]
for idx,(g,q) in enumerate(cases,1):
    # For fill audit, seed a fresh OHLC snapshot in the same session.
    sid=f'qa{idx:04d}'
    if g['name']=='fill':
        ADV.ask('O 1834 H 1837,4 L 1828 P 1831',session_id=sid)
    try:
        r=ADV.ask(q,session_id=sid)
        text=r.text or ''
        reasons=[]
        allowed={g['intent']}
        if g['name'] in {'performance7','performance10','performance20','trades30'}: allowed={'ENGINE_PERFORMANCE','RECENT_PERFORMANCE'}
        if g['name']=='scenario_fill': allowed={'SCENARIO','FILL_STATUS','SIDE_PLAN'}
        if g['name']=='scenario_low': allowed={'SCENARIO','COUNTERTREND_PLAN','SIDE_PLAN','LEVEL_EXECUTION'}
        if g['name'] in {'gapup','gapdown'}: allowed={'LEVEL_EXECUTION','SCENARIO','OPEN_SCENARIO','TARGET'}
        if g['name']=='bridge_long': allowed={'BRIDGE_LONG_TO_SHORT','COUNTERTREND_PLAN'}
        if g['name']=='bridge_short': allowed={'BRIDGE_SHORT_TO_LONG','COUNTERTREND_PLAN','SIDE_PLAN'}
        if r.intent not in allowed:
            reasons.append(f'intent={r.intent}, expected={sorted(allowed)}')
        for m in g['must']:
            if m.lower() not in text.lower(): reasons.append(f'missing:{m}')
        if g['must_any'] and not any(m.lower() in text.lower() for m in g['must_any']):
            reasons.append('missing_any:'+','.join(g['must_any']))
        for f in g['forbid']:
            if f.lower() in text.lower(): reasons.append(f'forbidden:{f}')
        if g['source'] and g['source'].lower() not in text.lower(): reasons.append('wrong_or_missing_source')
        if r.structured.get('internal_error'): reasons.append('internal_error:'+str(r.structured.get('internal_error')))
        ok=not reasons
    except Exception as e:
        r=None;text='';reasons=[f'exception:{type(e).__name__}:{e}'];ok=False
    counts[(g['name'],'PASS' if ok else 'FAIL')]+=1
    rec={'id':idx,'group':g['name'],'question':q,'expected_intent':g['intent'],'actual_intent':getattr(r,'intent',''),'answer':text,'source_expected':g['source'],'pass':ok,'reasons':reasons}
    results.append(rec)
    if not ok: failures.append(rec)

out=ROOT/'BFXPS_AI_V8_25_1000QA_RESULTS.json'
out.write_text(json.dumps({'summary':{'total':len(results),'passed':len(results)-len(failures),'failed':len(failures)},'counts':{f'{k[0]}:{k[1]}':v for k,v in counts.items()},'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
md=[f'# BFXPS AI V8.25 - 1000 Question QA Evidence','',f'- Total: **{len(results)}**',f'- PASS: **{len(results)-len(failures)}**',f'- FAIL: **{len(failures)}**','', '## Group summary','']
for g in groups:
    md.append(f'- {g["name"]}: PASS {counts[(g["name"],"PASS")]}/40; FAIL {counts[(g["name"],"FAIL")]}/40')
md+=['','## Full questions, actual answers, source and verdict','']
for rec in results:
    md += [f'### QA-{rec["id"]:04d} | {rec["group"]} | {"PASS" if rec["pass"] else "FAIL"}',f'**Question:** {rec["question"]}',f'**Expected intent:** `{rec["expected_intent"]}`  ',f'**Actual intent:** `{rec["actual_intent"]}`  ',f'**Expected source:** `{rec["source_expected"] or "context-dependent"}`  ',f'**Reasons:** {"; ".join(rec["reasons"]) if rec["reasons"] else "None"}','','**Actual answer:**','```text',rec['answer'],'```','']
(ROOT/'BFXPS_AI_V8_25_1000_QUESTIONS_AND_ANSWERS.md').write_text('\n'.join(md),encoding='utf-8')
print(json.dumps({'total':len(results),'passed':len(results)-len(failures),'failed':len(failures),'first_failures':failures[:20]},ensure_ascii=False,indent=2))

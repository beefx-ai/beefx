from __future__ import annotations
from pathlib import Path
import json, tempfile, sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'bfxps_ai'))
from bfxps_smart_advisor import SmartAdvisor
TRADES=ROOT/'outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv'

recent=[
'3 ngày vừa rồi bot giao dịch lời lõm thế nào?', 'Ba ngày gần nhất hệ lời lỗ ra sao?',
'Kết quả 3 ngày gần đây của bot?', 'PnL gần đây của hệ thế nào?',
'Mấy ngày gần đây bot giao dịch có lời không?', '3 phiên vừa rồi kết quả ra sao?',
'Bot giao dịch lời lỗ thế nào trong ba ngày?', 'Cho tôi hiệu suất gần đây theo engine',
'Ba ngày settled gần nhất bot kiếm hay mất bao nhiêu?', 'Kết quả 3 ngày của từng hệ?',
'3 ngày vừa rồi engine nào lời engine nào lỗ?', 'PnL ba phiên vừa qua?',
'Hệ dạo này lời lõm thế nào?', 'Giao dịch gần đây PnL bao nhiêu?',
'Ba ngày mới nhất đã settle kết quả thế nào?', '3 ngày qua báo lãi lỗ rõ từng engine',
'Hiệu suất gần đây minh bạch từng hệ', 'Bot gd 3 ngày vừa rồi lời lõm ntn',
'Kết quả các phiên gần nhất theo database?', 'Cho bảng lời lỗ 3 ngày vừa rồi',
]
reverse=[
'3 ngày vừa rồi nếu đánh ngược thì lời lõm thế nào?', 'Nếu đảo ngược mọi kèo gần đây thì PnL ra sao?',
'Đánh ngược hệ 3 phiên vừa rồi có lời không?', 'Reverse PnL các lệnh gần nhất?',
'Nếu LONG thay SHORT trong 3 ngày thì sao?', 'Nếu SHORT thay LONG thì hiệu suất thế nào?',
'Ba ngày qua đi ngược hệ lời lỗ bao nhiêu?', 'Tính thử đánh ngược từng engine gần đây',
'Kèo vừa rồi đảo chiều thì PnL thế nào?', 'Nếu làm ngược lại từng lệnh thì sao?',
'Opposite PnL ba phiên vừa qua', 'Đánh ngược cơ học các kèo settled?',
'Cho biết lời lỗ nếu đảo direction', 'Nếu tôi luôn đánh ngược bot thì 3 ngày qua sao?',
'Hiệu suất ngược hệ gần đây?', 'Pnl đánh ngược từng engine',
'Ba ngày vừa rồi reverse trade có ăn không?', 'Giả sử đảo LONG SHORT thì lời lõm ntn',
'Kết quả đối dấu PnL các lệnh gần nhất', 'Nếu đánh ngược nhưng giữ entry exit cũ thì sao?',
]
engine=[
'Hiệu suất từng engine trong database hiện tại?', 'PnL từng engine ra sao?',
'Engine nào lời engine nào lỗ?', 'So sánh hiệu suất engine gần đây',
'Kết quả từng hệ hiện tại?', 'Cho WR và PnL từng engine',
'AllDaysLadder, 12K và SIMCARRRY6 lời lỗ thế nào?', 'Tách hiệu suất theo từng profile',
'Không cộng trùng, báo PnL từng engine', 'Engine top nào đang hiệu quả?',
'Từng engine có bao nhiêu lệnh thực thi?', 'Báo win loss từng hệ',
'Hiệu suất riêng SIMCARRRY6 và engine5?', 'So sánh 3 engines trên lịch sử nạp',
'Pnl theo engine và số lệnh cancel?', 'WR từng engine trong những ngày có dữ liệu',
'Engine nào có PnL cao nhất gần đây?', 'Thống kê từng hệ, đừng cộng danh mục',
'Kết quả từng profile engine5 và simcarry', 'Minh bạch hiệu suất đến từng engine',
]
validation=[
'Tư vấn của bạn được kiểm định chưa?', 'Các khuyến nghị này đã backtest chưa?',
'Có bằng chứng cho tư vấn không?', 'Độ tin cậy tư vấn hiện tại?',
'Số liệu này từ đâu?', 'Kiểm định hiệu suất của advisor thế nào?',
'Minh bạch phần nào đã audit phần nào chưa', 'LONG reclaim đã được kiểm định chưa?',
'SHORT reclaim đã được backtest chưa?', 'Tư vấn gap và ladder có backtest riêng không?',
'Base outputs là backtest toàn kỳ à?', 'Audit tư vấn của hệ cho tôi',
'Phần nào chỉ là diễn giải outputs?', 'Có thể tin số PnL đánh ngược không?',
'Tư vấn này có OOS không?', 'Nêu rõ provenance của backtest reclaim',
'Bạn có tự tái lập backtest được không?', 'Các con số WR PnL có nguồn gì?',
'Kịch bản nào được promote advisory?', 'Phân loại mức kiểm định từng loại tư vấn',
]
mixed=[
'3 ngày vừa rồi lời lỗ thế nào và tư vấn đã kiểm định chưa?',
'Nếu đánh ngược gần đây thì sao, số đó có phải backtest không?',
'Hiệu suất từng engine và nguồn số liệu là gì?',
'PnL gần đây có được cộng hai profile engine5 không?',
'Tư vấn reclaim và kết quả base khác nhau thế nào?',
'Cho kết quả 3 phiên rồi nói rõ mức kiểm định',
'Đánh ngược bot có lời không và đã audit chưa?',
'Engine nào lời nhất, nhưng dữ liệu có đủ toàn kỳ không?',
'Báo PnL từng hệ và cảnh báo đếm đôi',
'Các tư vấn hiệu suất có minh bạch theo engine không?',
]
noise=[
'O 1808 H 1812 L 1804 P 1809. 3 ngày vừa rồi bot lời lỗ thế nào?',
'O 1845 H 1848 L 1842 P 1846. Nếu đánh ngược 3 ngày thì sao?',
'Giá 1833. Hiệu suất từng engine hiện tại?',
'Vừa bị cutloss. Tư vấn của bạn được kiểm định chưa?',
'Kèo ngày mai xong cho hỏi PnL 3 ngày gần đây?',
'Biên dự kiến 1835-1844. Đánh ngược hệ gần đây lời không?',
'Tôi đang SHORT. Nêu hiệu suất từng engine, không xử lý vị thế.',
'R5 KEEP. Số liệu tư vấn đã backtest chưa?',
'Giá đang 1809 nhưng tôi hỏi kết quả 3 ngày settled.',
'OHLC mới rồi, cho audit mức kiểm định tư vấn.',
]
assert sum(map(len,[recent,reverse,engine,validation,mixed,noise]))==100

cases=[]
for q in recent: cases.append((q,None if any(x in q.lower() for x in ['engine','từng hệ','tung he']) else 'RECENT_PERFORMANCE',['27/07/2026','24/07/2026','SIMCARRRY6','không tự bù']))
for q in reverse: cases.append((q,'RECENT_PERFORMANCE_REVERSE',['Đánh ngược cơ học','không phải chiến lược đã backtest','-16.20']))
for q in engine: cases.append((q,'ENGINE_PERFORMANCE',['SIMCARRRY6','AllDaysLadder_CAP0.3','12K_AllDay','không phải backtest toàn kỳ']))
for q in validation: cases.append((q,None,['LONG reclaim','SHORT reclaim','chưa mặc nhiên','không phải backtest chiến lược ngược']))
# mixed can route to validation/reverse/engine depending strongest explicit phrase
for q in mixed:
    cases.append((q,None,['không', 'engine']))
for q in noise:
    # inspect by semantic target
    if 'đánh ngược' in q.lower(): cases.append((q,'RECENT_PERFORMANCE_REVERSE',['không phải chiến lược đã backtest']))
    elif 'hiệu suất từng engine' in q.lower(): cases.append((q,'ENGINE_PERFORMANCE',['SIMCARRRY6']))
    elif 'kiểm định' in q.lower() or 'backtest' in q.lower() or 'audit' in q.lower(): cases.append((q,'ADVICE_VALIDATION',['LONG reclaim']))
    else: cases.append((q,'RECENT_PERFORMANCE',['27/07/2026']))

results=[]
with tempfile.TemporaryDirectory() as td:
    adv=SmartAdvisor(TRADES,memory_path=Path(td)/'memory.json')
    # seed memory pollution in same session
    adv.ask('O 1807,8 H 1812 L 1804 P 1809, giờ làm gì?',session_id='shared')
    for i,(q,intent,required) in enumerate(cases,1):
        sid='shared' if i%4==0 else f'qa{i}'
        r=adv.ask(q,session_id=sid)
        missing=[x for x in required if x.lower() not in r.text.lower()]
        intent_ok=True if intent is None else r.intent==intent
        passed=intent_ok and not missing
        results.append({'id':i,'question':q,'intent':r.intent,'expected_intent':intent,'pass':passed,'missing':missing,'answer':r.text})
failed=[x for x in results if not x['pass']]
report={'total':len(results),'passed':len(results)-len(failed),'failed':len(failed),'failures':failed,'results':results}
(ROOT/'BFXPS_AI_V8_20_PERFORMANCE_100QA_RESULTS.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'total':report['total'],'passed':report['passed'],'failed':report['failed']},ensure_ascii=False))
if failed:
    for f in failed[:10]: print('\nFAIL',f['id'],f['question'],f['intent'],f['missing'],'\n',f['answer'])
raise SystemExit(1 if failed else 0)

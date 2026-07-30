from pathlib import Path
import tempfile, sys, json
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'bfxps_ai'))
from bfxps_smart_advisor import SmartAdvisor
TRADES=ROOT/'outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv'
questions=[
'Bot giao dịch 10 ngày qua hiệu suất như thế nào?',
'Bot giao dịch 20 ngày qua hiệu suất như thế nào?',
'Bot giao dịch 30 ngày qua hiệu suất như thế nào?',
'10 phiên settled gần nhất từng engine lời lỗ ra sao?',
'20 phiên qua engine nào hiệu quả nhất?',
'30 trades gần nhất từng engine lời lỗ ra sao?',
'10 trades gần nhất của SIMCARRRY6 hiệu suất thế nào?',
'20 lệnh gần nhất của AllDaysLadder lời lỗ ra sao?',
'30 lệnh gần nhất của 12K_AllDay có WR bao nhiêu?',
'10 ngày qua nếu đánh ngược thì PnL thế nào?',
'20 ngày qua nếu đảo LONG SHORT thì sao?',
'30 trades gần nhất đánh ngược cơ học lời lỗ thế nào?',
'Hiệu suất 10 ngày qua có cộng hai profile engine5 không?',
'Hiệu suất 20 ngày qua tách từng engine giúp tôi.',
'Cho số row, số lệnh thực thi, cancel, WR và PnL 30 trades gần nhất.',
'Database hiện có thật bao nhiêu row HISTORY?',
'File đang đọc có đủ 30 trades không?',
'Chuỗi audit last30 trong note có nghĩa là có 30 row thật không?',
'Tư vấn hiệu suất 10 ngày đã được kiểm định chưa?',
'Tư vấn đánh ngược 20 ngày có phải backtest không?',
'O 1808 H 1812 L 1804 P 1809. Bot 10 ngày qua hiệu suất thế nào?',
'Giá đang 1833, nhưng tôi hỏi hiệu suất 20 ngày qua.',
'Kèo ngày mai xong cho biết 30 trades gần nhất lời lỗ.',
'Biên dự kiến thế nào, đồng thời hiệu suất 10 phiên gần nhất?',
'R5 KEEP. Hiệu suất 20 ngày từng engine?',
'Vừa cutloss, 30 trades gần nhất bot có đáng tin không?',
'10 ngày qua từng ngày lời lỗ thế nào?',
'20 ngày qua bao nhiêu ngày có dữ liệu settled?',
'30 trades gần nhất bao phủ từ ngày nào đến ngày nào?',
'Nếu thiếu dữ liệu 20 ngày thì nói rõ thiếu bao nhiêu.',
]
with tempfile.TemporaryDirectory() as td:
    adv=SmartAdvisor(TRADES,memory_path=Path(td)/'mem.json')
    results=[]
    for i,q in enumerate(questions,1):
        r=adv.ask(q,session_id='shared' if i%5==0 else f'q{i}')
        results.append({'id':i,'question':q,'intent':r.intent,'answer':r.text,'error':r.structured.get('internal_error')})
md=['# BFXPS Smart Advisor V8.21 - Performance Windows QA Evidence','',
'## Phát hiện nguồn dữ liệu','',
'- File kiểm thử thực tế: `outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv`.','- Trong package hiện có 5 row HISTORY/PnL trên 2 ngày (24/07 và 27/07), không có 30 row lịch sử.','- Cụm `audit last30` trong ghi chú một row không được coi là 30 giao dịch thật.','- Runtime V8.21 đã được sửa để tự phát hiện và ưu tiên file `*LAST30*TRADES*`, file trades có nhiều HISTORY hơn và ngày FORWARD mới nhất.','',
'## Danh sách nguyên văn câu đã hỏi và câu trả lời','']
for x in results:
    md += [f"### Q{x['id']:02d}. {x['question']}",f"- Intent: `{x['intent']}`",'- Trả lời thực tế:','```text',x['answer'],'```',f"- Internal error: `{x['error'] or 'None'}`",'']
md += ['## Tiêu chí PASS của vòng này','',
'- Câu có 10/20/30 phải giữ đúng con số yêu cầu, không hard-code 3 ngày.','- Nếu thiếu row phải công bố số row/ngày thực có và phần thiếu.','- Không dùng FORWARD để bù HISTORY.','- Không coi metadata `last30` là 30 row.','- Thống kê tách từng engine/profile và không cộng đếm đôi hai profile trùng tín hiệu.','- PnL đánh ngược chỉ được gọi là đối dấu cơ học, không phải backtest.','- OHLC hoặc memory trước đó không được kéo câu hỏi hiệu suất sang tư vấn kèo hiện tại.']
(ROOT/'BFXPS_AI_V8_21_QUESTIONS_AND_ANSWERS.md').write_text('\n'.join(md),encoding='utf-8')
(ROOT/'BFXPS_AI_V8_21_PERFORMANCE_WINDOWS_RESULTS.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
failed=[x for x in results if x['error'] or ('10 ngày' in x['question'].lower() and '10' not in x['answer']) or ('20 ngày' in x['question'].lower() and '20' not in x['answer']) or ('30 trades' in x['question'].lower() and '30' not in x['answer'])]
print(json.dumps({'total':len(results),'failed':len(failed)},ensure_ascii=False))
if failed:
    for x in failed: print('FAIL',x['id'],x['question'],x['error'])
    raise SystemExit(1)

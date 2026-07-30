from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from bfxps_smart_advisor import SmartAdvisor
from bfxps_runtime import describe, resolve_runtime_paths
from performance_chart import build_performance_chart
from dynamic_charts import build_charts_for_question, wants_chart

APP_VERSION = "V8.35"
APP_NAME = f"BFXPS Smart Advisor {APP_VERSION}+++ Python Dynamic Charts 30D"

HTML = r'''<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__APP_NAME__</title>
<style>
:root{color-scheme:light dark;--bg:#0e1117;--panel:#171b23;--text:#e6edf3;--muted:#9aa4b2;--accent:#2f81f7;--border:#30363d}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui;background:var(--bg);color:var(--text)}
main{max-width:1040px;margin:auto;padding:20px}.top{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:14px}
.badge{border:1px solid var(--border);border-radius:999px;padding:5px 10px;color:var(--muted)}
#chat{min-height:520px;border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:16px;overflow:auto}
.msg{max-width:88%;margin:10px 0;padding:10px 12px;border-radius:14px;white-space:pre-wrap}.user{margin-left:auto;background:var(--accent);color:white}.bot{background:#222833;border:1px solid var(--border)}
.quick-wrap{margin:12px 0 4px}.quick-title{font-size:13px;font-weight:700;margin-bottom:8px}.quick{display:flex;flex-wrap:wrap;gap:8px}.quick button{border:1px solid var(--border);border-radius:999px;padding:7px 11px;background:#222833;color:var(--text);cursor:pointer}.quick button:hover{border-color:var(--accent);background:#293446}.controls{display:grid;grid-template-columns:1fr 110px 110px 110px 110px;gap:8px;margin-top:12px}.controls input,.controls button{border:1px solid var(--border);border-radius:10px;padding:10px;background:var(--panel);color:var(--text)}
.controls button{background:var(--accent);color:white;font-weight:600;cursor:pointer}.small{font-size:12px;color:var(--muted);margin-top:8px}.perf-chart{margin-top:14px;border:1px solid var(--border);border-radius:14px;padding:10px;background:var(--panel)}.perf-chart img{width:100%;height:auto;border-radius:8px}.chart-card{margin-top:14px;border:1px solid var(--border);border-radius:14px;padding:10px;background:var(--panel)}.chart-card img{width:100%;height:auto;border-radius:8px}.chart-summary{font-size:13px;margin-top:8px}.chart-warning{font-size:13px;margin-top:6px;color:#ff6b6b;font-weight:700;white-space:pre-wrap}.hidden{display:none}@media(max-width:760px){.controls{grid-template-columns:1fr 1fr}.controls #q{grid-column:1/-1}.controls button{grid-column:1/-1}}
</style>
</head>
<body><main>
<div class="top"><div><h2 style="margin:0">__APP_NAME__</h2><div class="small">Python/Matplotlib dynamic charts • 3 CSDL là nguồn sự thật • PnL, forecast, Low/Center/High all days và R5</div></div><span class="badge" id="status">sẵn sàng</span></div>
<div id="chat"><div class="msg bot">Hỏi tự nhiên, nhập nhanh OHLC hoặc bấm một câu hỏi phổ biến bên dưới.</div></div>
<div class="quick-wrap">
  <div class="quick-title">Câu hỏi phổ biến</div>
  <div class="quick" id="quickQuestions">
    <button data-q="Hôm nay hệ có những kèo nào?">Kèo hôm nay</button>
    <button data-q="Kèo mới nhất ngày nào? Liệt kê rõ từng engine.">Ngày kèo mới nhất</button>
    <button data-q="List top engines hiện hành, hiển thị đúng 3 trades của mỗi engine gồm history và future, ghi rõ ngày.">3 trades/engine</button>
    <button data-q="Lịch sử: trả 3 kèo gần nhất, ưu tiên 1 kèo của mỗi engine top.">3 kèo gần nhất</button>
    <button data-q="Tách riêng kèo của từng engine hôm nay, gồm entry, target, size và trạng thái.">Tách từng engine</button>
    <button data-q="Biên dự kiến của từng engine và từng horizon là gì?">Biên dự kiến</button>
    <button data-q="O 1822 H 1852 L 1814,2 P 1850, forecast lên chart: vẽ mọi kèo, Expected Low, Center, Expected High, Entry, TP và R5.">Forecast on chart</button>
    <button data-q="PnL on chart từ CSDL 30D: equity, PnL từng ngày, drawdown, WR và các ngày R5 CANCEL.">PnL on chart</button>
    <button data-q="Chart giá kỳ vọng Low/Center/High all days của từng engine, dùng 3 CSDL và ghi rõ warm-up/forward.">Low/Center/High all days</button>
    <button data-q="Chart center và biên all: bao gồm hết Expected Low, Center, Expected High của mọi ngày và mọi kèo forward.">Chart biên all</button>
    <button data-q="R5 lên chart: nếu KEEP thì ghi rõ được đánh; nếu CANCEL, FLIP_HINT hoặc PRE_OPEN thì cảnh báo đỏ.">R5 on chart</button>
    <button data-q="Chart all: PnL 30D, forecast, Low/Center/High all days, kèo on chart và R5 cảnh báo đỏ.">Chart ALL</button>
    <button data-q="R5 hiện tại đang cho phép KEEP, CANCEL hay FLIP_HINT? Chỉ trả theo outputs mới nhất.">Quyền R5</button>
    <button data-q="O 1807,8 H 1812 L 1804 P 1809, giờ làm gì?">Nhập mẫu OHLC</button>
    <button data-q="Với OHLC hiện tại, kèo nào đã fill, kèo nào WAIT_ENTRY và có target nào đi qua trước entry?">Kiểm tra khớp lệnh</button>
    <button data-q="Kèo ngày mai là gì? Ghi rõ ngày, engine, direction, entry, target và size.">Kèo ngày mai</button>
    <button data-q="3 ngày settled gần nhất bot giao dịch lời lỗ thế nào theo từng engine?">PnL 3 ngày</button>
    <button data-q="3 ngày vừa rồi nếu đánh ngược từng kèo thì lời lỗ thế nào? Nêu rõ phần nào chỉ là giả định.">PnL đánh ngược</button>
    <button data-q="Hiệu suất từng engine trong database hiện tại là gì? Không cộng trùng profile.">Hiệu suất engine</button>
    <button data-q="Tư vấn của bạn đã được kiểm định chưa? Tách rõ base outputs, reclaim và phần chưa backtest.">Kiểm định tư vấn</button>
    <button data-q="O 1808 H 1816 L 1804 P 1815. LONG reclaim lên điểm chờ SHORT được không?">LONG reclaim</button>
    <button data-q="Hệ đang chờ LONG. O 2025 H 2028 L 2021 P 2022. SHORT reclaim xuống điểm chờ LONG được không?">SHORT reclaim</button>
  </div>
</div>
<div class="controls">
<input id="q" placeholder="Nhập câu hỏi...">
<input id="o" type="number" step="0.1" placeholder="Open">
<input id="h" type="number" step="0.1" placeholder="High">
<input id="l" type="number" step="0.1" placeholder="Low">
<input id="p" type="number" step="0.1" placeholder="Giá live">
<button id="send">Gửi</button>
</div>
<div id="chartGallery"></div><div class="small">Ứng dụng chỉ đọc dữ liệu hệ thống; không tự đặt lệnh, không tự bịa stop-loss hoặc xác suất.</div>
</main>
<script>
const chat=document.getElementById('chat'), q=document.getElementById('q'), status=document.getElementById('status');
function add(text, cls){const d=document.createElement('div');d.className='msg '+cls;d.textContent=text;chat.appendChild(d);chat.scrollTop=chat.scrollHeight}
async function serverAlive(){try{const r=await fetch('/health?ts='+Date.now(),{cache:'no-store'});if(!r.ok)return false;const d=await r.json();return d&&d.ok===true&&d.version==='V8.35'}catch(_){return false}}
async function send(){const question=q.value.trim();if(!question)return;add(question,'user');q.value='';status.textContent='đang kiểm tra server';
 if(!(await serverAlive())){add('Server BFXPS V8.35 đã dừng hoặc chưa khởi động. Hãy chạy lại RUN_BFXPS_AI_WEB.bat; không cần đổi câu hỏi.','bot');status.textContent='server offline';return}
 status.textContent='đang phân tích';
 const body={question,session_id:'web'};
 for(const [id,key] of [['o','session_open'],['h','session_high'],['l','session_low'],['p','live_price']]){const v=document.getElementById(id).value;if(v!=='')body[key]=Number(v)}
 try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),cache:'no-store'});const raw=await r.text();let data;try{data=JSON.parse(raw)}catch(_){throw new Error('Server trả dữ liệu không phải JSON')};if(!r.ok)throw new Error(data.error||('HTTP '+r.status));add(String(data.answer||data.error||'Hệ không trả được nội dung; kiểm tra outputs và chạy self-test.'),'bot');status.textContent=data.intent||'recovery';const gallery=document.getElementById('chartGallery');gallery.innerHTML='';if(Array.isArray(data.charts)){for(const c of data.charts){const card=document.createElement('div');card.className='chart-card';const title=document.createElement('div');title.className='quick-title';title.textContent=c.title||'Chart';const img=document.createElement('img');img.src='/dynamic-chart/'+encodeURIComponent(c.file)+'?t='+Date.now();img.alt=title.textContent;const src=document.createElement('div');src.className='small';src.textContent='Nguồn database: '+String(c.source||'outputs');card.append(title,img,src);if(c.summary){const sm=document.createElement('div');sm.className='chart-summary';sm.textContent=String(c.summary);card.appendChild(sm)}if(Array.isArray(c.warnings)&&c.warnings.length){const w=document.createElement('div');w.className='chart-warning';w.textContent=c.warnings.join('\n');card.appendChild(w)}gallery.appendChild(card);}}}
 catch(e){const alive=await serverAlive();add(alive?('Lỗi API: '+e.message+'. Xem runtime_logs/BFXPS_AI_WEB_SERVER.log.'):('Server BFXPS V8.35 đã dừng giữa lúc xử lý. Chạy lại RUN_BFXPS_AI_WEB.bat và xem runtime_logs/BFXPS_AI_WEB_SERVER.log.'),'bot');status.textContent=alive?'api error':'server offline'} }
document.getElementById('send').addEventListener('click',send);q.addEventListener('keydown',e=>{if(e.key==='Enter')send()});
document.getElementById('quickQuestions').addEventListener('click',e=>{const b=e.target.closest('button[data-q]');if(!b)return;q.value=b.dataset.q;q.focus();send();});
</script></body></html>'''.replace('__APP_NAME__', APP_NAME)


def make_handler(advisor: SmartAdvisor):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, data: dict):
            payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if urlparse(self.path).path == "/":
                data = HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(data)
            elif urlparse(self.path).path == "/performance-chart.png":
                chart = advisor.trades_file.parent / "PERFORMANCE_RECENT30_EQUITY.png"
                if chart.exists():
                    data = chart.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._json(404, {"error": "Performance chart not available"})
            elif urlparse(self.path).path.startswith("/dynamic-chart/"):
                name = Path(urlparse(self.path).path).name
                chart_dir = advisor.trades_file.parent / "dynamic_charts"
                chart = (chart_dir / name).resolve()
                if chart.parent == chart_dir.resolve() and chart.exists() and chart.suffix.lower() == ".png":
                    data = chart.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._json(404, {"error": "Dynamic chart not available"})
            elif urlparse(self.path).path == "/health":
                self._json(200, {"ok": True, "service": APP_NAME, "version": APP_VERSION})
            else:
                self._json(404, {"error": "Not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/chat":
                self._json(404, {"error": "Not found"})
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                question = str(body.get("question", "")).strip()
                if not question:
                    raise ValueError("Thiếu question")
                reply = advisor.ask(
                    question,
                    session_id=str(body.get("session_id", "web")),
                    as_of=body.get("as_of"),
                    live_price=body.get("live_price"),
                    session_open=body.get("session_open"),
                    session_high=body.get("session_high"),
                    session_low=body.get("session_low"),
                )
                charts = build_charts_for_question(
                    question, advisor.trades_file, advisor.history_trades_path,
                    advisor.trades_file.parent / "dynamic_charts",
                    last3_tsv=advisor.trades_file.parent / "BEST_ENGINE_CHART_LAST3_TRADES.tsv",
                    ohlc_tsv=advisor.ohlc_file if getattr(advisor, "ohlc_file", None) else None,
                    live_price=body.get("live_price"), session_open=body.get("session_open"),
                    session_high=body.get("session_high"), session_low=body.get("session_low"),
                )
                structured = dict(reply.structured)
                structured["charts"] = charts
                if wants_chart(question):
                    if charts:
                        structured["chart_status"] = "GENERATED_FROM_DATABASE"
                    else:
                        structured["chart_status"] = "MISSING_DATA_CONTACT_BEEFX_COM"
                answer_text = reply.text
                if wants_chart(question):
                    if charts:
                        source_names = ", ".join(sorted({c.get("source", "outputs") for c in charts}))
                        kinds = ", ".join(c.get("kind", "chart") for c in charts)
                        answer_text += f"\nĐã dựng {len(charts)} dynamic chart bằng Python/Matplotlib ({kinds}). Nguồn: {source_names}."
                        summaries = [str(c.get("summary")) for c in charts if c.get("summary")]
                        if summaries:
                            answer_text += "\n" + "\n".join(f"- {x}" for x in summaries)
                        warnings = []
                        for c in charts:
                            for w in c.get("warnings", []) or []:
                                if w not in warnings:
                                    warnings.append(w)
                        if warnings:
                            answer_text += "\nCẢNH BÁO TRÊN CHART:\n" + "\n".join(f"- {w}" for w in warnings)
                        answer_text += "\nKhông gọi ứng dụng tạo ảnh; chart chỉ dùng dữ liệu từ 3 CSDL trong outputs."
                    else:
                        answer_text += "\nChưa đủ dữ liệu trong 3 CSDL để dựng chart."
                self._json(200, {"answer": answer_text, **structured})
            except Exception as exc:
                self._json(500, {"answer": "Hệ gặp lỗi xử lý nhưng không được phép im lặng. Tạm thời NO TRADE; kiểm tra outputs và chạy self-test.", "error": str(exc), "intent": "RECOVERY_ERROR"})

        def log_message(self, fmt, *args):
            return

    return Handler


def main():
    p = argparse.ArgumentParser(description="BFXPS Smart Advisor local web server")
    p.add_argument("--root", help="BFXPS root; mặc định tự nhận từ vị trí bfxps_ai")
    p.add_argument("--trades", help="Mặc định outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv")
    p.add_argument("--ohlc")
    p.add_argument("--warning-catalog")
    p.add_argument("--policy")
    p.add_argument("--memory", help="Mặc định bfxps_ai/runtime/web_memory.json")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    paths = resolve_runtime_paths(
        root=args.root, trades=args.trades, ohlc=args.ohlc,
        warning_catalog=args.warning_catalog, policy=args.policy,
    )
    memory = Path(args.memory).expanduser() if args.memory else paths.web_memory
    if not memory.is_absolute():
        memory = paths.root / memory
    advisor = SmartAdvisor(paths.trades, paths.ohlc if paths.ohlc.exists() else None, paths.history_trades, paths.warning_catalog, memory, paths.policy)
    advisor.trades_file = paths.trades
    advisor.ohlc_file = paths.ohlc if paths.ohlc.exists() else None
    build_performance_chart(paths.history_trades, paths.trades.parent / "PERFORMANCE_RECENT30_EQUITY.png")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(advisor))
    print(describe(paths))
    print(f"BFXPS Smart Advisor: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

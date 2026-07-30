from pathlib import Path
import json, tempfile
from bfxps_smart_advisor import SmartAdvisor

ROOT = Path(__file__).resolve().parents[1]
trades = ROOT / "outputs" / "BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv"
with tempfile.TemporaryDirectory() as td:
    memory = Path(td) / "memory.json"
    advisor = SmartAdvisor(trades, memory_path=memory)
    sid = "truth-chain"
    q1 = "Ngày Mở cửa Đóng cửa Cao nhất Thấp nhất KL khớp KL HĐ mở OI Thay đổi 28/07/2026 1,807.8 1,824.0 1,834.9 1,796.3 266,987 41,854 12.90 (0.71%)\nkèo sao"
    r1 = advisor.ask(q1, session_id=sid)
    assert "High chỉ 1.834,9" in r1.text and "không có fill" in r1.text.lower(), r1.text
    q2 = "cả phiên bắt tao chờ 1835 cơ mà, làm gì cho short lệnh nào khớp"
    r2 = advisor.ask(q2, session_id=sid)
    assert r2.intent == "ENGINE_FILL_AUDIT", (r2.intent, r2.text)
    assert "không engine nào" in r2.text.lower(), r2.text
    assert "1.834,9" in r2.text and "1.835,0" in r2.text, r2.text
    assert r2.structured["market_input"]["update_applied"] is False, r2.structured["market_input"]
    assert r2.structured["memory"]["last_live_price"] == 1824.0, r2.structured["memory"]
    q3 = "tóm lại nay có engines nào khớp dc lệnh"
    r3 = advisor.ask(q3, session_id=sid)
    assert r3.intent == "ENGINE_FILL_AUDIT", (r3.intent, r3.text)
    assert "không engine nào" in r3.text.lower(), r3.text
    assert "SimPTKT" in r3.text, r3.text
    assert "Giá có đi qua" in r3.text, r3.text
    assert r3.structured["memory"]["last_live_price"] == 1824.0, r3.structured["memory"]
    # Reference prices must not mutate OHLC.
    for q in ["sao không khớp 1835", "entry 1835 cơ mà", "mày bảo chờ 1835", "short 1835 có khớp không"]:
        rr = advisor.ask(q, session_id=sid)
        m = rr.structured["memory"]
        assert m["last_live_price"] == 1824.0, (q, m, rr.text)
        assert rr.context.get("as_of") == "28/07/2026"
    # Explicit new live price still updates safely.
    r4 = advisor.ask("giờ giá đang 1840", session_id=sid)
    assert r4.structured["memory"]["last_live_price"] == 1840.0, r4.structured["memory"]
    assert r4.structured["market_input"]["update_applied"] is True
print("PASS: conversation references cannot rewrite OHLC; engine fill audit is independently reconciled.")

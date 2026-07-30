from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd

from bfxps_runtime import describe, resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor
from bfxps_customer_bridge import SessionSnapshot

REQUIRED_13 = [
    "STT", "Ngày entry", "Loại lệnh", "Entry", "Exit", "PNL points", "Win/Loss",
    "Ghi chú HybridV3", "Forecast", "AlgoCheck", "EntryAlgoV2Check", "So sánh OHLC", "Độ khớp",
]
MAX_LINES = 7
BANNED_CUSTOMER_JARGON = [
    "OriginalEntry", "OriginalTarget", "Expected target", "EntryTargetSwap",
    "gpt_simcarrry6", "gpt_simptkt", "operational", "raw geometry",
]


def assert_customer_answer(text: str, label: str) -> None:
    lines = [x for x in text.splitlines() if x.strip()]
    if not text.strip():
        raise AssertionError(f"Câu trả lời rỗng: {label}")
    if len(lines) > MAX_LINES:
        raise AssertionError(f"Quá {MAX_LINES} dòng ({len(lines)}): {label}\n{text}")
    bad = [term for term in BANNED_CUSTOMER_JARGON if term.lower() in text.lower()]
    if bad:
        raise AssertionError(f"Phun jargon nội bộ {bad}: {label}\n{text}")


def main() -> None:
    paths = resolve_runtime_paths()
    print(describe(paths))
    df = pd.read_csv(paths.trades, sep="\t", dtype=str)
    missing = [c for c in REQUIRED_13 if c not in df.columns]
    if missing:
        raise AssertionError(f"Thiếu cột chuẩn: {missing}")
    if df.empty:
        raise AssertionError("File trades rỗng")
    catalog = json.loads(paths.warning_catalog.read_text(encoding="utf-8"))
    contradiction = next((w for w in catalog.get("warnings", []) if w.get("id") == "BAND_DIRECTION_CONTRADICTION"), None)
    if not contradiction or not contradiction.get("evidence_metrics"):
        raise AssertionError("Thiếu evidence_metrics cho BAND_DIRECTION_CONTRADICTION")

    with tempfile.TemporaryDirectory(prefix="bfxps_ai_test_") as td:
        advisor = SmartAdvisor(
            paths.trades, paths.ohlc if paths.ohlc.exists() else None, paths.warning_catalog,
            Path(td) / "memory.json", paths.policy,
        )
        # V6: kiểm tra tư vấn giao dịch ngược nhịp không bị đồng nhất với tín hiệu hệ thống.
        fake_context = {"warnings": [], "active_plans": [], "r5_control": {"current_action": "FLIP_HINT", "max_position": 0.3}}
        fake_plan = {"direction": "SHORT", "operational_entry": 1828.4, "operational_target": 1802.0, "engine": "gpt_simcarrry6", "profile": "", "horizon": "t+1"}
        q_counter = "Open 1807.8 high 1812 low 1804 giá hiện tại 1809, tao nhất định long lên vùng chờ short"
        if advisor._classify_intent(q_counter) != "COUNTERTREND_PLAN":
            raise AssertionError("Không nhận diện COUNTERTREND_PLAN")
        counter_lines = advisor._answer_countertrend_plan(
            fake_context, fake_plan, q_counter,
            SessionSnapshot(live_price=1809.0, session_open=1807.8, session_high=1812.0, session_low=1804.0),
        )
        counter_text = "\n".join(counter_lines)
        for token in ["R5 đã phát FLIP_HINT", "LONG ngược nhịp", "hệ vẫn xác nhận hướng chính là SHORT", "điểm", "không tự động đảo sang SHORT"]:
            if token not in counter_text:
                raise AssertionError(f"Countertrend thiếu {token}:\n{counter_text}")

        general_questions = [
            "Kèo hiện tại là gì?",
            "Kèo đó target bao nhiêu?",
            "Có cảnh báo gì?",
            "Backtest cảnh báo này thế nào?",
            "Tại sao kèo này còn dùng được?",
        ]
        answers = []
        for q in general_questions:
            reply = advisor.ask(q, session_id="general")
            assert_customer_answer(reply.text, q)
            answers.append({"q": q, "intent": reply.intent, "answer": reply.text})

        # Hai câu khách vừa phản ánh: câu sau phải tách đúng Open và giá hiện tại.
        first = advisor.ask("giá đang 1809 nên làm gì", session_id="price_case")
        assert_customer_answer(first.text, "price 1809")
        if "Giá 1.809,0" not in first.text or "chờ hồi lên" not in first.text or "không SHORT đuổi" not in first.text:
            raise AssertionError(f"Câu 1809 chưa đúng trọng tâm:\n{first.text}")

        second = advisor.ask(
            "giá mở cửa 1807.8 giá hiện tại 1809 nên làm gì",
            session_id="price_case",
            live_price=1810,      # giả lập web còn giữ giá cũ
            session_open=1810,    # giả lập web còn giữ Open cũ
        )
        assert_customer_answer(second.text, "open 1807.8 live 1809")
        required = ["Giá 1.809,0", "O 1.807,8", "đứng ngoài", "không SHORT đuổi"]
        missing_text = [x for x in required if x not in second.text]
        if missing_text:
            raise AssertionError(f"Thiếu nội dung {missing_text}:\n{second.text}")
        if "mở cửa 1.809,0" in second.text:
            raise AssertionError(f"Parser vẫn lấy số cuối làm Open:\n{second.text}")
        mem = second.structured.get("memory", {})
        if mem.get("last_live_price") != 1809.0:
            raise AssertionError(f"Memory live sai: {mem}")
        stored = advisor.memory_store.load("price_case")
        if stored.last_snapshot.get("session_open") != 1807.8:
            raise AssertionError(f"Memory Open sai: {stored.last_snapshot}")

        # Câu đơn giữ ngắn; câu phức hợp có thể mở rộng nhưng không quá 7 dòng.
        for q in ["nói ngắn thôi", "nói kỹ hơn"]:
            reply = advisor.ask(q, session_id="price_case")
            assert_customer_answer(reply.text, q)
            if not (3 <= reply.structured.get("max_answer_lines", 0) <= 7):
                raise AssertionError("max_answer_lines không nằm trong 3..7")

        print(json.dumps(answers, ensure_ascii=False, indent=2))
        print("CASE 1:\n" + first.text)
        print("CASE 2:\n" + second.text)
    print("SELFTEST PASS: adaptive 3..7 dòng, tách Open/live, hỗ trợ countertrend có điều kiện, không phun jargon nội bộ.")


if __name__ == "__main__":
    main()

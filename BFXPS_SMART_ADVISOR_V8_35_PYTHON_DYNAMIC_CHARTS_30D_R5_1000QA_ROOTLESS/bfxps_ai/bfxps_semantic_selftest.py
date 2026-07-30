from __future__ import annotations

from bfxps_customer_bridge import SessionSnapshot
from bfxps_smart_advisor import SmartAdvisor


def main() -> None:
    advisor = SmartAdvisor.__new__(SmartAdvisor)
    context = {
        "active_plans": [
            {"engine": "gpt_simcarrry6", "profile": "", "horizon": "T+1", "direction": "SHORT", "operational_entry": 1835.0, "operational_target": 1811.1},
            {"engine": "engine5", "profile": "A", "horizon": "T+1", "direction": "SHORT", "operational_entry": 1824.5, "operational_target": 1799.8},
            {"engine": "gpt_simptkt", "profile": "", "horizon": "T+1", "direction": "SHORT", "operational_entry": 1816.2, "operational_target": 1791.5},
        ],
        "consensus": {"direction": "SHORT", "is_unanimous": True, "count": 3},
        "warnings": [],
        "r5_control": {"current_action": "FLIP_HINT", "max_position": 0.3},
    }
    snapshot = SessionSnapshot(as_of=None, live_price=1809.0, session_open=1807.8, session_high=1810.0, session_low=1803.0)
    equivalent_long = [
        "Cho tao kèo ngược hệ, tao ko thích SHORT hôm nay.",
        "cho tao kèo long lên điểm chờ short",
        "long thì sao",
    ]
    normalized = []
    for q in equivalent_long:
        intent = advisor._classify_intent(q)
        if intent == "COUNTERTREND_PLAN":
            lines = advisor._answer_countertrend_plan(context, None, q, snapshot)
        elif intent == "SIDE_PLAN":
            lines = advisor._answer_side_plan(context, None, q, snapshot)
        else:
            raise AssertionError((q, intent))
        text = "\n".join(advisor._limit_answer_lines(lines, max_lines=7))
        for token in [
            "LONG ngược nhịp", "khởi đầu 0,10 và tối đa 0,30 vị thế", "1.816,2 → 1.824,5 → 1.835,0",
            "1.799,8", "1.791,5", "reclaim được Open 1.807,8",
        ]:
            if token not in text:
                raise AssertionError(f"{q}: thiếu {token}\n{text}")
        normalized.append(text)
    if len(set(normalized)) != 1:
        raise AssertionError("Các cách hỏi LONG tương đương chưa cho cùng logic")

    context["r5_control"] = {"current_action": "KEEP", "max_position": 0.3}
    q = "short thì sao"
    if advisor._classify_intent(q) != "SIDE_PLAN":
        raise AssertionError("Không nhận SIDE_PLAN cho SHORT")
    text = "\n".join(advisor._answer_side_plan(context, None, q, snapshot))
    for token in ["cùng hướng kèo chính", "không SHORT đuổi", "đợi hồi lên entry"]:
        if token not in text:
            raise AssertionError(f"SHORT thiếu {token}\n{text}")

    # Đối xứng khi database toàn hệ LONG: SHORT phải thành kèo ngược nhịp, LONG là kèo chính.
    long_context = {
        "active_plans": [
            {"engine": "gpt_simcarrry6", "profile": "", "horizon": "t+1", "direction": "LONG", "operational_entry": 1800.0, "operational_target": 1825.0},
            {"engine": "engine5", "profile": "A", "horizon": "t+1", "direction": "LONG", "operational_entry": 1808.0, "operational_target": 1832.0},
        ],
        "consensus": {"direction": "LONG", "is_unanimous": True, "count": 2},
        "warnings": [],
        "r5_control": {"current_action": "FLIP_HINT", "max_position": 0.3},
    }
    long_snapshot = SessionSnapshot(as_of=None, live_price=1815.0, session_open=1812.0, session_high=1818.0, session_low=1809.0)
    text = "\n".join(advisor._answer_side_plan(long_context, None, "short thì sao", long_snapshot))
    for token in ["SHORT ngược nhịp", "hệ vẫn xác nhận hướng chính là LONG", "khởi đầu 0,10 và tối đa 0,30 vị thế", "không tự động đảo sang LONG"]:
        if token not in text:
            raise AssertionError(f"Đối xứng LONG thiếu {token}\n{text}")
    long_context["r5_control"] = {"current_action": "KEEP", "max_position": 0.3}
    text = "\n".join(advisor._answer_side_plan(long_context, None, "long thì sao", long_snapshot))
    for token in ["LONG là cùng hướng kèo chính", "không LONG đuổi"]:
        if token not in text:
            raise AssertionError(f"Kèo chính LONG thiếu {token}\n{text}")

    print("SEMANTIC SELFTEST PASS: SHORT/LONG đối xứng")


if __name__ == "__main__":
    main()

# Forecast chart/range intents must not fall back to generic current-plan template.
def _test_forecast_intents(advisor):
    cases = {
        "cho tao cái chart forecast": "FORECAST_CHART",
        "dự báo biên hnay ntn": "FORECAST_RANGE",
        "biên hôm nay thế nào": "FORECAST_RANGE",
    }
    for q, expected in cases.items():
        got = advisor._classify_intent(q)
        if got != expected:
            raise AssertionError((q, got, expected))

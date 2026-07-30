from __future__ import annotations
from bfxps_customer_bridge import SessionSnapshot
from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor

NATURAL_QUALITY_QUESTIONS = [
    "hôm nay sao",
    "nay thế nào",
    "kèo ổn không",
    "kèo này có sạch không",
    "có nên đánh không",
    "đánh giá kèo hôm nay",
    "hôm nay có gì đáng ngại",
    "các hệ cùng short thì có chắc ăn không",
]

def main() -> None:
    paths = resolve_runtime_paths(root='.', require_inputs=True)
    advisor = SmartAdvisor(paths.trades, warning_catalog_path=paths.warning_catalog, policy_path=paths.policy)
    for idx, question in enumerate(NATURAL_QUALITY_QUESTIONS, 1):
        reply = advisor.ask(question, session_id=f'decision-natural-{idx}')
        required = ["Cảnh báo cụ thể:", "cặp giá gốc tự mâu thuẫn", "chỉ SHORT khi"]
        missing = [token for token in required if token not in reply.text]
        if missing:
            raise AssertionError(f"Natural warning miss {question}: {missing}\n{reply.text}")

    reply = advisor.ask("giá đang 1809 có nên short không", session_id='decision-price')
    for token in ["không SHORT đuổi", "đứng ngoài", "không phải đã thắng"]:
        if token not in reply.text:
            raise AssertionError(f"Price decision missing {token}\n{reply.text}")

    fake_plan = {
        "date":"28/07/2026", "engine":"gpt_simcarrry6", "horizon":"t+1", "direction":"LONG",
        "original_entry":1835.0, "original_target":1811.1,
        "entry_target_swap_applied":True, "operational_entry":1811.1, "operational_target":1835.0,
    }
    fake_warning = {
        "id":"BAND_DIRECTION_CONTRADICTION", "level":"CRITICAL", "direction":"LONG",
        "plan_key":["28/07/2026","gpt_simcarrry6","t+1"],
        "evidence_metrics":{
            "full":{"raw_touched_trades":1005,"raw_wr_pct":39.1045,"raw_pnl_points":-4061.5864},
            "splits":[
                {"period":"TRAIN 2018-2022","raw_pnl_points":-1901.2314},
                {"period":"OOS1 2023-2024","raw_pnl_points":-933.5891},
                {"period":"OOS2 2025-2026","raw_pnl_points":-1226.7659},
            ],
        },
    }
    ctx={"active_plans":[fake_plan],"warnings":[fake_warning]}
    text="\n".join(advisor._dominant_warning_lines(ctx, fake_plan, SessionSnapshot(), evidence=True, action=True))
    for token in ["bản gốc bảo LONG", "đỉnh kỳ vọng 1.811,1", "LONG đã sửa: chờ giá chạm 1.811,1"]:
        if token not in text:
            raise AssertionError(f"LONG symmetry missing {token}\n{text}")
    print("DECISION SELFTEST PASS: natural paraphrases + SHORT/LONG warning symmetry")

if __name__ == '__main__':
    main()

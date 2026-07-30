from __future__ import annotations

import tempfile
from pathlib import Path

from bfxps_customer_bridge import SessionSnapshot
from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor


def require(text: str, tokens: list[str], label: str) -> None:
    missing = [token for token in tokens if token not in text]
    if missing:
        raise AssertionError(f"{label}: missing {missing}\n{text}")


def forbid(text: str, tokens: list[str], label: str) -> None:
    found = [token for token in tokens if token in text]
    if found:
        raise AssertionError(f"{label}: leaked jargon {found}\n{text}")


def warning(direction: str, key: list[str]) -> dict:
    return {
        "id": "BAND_DIRECTION_CONTRADICTION",
        "level": "CRITICAL",
        "direction": direction,
        "plan_key": key,
        "evidence_metrics": {
            "full": {
                "raw_touched_trades": 1005,
                "raw_wr_pct": 39.1045,
                "raw_pnl_points": -4061.5864,
                "swap_operational_pnl_points": 1637.9134,
            },
            "splits": [
                {"period": "TRAIN 2018-2022", "raw_pnl_points": -1901.2314, "swap_operational_pnl_points": 718.7607},
                {"period": "OOS1 2023-2024", "raw_pnl_points": -933.5891, "swap_operational_pnl_points": 144.8849},
                {"period": "OOS2 2025-2026", "raw_pnl_points": -1226.7659, "swap_operational_pnl_points": 774.2678},
            ],
        },
    }


def main() -> None:
    paths = resolve_runtime_paths(root=".", require_inputs=True)
    with tempfile.TemporaryDirectory(prefix="bfxps_plain_") as td:
        advisor = SmartAdvisor(
            paths.trades,
            warning_catalog_path=paths.warning_catalog,
            policy_path=paths.policy,
            memory_path=Path(td) / "memory.json",
        )

        natural = advisor.ask(
            "O 1807,8 H 1833 L 1804 P 1828, giờ làm gì?",
            session_id="natural",
        ).text
        require(natural, ["Kèo hệ đang xét", "size nền", "Quyền R5:", "OHLC khách cung cấp", "Chưa SHORT"], "natural")
        forbid(natural, ["ExpectedLow", "ExpectedHigh", "TRAIN", "OOS1", "OOS2", "hình học gốc"], "natural")

        short_plan = {
            "date": "28/07/2026",
            "engine": "gpt_simcarrry6",
            "horizon": "t+1",
            "direction": "SHORT",
            "original_entry": 1811.1,
            "original_target": 1827.3,
            "entry_target_swap_applied": True,
            "operational_entry": 1827.3,
            "operational_target": 1811.1,
        }
        short_key = ["28/07/2026", "gpt_simcarrry6", "t+1"]
        short_ctx = {
            "active_plans": [short_plan],
            "warnings": [warning("SHORT", short_key)],
            "r5_control": {"current_action": "PRE_OPEN", "max_position": 0.3},
        }
        snapshot = SessionSnapshot(live_price=1828.0, session_open=1807.8, session_high=1833.0, session_low=1804.0)
        action = "\n".join(advisor._intraday_action_lines(short_ctx, short_plan, snapshot, requested_side="SHORT"))
        require(action, ["OHLC đang vi phạm đúng điều kiện kích hoạt", "giá hiện tại 1.828,0 vẫn nằm trên", "mất 1.827,3"], "short-ohlc")
        definition = "\n".join(advisor._dominant_warning_lines(short_ctx, short_plan, snapshot, evidence=False, action=True))
        require(definition, ["bản gốc bảo SHORT", "đáy kỳ vọng 1.827,3", "cao hơn giá tham chiếu 1.811,1", "không được dùng cặp giá gốc"], "short-definition")
        forbid(definition, ["ExpectedLow", "TRAIN", "OOS"], "short-definition")

        long_plan = {
            "date": "28/07/2026",
            "engine": "gpt_simcarrry6",
            "horizon": "t+1",
            "direction": "LONG",
            "original_entry": 1835.0,
            "original_target": 1811.1,
            "entry_target_swap_applied": True,
            "operational_entry": 1811.1,
            "operational_target": 1835.0,
        }
        long_key = ["28/07/2026", "gpt_simcarrry6", "t+1"]
        long_ctx = {
            "active_plans": [long_plan],
            "warnings": [warning("LONG", long_key)],
            "r5_control": {"current_action": "PRE_OPEN", "max_position": 0.3},
        }
        long_snapshot = SessionSnapshot(live_price=1809.0, session_open=1815.0, session_high=1818.0, session_low=1804.0)
        long_action = "\n".join(advisor._intraday_action_lines(long_ctx, long_plan, long_snapshot, requested_side="LONG"))
        require(long_action, ["OHLC đang vi phạm đúng điều kiện kích hoạt", "giá hiện tại 1.809,0 vẫn nằm dưới", "lấy lại 1.811,1"], "long-ohlc")
        long_definition = "\n".join(advisor._dominant_warning_lines(long_ctx, long_plan, long_snapshot, evidence=False, action=True))
        require(long_definition, ["bản gốc bảo LONG", "đỉnh kỳ vọng 1.811,1", "thấp hơn giá tham chiếu 1.835,0"], "long-definition")

        evidence = advisor._focused_evidence(short_ctx, short_plan)
        evidence_text = "\n".join(evidence)
        require(evidence_text, ["giai đoạn xây dựng 2018–2022", "kiểm tra độc lập 2023–2024", "kiểm tra độc lập 2025–2026"], "evidence")
        forbid(evidence_text, ["TRAIN", "OOS1", "OOS2", "ExpectedLow"], "evidence")

    print("PLAIN LANGUAGE OHLC SELFTEST PASS: định nghĩa rõ cảnh báo, OHLC nói đúng điều kiện bị phá, đối xứng LONG/SHORT")


if __name__ == "__main__":
    main()

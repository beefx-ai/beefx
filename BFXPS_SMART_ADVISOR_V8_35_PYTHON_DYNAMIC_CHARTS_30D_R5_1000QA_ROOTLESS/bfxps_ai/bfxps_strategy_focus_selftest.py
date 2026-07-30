from pathlib import Path

from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor


def need(text: str, required: list[str], label: str) -> None:
    missing = [x for x in required if x not in text]
    if missing:
        raise AssertionError(f"{label} missing {missing}\n{text}")


def reject(text: str, forbidden: list[str], label: str) -> None:
    found = [x for x in forbidden if x in text]
    if found:
        raise AssertionError(f"{label} contains forbidden {found}\n{text}")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = resolve_runtime_paths(root=root)
    advisor = SmartAdvisor(
        trades_path=paths.trades,
        ohlc_path=paths.ohlc if paths.ohlc.exists() else None,
        warning_catalog_path=paths.warning_catalog,
        policy_path=paths.policy,
        memory_path=None,
    )

    system = advisor.ask("kèo của hệ đang là gì, khối lượng đi ntn trong kb nào thì đúng", session_id="strategy_system").text
    need(system, ["Kèo chính hôm nay", "Khối lượng:", "R5/Engine5", "Kịch bản đúng"], "system playbook")
    reject(system, ["Trong dữ liệu nội bộ, tình huống này được gắn nhãn V44"], "system playbook")

    carry = advisor.ask("simcary đánh cụ thể ntn", session_id="strategy_carry").text
    need(carry, ["SIMCARRRY6 t+1", "Khối lượng:", "Quyền R5:", "Kịch bản đúng"], "simcary aliases")

    ptkt = advisor.ask("simptkt đánh cụ thể ntn", session_id="strategy_ptkt").text
    need(ptkt, ["không có plan active của SimPTKT", "không lấy kèo của engine khác"], "missing SimPTKT")
    reject(ptkt, ["vùng vào 1.835,0 → mốc chốt 1.811,1"], "missing SimPTKT")

    accepted = advisor.ask("O 1807,8 H 1840 L 1804 P 1832, simcary đánh cụ thể ntn", session_id="strategy_ohlc_accept").text
    need(accepted, ["SIMCARRRY6 t+1", "OHLC đã thỏa điều kiện kích hoạt SHORT", "mốc chốt"], "OHLC accepted branch")

    rejected = advisor.ask("O 1807,8 H 1840 L 1804 P 1838, simcary đánh cụ thể ntn", session_id="strategy_ohlc_reject").text
    need(rejected, ["SIMCARRRY6 t+1", "Chưa SHORT", "giá hiện tại 1.838,0 vẫn nằm trên"], "OHLC rejected branch")

    print("STRATEGY FOCUS SELFTEST PASS: system/engine focus, volume ladder, absent-engine honesty, OHLC secondary routing")


if __name__ == "__main__":
    main()

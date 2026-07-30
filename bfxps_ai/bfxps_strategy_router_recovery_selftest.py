from pathlib import Path

from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor


def need(text: str, tokens: list[str], label: str) -> None:
    if not text.strip():
        raise AssertionError(f"{label}: empty answer")
    missing = [x for x in tokens if x not in text]
    if missing:
        raise AssertionError(f"{label}: missing {missing}\n{text}")


class BlankComposeAdvisor(SmartAdvisor):
    def _compose(self, *args, **kwargs):
        return "", {}


class ErrorComposeAdvisor(SmartAdvisor):
    def _compose(self, *args, **kwargs):
        raise RuntimeError("forced compose failure")


def build(cls=SmartAdvisor):
    paths = resolve_runtime_paths(root=Path(__file__).resolve().parents[1])
    return cls(
        paths.trades,
        ohlc_path=paths.ohlc if paths.ohlc.exists() else None,
        warning_catalog_path=paths.warning_catalog,
        policy_path=paths.policy,
        memory_path=None,
    )


def main() -> None:
    advisor = build()
    cases = [
        ("kèo sao", "SYSTEM_PLAYBOOK", ["Kèo chính hôm nay", "size" if False else "Khối lượng:", "R5/Engine5"]),
        ("cập nhật kèo hnay đi", "SYSTEM_PLAYBOOK", ["Kèo chính hôm nay", "Kịch bản đúng"]),
        ("kèo của hệ đang là gì, khối lượng đi ntn trong kb nào thì đúng", "SYSTEM_PLAYBOOK", ["Khối lượng:", "Kịch bản đúng"]),
        ("simcary đánh cụ thể ntn", "ENGINE_PLAYBOOK", ["SIMCARRRY6 t+1", "Khối lượng:", "Quyền R5:"]),
        ("simptkt đánh cụ thể ntn", "ENGINE_PLAYBOOK", ["không có plan active của SimPTKT", "không lấy kèo của engine khác"]),
        ("O 1807,8 H 1833 L 1804 P 1828, giờ làm gì?", "SCENARIO", ["Kèo hệ đang xét", "OHLC khách cung cấp", "Chưa SHORT", "phiên chưa chạm entry"]),
        ("O 1807,8 H 1840 L 1804 P 1838, giờ làm gì?", "SCENARIO", ["OHLC đang vi phạm đúng điều kiện kích hoạt", "không SHORT", "kèo SHORT bị vô hiệu"]),
    ]
    for i, (question, intent, tokens) in enumerate(cases, 1):
        reply = advisor.ask(question, session_id=f"router-{i}")
        if reply.intent != intent:
            raise AssertionError(f"{question}: intent={reply.intent}, expected={intent}\n{reply.text}")
        need(reply.text, tokens, question)

    blank = build(BlankComposeAdvisor).ask("kèo sao", session_id="blank")
    need(blank.text, ["Kèo chính hôm nay"], "blank compose recovery")
    if not blank.structured.get("empty_answer_recovered"):
        raise AssertionError("blank compose did not set recovery marker")

    errored = build(ErrorComposeAdvisor).ask("kèo sao", session_id="error")
    need(errored.text, ["Kèo chính hôm nay"], "exception compose recovery")
    if "internal_error" not in errored.structured:
        raise AssertionError("exception compose did not preserve internal error marker")

    server = (Path(__file__).resolve().parent / "bfxps_server.py").read_text(encoding="utf-8")
    need(server, ["data.answer||data.error", "Hệ không trả được nội dung", "RECOVERY_ERROR"], "web no-blank contract")
    print("STRATEGY ROUTER RECOVERY SELFTEST PASS: intent routing, engine isolation, OHLC focus, no-empty planner/API/UI")


if __name__ == "__main__":
    main()

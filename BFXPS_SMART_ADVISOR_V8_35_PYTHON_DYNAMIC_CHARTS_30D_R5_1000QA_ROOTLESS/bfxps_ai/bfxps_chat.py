from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bfxps_smart_advisor import SmartAdvisor
from bfxps_runtime import describe, resolve_runtime_paths

APP_VERSION = "V8.35"
APP_NAME = f"BFXPS Smart Advisor {APP_VERSION}+++ Python Dynamic Charts 30D"


def build_advisor(args: argparse.Namespace) -> tuple[SmartAdvisor, object]:
    paths = resolve_runtime_paths(
        root=args.root,
        trades=args.trades,
        ohlc=args.ohlc,
        warning_catalog=args.warning_catalog,
        policy=args.policy,
    )
    memory = Path(args.memory).expanduser() if args.memory else paths.chat_memory
    if not memory.is_absolute():
        memory = paths.root / memory
    return SmartAdvisor(
        trades_path=paths.trades,
        ohlc_path=paths.ohlc if paths.ohlc.exists() else None,
        history_trades_path=paths.history_trades,
        warning_catalog_path=paths.warning_catalog,
        memory_path=memory,
        policy_path=paths.policy,
    ), paths

def main() -> None:
    p = argparse.ArgumentParser(description=APP_NAME)
    p.add_argument("--root", help="BFXPS root; mặc định tự nhận từ vị trí bfxps_ai")
    p.add_argument("--trades", help="Mặc định outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv")
    p.add_argument("--ohlc")
    p.add_argument("--warning-catalog")
    p.add_argument("--policy")
    p.add_argument("--memory", help="Mặc định bfxps_ai/runtime/chat_memory.json")
    p.add_argument("--session", default="default")
    p.add_argument("--as-of")
    p.add_argument("--open", dest="session_open", type=float)
    p.add_argument("--high", dest="session_high", type=float)
    p.add_argument("--low", dest="session_low", type=float)
    p.add_argument("--price", dest="live_price", type=float)
    p.add_argument("--question")
    p.add_argument("--json", action="store_true")
    p.add_argument("--payload", action="store_true", help="Xuất payload đầy đủ để nối LLM ngoài")
    args = p.parse_args()

    advisor, paths = build_advisor(args)
    kwargs = {
        "session_id": args.session,
        "as_of": args.as_of,
        "live_price": args.live_price,
        "session_open": args.session_open,
        "session_high": args.session_high,
        "session_low": args.session_low,
    }

    if args.question:
        if args.payload:
            print(json.dumps(advisor.make_llm_payload(args.question, **kwargs), ensure_ascii=False, indent=2, default=str))
            return
        reply = advisor.ask(args.question, **kwargs)
        if args.json:
            print(json.dumps({"answer": reply.text, **reply.structured}, ensure_ascii=False, indent=2, default=str))
        else:
            print(reply.text)
        return

    print(f"{APP_NAME}. Trả lời theo database mới nhất. Gõ 'exit' để thoát.")
    print(describe(paths))
    while True:
        try:
            question = input("\nKhách> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nĐã thoát.")
            break
        if not question:
            continue
        if question.lower() in {"exit", "quit", "thoát"}:
            break
        if question == "/state":
            memory = advisor.memory_store.load(args.session)
            print(json.dumps(memory.to_dict(), ensure_ascii=False, indent=2))
            continue
        try:
            reply = advisor.ask(question, **kwargs)
            print("\nBFXPS AI> " + reply.text.replace("\n", "\n"))
        except Exception as exc:
            print(f"\n[ERROR] {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()

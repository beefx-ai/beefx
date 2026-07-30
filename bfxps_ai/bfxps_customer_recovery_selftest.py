from __future__ import annotations

import csv
import re
import tempfile
from pathlib import Path

from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor


def require(text: str, tokens: list[str], label: str) -> None:
    missing = [t for t in tokens if t not in text]
    if missing:
        raise AssertionError(f"{label}: missing {missing}\n{text}")


def forbid(text: str, tokens: list[str], label: str) -> None:
    bad = [t for t in tokens if t in text]
    if bad:
        raise AssertionError(f"{label}: forbidden {bad}\n{text}")


def make_variant(src: Path, dst: Path, action: str) -> None:
    rows = []
    with src.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fields = reader.fieldnames or []
        for row in reader:
            if row.get("RowKind") == "FORWARD" and "engine5" in row.get("EngineChartLabel", ""):
                row["R5Action"] = action
                note = row.get("Ghi chú HybridV3", "")
                note += f"; R5Overlay={action}; Units={'0' if action == 'CANCEL' else '0.3'}"
                row["Ghi chú HybridV3"] = note
            rows.append(row)
    with dst.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def make_long_flip(src: Path, dst: Path) -> None:
    rows = []
    with src.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fields = reader.fieldnames or []
        for row in reader:
            if row.get("RowKind") == "FORWARD":
                row["Loại lệnh"] = "LONG"
                row["Entry"] = "1811.1"
                row["Exit"] = "1835.0"
                row["Forecast"] = "1835.0"
                note = row.get("Ghi chú HybridV3", "")
                replacements = {
                    "OriginalEntry": "1835.0000",
                    "OriginalTarget": "1811.1000",
                    "OperationalEntry": "1811.1000",
                    "OperationalTarget": "1835.0000",
                }
                for key, value in replacements.items():
                    if re.search(fr"{key}=[^;]*", note):
                        note = re.sub(fr"{key}=[^;]*", f"{key}={value}", note)
                    else:
                        note += f"; {key}={value}"
                if "engine5" in row.get("EngineChartLabel", ""):
                    row["R5Action"] = "FLIP_HINT"
                    note += "; R5Overlay=FLIP_HINT; Units=0.3"
                row["Ghi chú HybridV3"] = note
            rows.append(row)
    with dst.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def advisor(trades: Path, root: Path, memory: Path) -> SmartAdvisor:
    return SmartAdvisor(
        trades,
        warning_catalog_path=root / "bfxps_ai/config/backtested_warning_catalog.json",
        policy_path=root / "bfxps_ai/config/advisor_policy.json",
        memory_path=memory,
    )


def main() -> None:
    paths = resolve_runtime_paths(root=".", require_inputs=True)
    root = paths.root
    with tempfile.TemporaryDirectory(prefix="bfxps_recovery_") as td_raw:
        td = Path(td_raw)

        # PRE_OPEN real-data dialogue: price and entry must never be confused.
        a = advisor(paths.trades, root, td / "pre.json")
        r = a.ask(
            "O 1807,8 H 1842 L 1804 P 1840, mày bảo SHORT 1835 mà nó vượt mạnh rồi, sai rồi còn làm gì?",
            session_id="pre",
        )
        require(r.text, ["kèo vào tại 1.835,0 đã bị vô hiệu", "không LONG đuổi", "retest 1.835,0", "1.844,3"], "pre-blame")
        r = a.ask("tại mày tao SHORT 1835 đang lỗ, xử lý đi", session_id="pre")
        require(r.text, ["giá 1.840,0", "SHORT từ 1.835,0", "bất lợi khoảng 5,0 điểm", "không bình quân"], "entry-not-live")
        forbid(r.text, ["giá hiện lùi xuống 1.835,0", "có thể canh SHORT"], "entry-not-live")
        r = a.ask("giờ LONG đuổi được không?", session_id="pre")
        require(r.text, ["Không LONG đuổi tại 1.840,0", "chờ retest 1.835,0", "FLIP_HINT"], "no-chase")
        r = a.ask("nếu giá rơi lại 1835 thì sao?", session_id="pre")
        require(r.text, ["chưa tự động là kèo SHORT", "retest không lấy lại được", "breakout còn hiệu lực"], "conditional-entry")
        r = a.ask("nếu vượt tiếp 1844,3 thì sao?", session_id="pre")
        require(r.text, ["không SHORT và không LONG đuổi", "chốt bớt tại 1.844,3", "nếu chưa có vị thế thì chờ retest"], "next-level")

        # R5 authority matrix after an invalidated SHORT.
        expectations = {
            "KEEP": ["đóng/giảm về 0", "R5 vẫn KEEP SHORT", "không tự đảo LONG"],
            "CANCEL": ["đóng/giảm về 0", "R5 đang CANCEL/units 0", "NO TRADE"],
            "FLIP_HINT": ["đóng/giảm về 0", "R5 đã FLIP_HINT", "không LONG đuổi", "1.844,3"],
        }
        for action, tokens in expectations.items():
            variant = td / f"{action}.tsv"
            make_variant(paths.trades, variant, action)
            adv = advisor(variant, root, td / f"{action}.json")
            out = adv.ask(
                "O 1807,8 H 1842 L 1804 P 1840, tao SHORT 1835 đang lỗ, mày sai rồi xử lý đi",
                session_id=action,
            ).text
            require(out, tokens, f"r5-{action.lower()}")

        # Mirror: frozen LONG breaks down; FLIP_HINT may only authorize a retested SHORT.
        long_tsv = td / "LONG_FLIP.tsv"
        make_long_flip(paths.trades, long_tsv)
        lf = advisor(long_tsv, root, td / "long.json")
        out = lf.ask(
            "O 1835 H 1838 L 1804 P 1807, tao LONG 1811,1 đang lỗ, mày sai rồi xử lý đi",
            session_id="long",
        ).text
        require(out, ["entry LONG 1.811,1", "đã bị vô hiệu", "đóng/giảm về 0", "R5 đã FLIP_HINT", "không SHORT đuổi"], "mirror-long")

    print("CUSTOMER RECOVERY SELFTEST PASS: accountability, entry/live separation, no revenge trade, R5 authority, mirror symmetry")


if __name__ == "__main__":
    main()

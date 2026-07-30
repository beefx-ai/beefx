from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

CORE_COLUMNS = [
    "STT", "Ngày entry", "Loại lệnh", "Entry", "Exit", "PNL points",
    "Win/Loss", "Ghi chú HybridV3", "Forecast", "AlgoCheck",
    "EntryAlgoV2Check", "So sánh OHLC", "Độ khớp"
]

BRIDGE_VERSION = "BFXPS-13COL-3.2"
DEFAULT_WARNING_CATALOG = Path(__file__).resolve().parents[1] / "config" / "backtested_warning_catalog.json"
SEVERITY_ORDER = {"BLOCKER": 0, "CRITICAL": 1, "HIGH": 2, "MEDIUM": 3, "NOTICE": 4, "INFO": 5}

ENGINE_ROLES = {
    "gpt_simcarrry6": "kế hoạch operational theo forecast band và ladder",
    "gpt_simptkt": "xác nhận PTKT độc lập",
    "engine5": "engine giao dịch/risk overlay",
}


@dataclass
class SessionSnapshot:
    as_of: str | None = None
    live_price: float | None = None
    session_open: float | None = None
    session_high: float | None = None
    session_low: float | None = None
    session_close: float | None = None
    is_completed_bar: bool = False
    input_source: str = ""


def _read_table(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    sep = "\t" if path.suffix.lower() == ".tsv" else ","
    df = pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    missing = [c for c in CORE_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Thiếu cột bắt buộc: {missing}")
    return df.copy()


def parse_note(note: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in str(note).split(";"):
        token = token.strip()
        if "=" in token:
            key, value = token.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def row_kind(row: pd.Series) -> str:
    meta = parse_note(row.get("Ghi chú HybridV3", ""))
    declared = meta.get("RowKind", "").upper()
    if declared in {"HISTORY", "FORWARD"}:
        return declared
    winloss = str(row.get("Win/Loss", "")).upper()
    note = str(row.get("Ghi chú HybridV3", "")).upper()
    if winloss.startswith("PRE") or "PHASE=PRE" in note or "SIM_FORWARD" in note:
        return "FORWARD"
    return "HISTORY"


def _num(value: Any) -> float | None:
    try:
        s = str(value).strip().replace(",", "")
        if not s or s.lower() == "nan":
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    s = str(value).strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _parse_date(value: str) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, dayfirst=True, errors="coerce")
    return None if pd.isna(ts) else ts.normalize()


def _fmt(x: float | None, digits: int = 1) -> str:
    if x is None:
        return "NA"
    return f"{x:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def normalize_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        meta = parse_note(r.get("Ghi chú HybridV3", ""))
        engine = (
            meta.get("InterfaceEngine")
            or meta.get("Engine")
            or str(r.get("EngineChartLabel", ""))
        )
        horizon = meta.get("InterfaceHorizon") or meta.get("Horizon", "")
        action_col = str(r.get("So sánh OHLC", ""))
        risk_action = meta.get("RiskAction", "")
        if not risk_action and action_col.upper().startswith(("TAKE_", "KEEP", "REDUCE", "BOOST")):
            risk_action = action_col
        target_rule = meta.get("target_rule", "")
        original_entry = _num(meta.get("OriginalEntry", ""))
        original_target = _num(meta.get("OriginalTarget", ""))
        operational_entry = _num(meta.get("OperationalEntry", "")) or _num(r.get("Entry"))
        operational_target = _num(meta.get("OperationalTarget", "")) or _num(r.get("Exit"))
        direct_r5_action = str(r.get("R5Action", "")).strip()
        r5_action = meta.get("R5Overlay", "") or direct_r5_action
        phase = meta.get("Phase", "") or meta.get("DirectionMode", "")
        if not phase and (row_kind(r) == "FORWARD"):
            phase = "PRE_OHLCV" if not _bool(r.get("OHLCV_Available", "")) else "POST_OHLCV"
        rows.append({
            "date": str(r.get("Ngày entry", "")),
            "date_ts": _parse_date(str(r.get("Ngày entry", ""))),
            "kind": row_kind(r),
            "engine": engine,
            "engine_role": ENGINE_ROLES.get(engine, "nguồn tín hiệu của hệ"),
            "profile": meta.get("Profile", ""),
            "horizon": horizon,
            "direction": str(r.get("Loại lệnh", "")).upper(),
            "entry": _num(r.get("Entry")),
            "exit": _num(r.get("Exit")),
            "exit_mode": str(r.get("Exit", "")),
            "pnl_points": _num(r.get("PNL points")),
            "win_loss": str(r.get("Win/Loss", "")),
            "forecast": _num(r.get("Forecast")),
            "algo_check": str(r.get("AlgoCheck", "")),
            "entry_check": str(r.get("EntryAlgoV2Check", "")),
            "action_or_outcome": action_col,
            "match_score": _num(r.get("Độ khớp")),
            "risk_action": risk_action,
            "r5_action": r5_action,
            "r5_action_source": "note:R5Overlay" if meta.get("R5Overlay", "") else ("column:R5Action" if direct_r5_action else ""),
            "phase": phase,
            "ohlcv_available": _bool(r.get("OHLCV_Available", "")),
            "units": _num(meta.get("Units", "")),
            "target_rule": target_rule,
            "entry_rule": meta.get("entry_rule", ""),
            "direction_source": meta.get("DirectionSource", ""),
            "target_source_date": meta.get("TargetSourceDate", ""),
            "basis_date": meta.get("Basis") or meta.get("BasisDate") or meta.get("SignalBasisDate", ""),
            "basis_date_ts": _parse_date(meta.get("Basis") or meta.get("BasisDate") or meta.get("SignalBasisDate", "")),
            "frozen_pre_date": meta.get("FrozenPreDate", ""),
            "target_age_sessions": _num(meta.get("TargetAgeSessions", "")),
            "volume_rule": meta.get("VolumeRule", ""),
            "original_entry": original_entry,
            "original_target": original_target,
            "entry_target_swap_applied": _bool(meta.get("EntryTargetSwapApplied", "False")),
            "operational_entry": operational_entry,
            "operational_target": operational_target,
            "is_expected_band": ("EXPECTEDHIGH" in target_rule.upper() or "EXPECTEDLOW" in target_rule.upper()),
            "is_ptkt_atr_fallback": ("PTKT_ATR14_PRIOR_FALLBACK" in target_rule.upper()),
            "raw_note": str(r.get("Ghi chú HybridV3", "")),
        })
    return rows


def _canonical_r5_action(value: Any) -> str:
    s = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not s:
        return ""
    if "CANCEL" in s:
        return "CANCEL"
    if "FLIP_HINT" in s or s == "FLIP" or s.startswith("FLIP_"):
        return "FLIP_HINT"
    if "KEEP" in s:
        return "KEEP"
    return s


def _profile_cap(profile: str) -> float | None:
    m = re.search(r"CAP\s*([0-9]+(?:[._][0-9]+)?)", str(profile or ""), flags=re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace("_", "."))
    except ValueError:
        return None


def summarize_r5_control(active: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [p for p in active if p.get("engine") == "engine5" or p.get("r5_action")]
    explicit = [_canonical_r5_action(p.get("r5_action")) for p in candidates if _canonical_r5_action(p.get("r5_action"))]
    unique = sorted(set(explicit))
    conflict = len(unique) > 1
    if "CANCEL" in unique:
        action = "CANCEL"
    elif "FLIP_HINT" in unique:
        action = "FLIP_HINT"
    elif "KEEP" in unique:
        action = "KEEP"
    elif unique:
        action = unique[0]
    else:
        action = "PRE_OPEN" if any(
            "PRE" in str(p.get("phase", "")).upper() or not p.get("ohlcv_available", False)
            for p in candidates
        ) else "NO_SIGNAL"
    caps = [x for x in (_profile_cap(p.get("profile", "")) for p in candidates) if x is not None]
    cap = min(caps) if caps else None
    last_completed = next((
        _canonical_r5_action(p.get("r5_action"))
        for p in history
        if p.get("engine") == "engine5" and _canonical_r5_action(p.get("r5_action"))
    ), "")
    contract = {
        "PRE_OPEN": "SCENARIO_ONLY",
        "KEEP": "KEEP_FROZEN_NO_AUTO_FLIP",
        "CANCEL": "NO_TRADE_NO_AUTO_REVERSE",
        "FLIP_HINT": "CONDITIONAL_FLIP_CANDIDATE",
        "NO_SIGNAL": "NO_R5_AUTHORITY",
    }.get(action, "UNKNOWN_R5_ACTION")
    return {
        "stage": "PRE_OPEN" if action == "PRE_OPEN" else ("OPEN_OR_LIVE" if action in {"KEEP", "CANCEL", "FLIP_HINT"} else "UNKNOWN"),
        "current_action": action,
        "explicit_action": action in {"KEEP", "CANCEL", "FLIP_HINT"},
        "contract": contract,
        "max_position": cap,
        "profiles": sorted({str(p.get("profile") or "") for p in candidates if p.get("profile")}),
        "source_count": len(candidates),
        "source_actions": unique,
        "conflict": conflict,
        "resolution_rule": "CANCEL > FLIP_HINT > KEEP" if conflict else "single_action_or_stage",
        "last_completed_action": last_completed,
        "rules": [
            "R5 là corrective overlay của kèo frozen, không phải engine độc lập.",
            "PRE_OPEN chỉ dựng kịch bản; không được nói R5 đã KEEP/CANCEL/FLIP_HINT.",
            "CANCEL hoặc units=0 là NO TRADE; không tự biến CANCEL thành lệnh đảo chiều.",
            "FLIP_HINT chỉ là gợi ý đảo có điều kiện, cần Open/intraday xác nhận và size giảm.",
            "KEEP giữ hướng frozen; mọi scalp ngược nếu có chỉ là bridge ngoài kèo chính và phải đóng trước vùng entry hệ.",
        ],
    }


def read_ohlc(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    aliases = {c.lower(): c for c in df.columns}

    def get(*names: str) -> str:
        for name in names:
            if name.lower() in aliases:
                return aliases[name.lower()]
        raise ValueError(f"Thiếu cột OHLC: {names}")

    time_col = get("Timestamp", "Date", "Datetime", "Time")
    out = pd.DataFrame({
        "Timestamp": pd.to_datetime(df[time_col], errors="coerce"),
        "Open": pd.to_numeric(df[get("Open")], errors="coerce"),
        "High": pd.to_numeric(df[get("High")], errors="coerce"),
        "Low": pd.to_numeric(df[get("Low")], errors="coerce"),
        "Close": pd.to_numeric(df[get("Close")], errors="coerce"),
    })
    out = out.dropna().sort_values("Timestamp")
    out["Date"] = out["Timestamp"].dt.normalize()
    if out.empty:
        raise ValueError("OHLC không có dòng hợp lệ")
    return out


def _volume_max(plan: dict[str, Any]) -> float:
    m = re.search(r"MAX_([0-9.]+)", plan.get("volume_rule", ""))
    if m:
        return float(m.group(1))
    risk = str(plan.get("risk_action", "")).upper()
    if "HALF" in risk:
        return 0.5
    return 1.0


def _ladder_fills(plan: dict[str, Any], bar: dict[str, float]) -> list[float]:
    entry = plan.get("operational_entry")
    direction = plan.get("direction")
    if entry is None:
        return []
    max_size = _volume_max(plan)
    steps = max(1, int(round(max_size / 0.1)))
    op, hi, lo = bar["Open"], bar["High"], bar["Low"]
    if direction == "SHORT":
        if op >= entry:
            start = op
        elif hi >= entry:
            start = entry
        else:
            return []
        return [start + i for i in range(steps) if start + i <= hi + 1e-9]
    if direction == "LONG":
        if op <= entry:
            start = op
        elif lo <= entry:
            start = entry
        else:
            return []
        return [start - i for i in range(steps) if start - i >= lo - 1e-9]
    return []


def settle_plan_daily(plan: dict[str, Any], bar: dict[str, float]) -> dict[str, Any]:
    """Settle a plan from daily OHLC. Exact for the documented SIMCARRRY ladder;
    conservative/diagnostic for generic engines because intraday ordering is unavailable.
    """
    direction = plan.get("direction")
    entry = plan.get("operational_entry")
    target = plan.get("operational_target")
    if entry is None:
        return {"status": "NO_ENTRY", "filled": False}

    is_ladder = "PER_1PT" in str(plan.get("volume_rule", ""))
    if is_ladder:
        fills = _ladder_fills(plan, bar)
    else:
        op, hi, lo = bar["Open"], bar["High"], bar["Low"]
        if direction == "SHORT":
            fills = [op if op >= entry else entry] if hi >= entry else []
        elif direction == "LONG":
            fills = [op if op <= entry else entry] if lo <= entry else []
        else:
            fills = []

    if not fills:
        return {"status": "NO_FILL", "filled": False, "pnl_points": 0.0}

    avg_entry = sum(fills) / len(fills)
    size = len(fills) * 0.1 if is_ladder else _volume_max(plan)
    target_hit = False
    if target is not None:
        target_hit = (direction == "SHORT" and bar["Low"] <= target) or (
            direction == "LONG" and bar["High"] >= target
        )
    exit_price = target if target_hit and target is not None else bar["Close"]
    gross = (avg_entry - exit_price) if direction == "SHORT" else (exit_price - avg_entry)
    pnl = gross * size
    return {
        "status": "HIT_TARGET" if target_hit else ("EXIT_CLOSE_WIN" if pnl > 0 else "EXIT_CLOSE_LOSS"),
        "filled": True,
        "fill_price": avg_entry,
        "size": size,
        "exit_price": exit_price,
        "target_hit": target_hit,
        "pnl_points": pnl,
        "diagnostic_only": not is_ladder,
    }


def evaluate_live_plan(plan: dict[str, Any], snap: SessionSnapshot) -> dict[str, Any]:
    entry = plan.get("operational_entry")
    target = plan.get("operational_target")
    direction = plan.get("direction")
    if entry is None:
        return {"state": "NO_ENTRY", "message": "Kế hoạch không có entry hợp lệ."}

    hi = snap.session_high
    lo = snap.session_low
    op = snap.session_open
    live = snap.live_price
    if hi is None and lo is None and op is None:
        if live is None:
            return {"state": "PRE_OPEN", "message": "Chưa có dữ liệu intraday của phiên."}
        if direction == "SHORT" and live < entry:
            return {
                "state": "UNKNOWN_PRIOR_TOUCH",
                "message": f"Giá hiện tại {_fmt(live)} đang dưới entry {_fmt(entry)}, nhưng thiếu High/Open nên chưa thể xác nhận trước đó đã khớp hay chưa.",
            }
        if direction == "LONG" and live > entry:
            return {
                "state": "UNKNOWN_PRIOR_TOUCH",
                "message": f"Giá hiện tại {_fmt(live)} đang trên entry {_fmt(entry)}, nhưng thiếu Low/Open nên chưa thể xác nhận trước đó đã khớp hay chưa.",
            }
        return {"state": "AT_OR_BEYOND_ENTRY", "message": "Giá hiện tại đã ở vùng entry; cần High/Low/Open để xác nhận fill."}

    hi_candidates = [x for x in [op, live, lo] if x is not None]
    lo_candidates = [x for x in [op, live, hi] if x is not None]
    hi = hi if hi is not None else (max(hi_candidates) if hi_candidates else None)
    lo = lo if lo is not None else (min(lo_candidates) if lo_candidates else None)
    if hi is None or lo is None:
        return {"state": "PRE_OPEN", "message": "Chưa đủ dữ liệu intraday để xác nhận fill."}
    filled = (direction == "SHORT" and hi >= entry) or (direction == "LONG" and lo <= entry)
    if not filled:
        open_prepassed_target = bool(
            target is not None
            and op is not None
            and (
                (direction == "SHORT" and op <= target and op < entry)
                or (direction == "LONG" and op >= target and op > entry)
            )
        )
        if open_prepassed_target:
            side_text = "dưới" if direction == "SHORT" else "trên"
            chase_text = "SHORT" if direction == "SHORT" else "LONG"
            return {
                "state": "WAIT_ENTRY_TARGET_PREPASSED",
                "message": (
                    f"Open {_fmt(op)} đã nằm {side_text} target {_fmt(target)} trước khi entry {direction} {_fmt(entry)} được chạm. "
                    f"Tại Open: chưa có vị thế, không {chase_text} đuổi; chỉ chờ giá quay về entry. "
                    "Nếu entry được fill sau đó, target chỉ được công nhận khi giá quay lại target sau thời điểm fill."
                ),
                "no_trade_at_open": True,
                "target_prepassed_before_fill": True,
                "distance_to_entry": abs(entry - op),
                "distance_open_to_target": abs(target - op),
            }
        distance = (entry - (live if live is not None else hi)) if direction == "SHORT" else ((live if live is not None else lo) - entry)
        return {
            "state": "WAIT_ENTRY",
            "message": f"Chưa khớp. Chờ giá chạm entry {_fmt(entry)}; khoảng cách hiện tại khoảng {_fmt(abs(distance))} điểm.",
            "no_trade_at_open": True if op is not None else False,
            "distance_to_entry": abs(distance),
        }

    target_hit = False
    if target is not None:
        target_hit = (direction == "SHORT" and lo <= target) or (direction == "LONG" and hi >= target)
    if target_hit:
        # OHLC range alone cannot establish event order. Be conservative when
        # target was already beyond the open before a later entry touch.
        pre_entry_target_possible = (
            direction == "SHORT" and op is not None and op <= target and hi >= entry
        ) or (
            direction == "LONG" and op is not None and op >= target and lo <= entry
        )
        current_confirms_target = (
            direction == "SHORT" and live is not None and live <= target
        ) or (
            direction == "LONG" and live is not None and live >= target
        )
        entry_at_open_precedes_range = (
            direction == "SHORT" and op is not None and op >= entry
        ) or (
            direction == "LONG" and op is not None and op <= entry
        )
        if current_confirms_target or entry_at_open_precedes_range:
            return {"state": "TARGET_HIT", "message": f"Đã khớp và đã chạm target {_fmt(target)} theo trình tự có thể xác nhận."}
        if pre_entry_target_possible:
            return {
                "state": "FILLED_SEQUENCE_UNCERTAIN",
                "message": (
                    f"Entry {_fmt(entry)} đã được chạm, nhưng target {_fmt(target)} cũng xuất hiện trong range trước đó; "
                    "OHLC tổng hợp không đủ xác nhận target xảy ra sau fill. Tạm coi vị thế vừa khớp/chưa chốt."
                ),
            }
        return {
            "state": "TARGET_SEQUENCE_UNKNOWN",
            "message": f"Entry và target đều nằm trong range, nhưng OHLC tổng hợp không đủ xác nhận target xảy ra sau fill.",
        }
    return {
        "state": "FILLED_ACTIVE",
        "message": f"Entry {_fmt(entry)} đã được chạm; target {_fmt(target)} chưa được chạm sau fill theo dữ liệu hiện có.",
    }


def load_warning_catalog(path: str | Path | None = None) -> dict[str, Any]:
    catalog_path = Path(path) if path else DEFAULT_WARNING_CATALOG
    if not catalog_path.exists():
        return {"catalog_version": "MISSING", "warnings": []}
    with catalog_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _catalog_by_id(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id")): item for item in catalog.get("warnings", [])}


def _warning_from_catalog(
    warning_id: str,
    catalog_map: dict[str, dict[str, Any]],
    plan: dict[str, Any],
    *,
    level_override: str | None = None,
    detail: str = "",
    mitigated: bool = False,
) -> dict[str, Any]:
    base = dict(catalog_map.get(warning_id, {"id": warning_id, "level": "INFO", "title": warning_id}))
    if level_override:
        base["level"] = level_override
    base.update({
        "plan_key": _plan_key(plan),
        "engine": plan.get("engine"),
        "profile": plan.get("profile"),
        "horizon": plan.get("horizon"),
        "direction": plan.get("direction"),
        "detail": detail,
        "mitigated": mitigated,
    })
    return base


def evaluate_backtested_warnings(plan: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    cmap = _catalog_by_id(catalog)
    warnings: list[dict[str, Any]] = []
    direction = str(plan.get("direction", "")).upper()
    original_entry = plan.get("original_entry")
    original_target = plan.get("original_target")
    swap = bool(plan.get("entry_target_swap_applied"))

    contradiction = False
    if original_entry is not None and original_target is not None and plan.get("is_expected_band"):
        contradiction = (
            direction == "LONG" and original_target < original_entry
        ) or (
            direction == "SHORT" and original_target > original_entry
        )
    if contradiction:
        level = "HIGH" if swap else "CRITICAL"
        detail = (
            f"Raw direction {direction}: OriginalEntry {_fmt(original_entry)}, OriginalTarget {_fmt(original_target)}. "
            + ("Engine đã EntryTargetSwap để giảm rủi ro hình học." if swap else "Chưa thấy cơ chế swap/mitigation trong metadata.")
        )
        warnings.append(_warning_from_catalog(
            "BAND_DIRECTION_CONTRADICTION", cmap, plan,
            level_override=level, detail=detail, mitigated=swap,
        ))

    age = plan.get("target_age_sessions")
    if plan.get("is_expected_band") and age is not None and age >= 3:
        warnings.append(_warning_from_catalog(
            "STALE_EXPECTED_BAND", cmap, plan,
            detail=f"TargetAgeSessions={int(age)}; operational target {_fmt(plan.get('operational_target'))}.",
        ))

    if plan.get("is_ptkt_atr_fallback"):
        warnings.append(_warning_from_catalog(
            "PTKT_ATR_FALLBACK_ACTIVE", cmap, plan,
            detail=f"Target rule: {plan.get('target_rule')}; target {_fmt(plan.get('operational_target'))}.",
        ))

    max_size = _volume_max(plan)
    if (
        plan.get("engine") == "gpt_simcarrry6"
        and str(plan.get("horizon", "")).lower() == "t+2"
        and (max_size <= 0.5 or "HALF" in str(plan.get("risk_action", "")).upper())
    ):
        warnings.append(_warning_from_catalog(
            "T2_BAND_CONFIDENCE_REDUCED", cmap, plan,
            detail=f"RiskAction={plan.get('risk_action') or 'NA'}; max size={max_size:.1f}.",
        ))

    if swap:
        warnings.append(_warning_from_catalog(
            "ENTRY_TARGET_SWAP_ACTIVE", cmap, plan,
            detail=(
                f"Raw {_fmt(original_entry)} -> {_fmt(original_target)}; operational "
                f"{_fmt(plan.get('operational_entry'))} -> {_fmt(plan.get('operational_target'))}."
            ),
            mitigated=True,
        ))

    if str(plan.get("r5_action", "")).upper() == "CANCEL" or plan.get("units") == 0:
        warnings.append(_warning_from_catalog(
            "R5_CANCELLED_ZERO_UNITS", cmap, plan,
            detail=f"R5={plan.get('r5_action') or 'NA'}; Units={plan.get('units')}.",
        ))
    return warnings


def evaluate_runtime_warning(plan: dict[str, Any], state: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    cmap = _catalog_by_id(catalog)
    state_name = state.get("state")
    if state_name == "WAIT_ENTRY_TARGET_PREPASSED":
        return [_warning_from_catalog(
            "OPEN_TARGET_PREPASSED_BEFORE_FILL", cmap, plan,
            detail=state.get("message", ""),
        )]
    if state_name in {"FILLED_SEQUENCE_UNCERTAIN", "TARGET_SEQUENCE_UNKNOWN"}:
        return [_warning_from_catalog(
            "INTRADAY_SEQUENCE_UNCERTAIN", cmap, plan,
            detail=state.get("message", ""),
        )]
    return []


def _sort_warnings(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(warnings, key=lambda w: (
        SEVERITY_ORDER.get(str(w.get("level", "INFO")).upper(), 99),
        str(w.get("engine", "")), str(w.get("horizon", "")), str(w.get("id", ""))
    ))


def _warning_summary(warnings: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(w.get("level", "INFO")).upper() for w in warnings)
    return {
        "total": len(warnings),
        "counts": dict(counts),
        "has_blocker": counts.get("BLOCKER", 0) > 0,
        "has_critical": counts.get("CRITICAL", 0) > 0,
        "has_high_or_above": sum(counts.get(x, 0) for x in ["BLOCKER", "CRITICAL", "HIGH"]) > 0,
        "clear_of_direction_band_contradiction": not any(w.get("id") == "BAND_DIRECTION_CONTRADICTION" for w in warnings),
    }


def _plan_key(plan: dict[str, Any]) -> tuple[str, str, str]:
    return (plan.get("date", ""), plan.get("engine", ""), plan.get("horizon", ""))


def build_context(
    path: str | Path,
    *,
    ohlc_path: str | Path | None = None,
    as_of: str | None = None,
    snapshot: SessionSnapshot | None = None,
    warning_catalog_path: str | Path | None = None,
) -> dict[str, Any]:
    df = _read_table(path)
    rows = normalize_rows(df)
    warning_catalog = load_warning_catalog(warning_catalog_path)
    ohlc = read_ohlc(ohlc_path) if ohlc_path else None
    as_of_ts = _parse_date(as_of) if as_of else None

    reconciled_history: list[dict[str, Any]] = []
    forwards: list[dict[str, Any]] = []
    stale_forward_plans: list[dict[str, Any]] = []
    latest_ohlc_date = None
    latest_completed_bar = None
    if ohlc is not None and not ohlc.empty:
        latest_ohlc_date = ohlc.iloc[-1]["Date"]
        latest_completed_bar = {
            "date": latest_ohlc_date.strftime("%d/%m/%Y"),
            "open": float(ohlc.iloc[-1]["Open"]),
            "high": float(ohlc.iloc[-1]["High"]),
            "low": float(ohlc.iloc[-1]["Low"]),
            "close": float(ohlc.iloc[-1]["Close"]),
        }

    for row in rows:
        if row["kind"] == "HISTORY":
            reconciled_history.append(row)
            continue
        row_ts = row.get("date_ts")
        if ohlc is not None and row_ts is not None:
            hit = ohlc[ohlc["Date"].eq(row_ts)]
            if not hit.empty:
                bar_row = hit.iloc[-1]
                bar = {k: float(bar_row[k]) for k in ["Open", "High", "Low", "Close"]}
                settled = settle_plan_daily(row, bar)
                converted = dict(row)
                converted["kind"] = "HISTORY_DERIVED"
                converted["derived_settlement"] = settled
                converted["pnl_points"] = settled.get("pnl_points")
                converted["win_loss"] = "Win" if (settled.get("pnl_points") or 0) > 0 else ("Loss" if (settled.get("pnl_points") or 0) < 0 else "NoFill")
                converted["action_or_outcome"] = settled.get("status", "")
                reconciled_history.append(converted)
                continue
        # A forward row becomes stale when a newer completed OHLC basis exists.
        # Example: a t+2 plan for 28/07 generated from basis 24/07 must not remain
        # the primary plan after OHLC 27/07 is available; the horizon must roll to
        # t+1 from basis 27/07 (or the bridge must explicitly report it missing).
        basis_ts = row.get("basis_date_ts")
        if (
            latest_ohlc_date is not None
            and row_ts is not None
            and row_ts > latest_ohlc_date
            and basis_ts is not None
            and basis_ts < latest_ohlc_date
        ):
            stale = dict(row)
            stale["freshness_status"] = "STALE_FORWARD_BASIS"
            stale["freshness_reason"] = (
                f"Basis {basis_ts.strftime('%d/%m/%Y')} cũ hơn OHLC hoàn thành "
                f"{latest_ohlc_date.strftime('%d/%m/%Y')}"
            )
            stale_forward_plans.append(stale)
            continue
        row["freshness_status"] = "FRESH_FORWARD"
        forwards.append(row)

    if as_of_ts is not None:
        exact = [r for r in forwards if r.get("date_ts") is not None and r["date_ts"] == as_of_ts]
        if exact:
            active = exact
        else:
            future = [r for r in forwards if r.get("date_ts") is not None and r["date_ts"] >= as_of_ts]
            min_date = min((r["date_ts"] for r in future), default=None)
            active = [r for r in future if min_date is not None and r["date_ts"] == min_date]
    else:
        min_date = min((r["date_ts"] for r in forwards if r.get("date_ts") is not None), default=None)
        active = [r for r in forwards if min_date is not None and r["date_ts"] == min_date] if min_date is not None else []

    active.sort(key=lambda r: (-(r.get("match_score") or 0), r.get("engine", "")))
    reconciled_history.sort(key=lambda r: r.get("date_ts") or pd.Timestamp.min, reverse=True)

    direction_counts = Counter(r.get("direction") for r in active if r.get("direction"))
    consensus_direction = None
    consensus_strength = 0.0
    if direction_counts:
        consensus_direction, count = direction_counts.most_common(1)[0]
        consensus_strength = count / len(active)

    plan_states = []
    warnings: list[dict[str, Any]] = []
    snap = snapshot or SessionSnapshot(as_of=as_of)
    for p in active:
        state = evaluate_live_plan(p, snap)
        plan_states.append({
            "plan_key": _plan_key(p),
            "engine": p.get("engine"),
            "horizon": p.get("horizon"),
            "state": state,
        })
        warnings.extend(evaluate_backtested_warnings(p, warning_catalog))
        warnings.extend(evaluate_runtime_warning(p, state, warning_catalog))
    warnings = _sort_warnings(warnings)

    return {
        "schema_version": BRIDGE_VERSION,
        "source_file": str(Path(path)),
        "as_of": as_of or (active[0]["date"] if active else ""),
        "latest_completed_ohlc": latest_completed_bar,
        "active_plans": active,
        "all_forward_plans": sorted(forwards, key=lambda r: (r.get("date_ts") or pd.Timestamp.max, r.get("engine", ""), r.get("horizon", ""))),
        "stale_forward_plans": stale_forward_plans,
        "freshness": {
            "latest_completed_ohlc_date": latest_ohlc_date.strftime("%d/%m/%Y") if latest_ohlc_date is not None else "",
            "fresh_forward_count": len(forwards),
            "stale_forward_count": len(stale_forward_plans),
            "active_plan_is_fresh": all(p.get("freshness_status") == "FRESH_FORWARD" for p in active),
        },
        "plan_states": plan_states,
        "consensus": {
            "direction": consensus_direction,
            "strength": consensus_strength,
            "count": len(active),
            "is_unanimous": bool(active) and consensus_strength == 1.0,
        },
        "recent_history": reconciled_history[:100],
        "history_row_count": len(reconciled_history),
        "r5_control": summarize_r5_control(active, reconciled_history),
        "warning_catalog_version": warning_catalog.get("catalog_version", "NA"),
        "warnings": warnings,
        "warning_summary": _warning_summary(warnings),
        "rules": [
            "Chỉ trả lời bằng dữ liệu trong context.",
            "Không biến Độ khớp thành xác suất.",
            "Không tự tạo stop-loss vì schema 13 cột không có stop-loss chuẩn.",
            "Không cộng chồng PnL của các engine/horizon cùng phiên như một danh mục duy nhất.",
            "Phải phân biệt kế hoạch chưa khớp, đã khớp và target đã chạm.",
            "Cảnh báo BLOCKER/CRITICAL/HIGH đã qua backtest hoặc live-integrity phải được nêu trước entry/target.",
            "Không tự tạo cảnh báo ngoài warning catalog và metadata hiện có.",
            "Không dùng forward plan có BasisDate cũ hơn ngày OHLC hoàn thành mới nhất làm kèo chính.",
            "Nếu chưa có t+1 mới sau khi horizon cuốn, phải báo thiếu kế hoạch mới thay vì nâng t+2 cũ lên.",
            "R5 là corrective overlay; PRE_OPEN không được trình bày như action OPEN đã xác nhận.",
            "CANCEL/units=0 là NO TRADE và không tự động cho phép đảo chiều; FLIP_HINT mới mở kịch bản đảo có điều kiện.",
            "Đây là hỗ trợ quyết định, không phải cam kết lợi nhuận hay lệnh tự động.",
        ],
    }


def _extract_price(question: str) -> float | None:
    candidates = re.findall(r"(?<!\d)(\d{3,4}(?:[.,]\d+)?)(?!\d)", question.replace(".", ""))
    if not candidates:
        return None
    try:
        return float(candidates[-1].replace(",", "."))
    except ValueError:
        return None


def _intent(question: str) -> str:
    q = question.lower()
    # Specific operational intents must win over generic words such as "khác nhau".
    if any(k in q for k in ["cảnh báo", "red flag", "backtest warning", "có gì nguy hiểm", "đắt giá"]):
        return "WARNINGS"
    if any(k in q for k in ["stop", "cắt lỗ", "rủi ro", "risk"]):
        return "RISK"
    if any(k in q for k in ["target", "chốt", "thoát", "mục tiêu"]):
        return "TARGET"
    if any(k in q for k in ["ưu tiên", "hệ nào", "engine nào", "theo hệ nào"]):
        return "PRIORITY"
    if any(k in q for k in ["đã khớp", "chưa khớp", "chạm entry", "vào được", "giá hiện tại", "giá "]):
        return "FILL_STATUS"
    if any(k in q for k in ["lịch sử", "gần đây", "kết quả", "lời", "lỗ", "hiệu quả", "27/7"]):
        return "HISTORY"
    if any(k in q for k in ["tại sao", "vì sao", "chênh", "khác target"]):
        return "EXPLAIN"
    if any(k in q for k in ["đồng thuận", "xung đột", "cùng hướng", "khác nhau"]):
        return "CONSENSUS"
    return "CURRENT_PLAN"


def _engine_display(name: str, profile: str = "") -> str:
    mapping = {
        "gpt_simcarrry6": "SIMCARRRY6",
        "gpt_simptkt": "SIMPTKT",
        "engine5": profile or "ENGINE5",
    }
    return mapping.get(name, name or profile or "ENGINE")


def _plan_line(p: dict[str, Any], state: dict[str, Any] | None = None) -> str:
    role = p.get("engine_role", "")
    engine_label = _engine_display(p.get("engine", ""), p.get("profile", ""))
    horizon = p.get("horizon", "") or "NA"
    s = (
        f"{engine_label} ({horizon}, {role}): {p.get('direction')} | "
        f"Entry {_fmt(p.get('operational_entry'))} | Target {_fmt(p.get('operational_target'))} | "
        f"{p.get('risk_action') or p.get('action_or_outcome') or 'không ghi risk action'} | Độ khớp {_fmt(p.get('match_score'))}."
    )
    if state:
        s += " " + state.get("message", "")
    return s


def _warning_lines(context: dict[str, Any], *, include_clear: bool = False) -> list[str]:
    warnings = context.get("warnings", [])
    if not warnings:
        return ["Cảnh báo backtest: không có cảnh báo nào kích hoạt trên metadata hiện tại."] if include_clear else []
    lines = ["CẢNH BÁO HỆ THỐNG (ưu tiên theo mức độ):"]
    for w in warnings:
        engine = _engine_display(w.get("engine", ""), w.get("profile", ""))
        mitigated = " | đã được mitigation" if w.get("mitigated") else ""
        lines.append(
            f"- [{w.get('level')}] {w.get('title')} | {engine} {w.get('horizon') or ''}{mitigated}. "
            f"{w.get('detail') or w.get('interpretation') or ''} Evidence: {w.get('evidence_scope', 'NA')}"
        )
    return lines


def _critical_warning_lines(context: dict[str, Any]) -> list[str]:
    severe = [w for w in context.get("warnings", []) if str(w.get("level", "")).upper() in {"BLOCKER", "CRITICAL", "HIGH"}]
    if not severe:
        return []
    cloned = dict(context)
    cloned["warnings"] = severe
    return _warning_lines(cloned)


def answer_rule_based(question: str, context: dict[str, Any]) -> str:
    intent = _intent(question)
    plans = context.get("active_plans", [])
    history = context.get("recent_history", [])
    as_of = context.get("as_of", "")
    latest = context.get("latest_completed_ohlc")
    states_by_key = {tuple(x["plan_key"]): x["state"] for x in context.get("plan_states", [])}

    if not plans:
        return "Không có kế hoạch FORWARD hợp lệ cho ngày yêu cầu. Không suy đoán kèo mới từ các dòng HISTORY."

    header = f"BFXPS as-of {as_of}."
    if latest:
        header += f" OHLC hoàn thành gần nhất {latest['date']}: O {_fmt(latest['open'])}, H {_fmt(latest['high'])}, L {_fmt(latest['low'])}, C {_fmt(latest['close'])}."

    if intent == "WARNINGS":
        lines = [header]
        lines.extend(_warning_lines(context, include_clear=True))
        if context.get("warning_summary", {}).get("clear_of_direction_band_contradiction"):
            lines.append("Không kích hoạt red flag direction-expected-band mâu thuẫn trên các kế hoạch active hiện tại.")
        lines.append("AI chỉ nêu cảnh báo có trong catalog/evidence; không tự suy diễn thêm.")
        return "\n".join(lines)

    if intent == "CONSENSUS":
        con = context["consensus"]
        if con["is_unanimous"]:
            lines = [f"{header} Có đồng thuận {con['direction']} ở {con['count']}/{con['count']} kế hoạch đang hoạt động."]
            lines.extend(_warning_lines(context))
        else:
            lines = [f"{header} Không có đồng thuận tuyệt đối; hướng chiếm đa số là {con['direction']} ({con['strength']:.0%})."]
            lines.extend(_warning_lines(context))
        lines.extend(_plan_line(p) for p in plans)
        lines.append("Độ khớp là điểm nội bộ, không phải xác suất thắng.")
        return "\n".join(lines)

    if intent == "PRIORITY":
        operational = [p for p in plans if p.get("engine") == "gpt_simcarrry6"]
        confirmation = [p for p in plans if p.get("engine") == "gpt_simptkt"]
        lines = [header]
        if operational:
            lines.append("Ưu tiên trình bày SIMCARRRY6 như kế hoạch operational vì có forecast band, target rule và risk action cụ thể.")
            lines.append(_plan_line(operational[0]))
        if confirmation:
            lines.append("Dùng SIMPTKT làm xác nhận PTKT độc lập, không tự động thay thế kế hoạch operational.")
            lines.append(_plan_line(confirmation[0]))
        lines.append("Không tuyên bố engine nào có xác suất thắng cao hơn nếu file không cung cấp thống kê đó.")
        return "\n".join(lines)

    if intent == "FILL_STATUS":
        extracted = _extract_price(question)
        lines = [header]
        lines.extend(_warning_lines(context))
        if extracted is not None:
            lines.append(f"Giá được hỏi: {_fmt(extracted)}.")
        for p in plans:
            state = states_by_key.get(_plan_key(p))
            lines.append(_plan_line(p, state))
        lines.append("Muốn xác nhận fill chắc chắn cần tối thiểu Open/High/Low của phiên, không chỉ một giá hiện tại.")
        return "\n".join(lines)

    if intent == "TARGET":
        lines = [header]
        lines.extend(_warning_lines(context))
        for p in plans:
            lines.append(_plan_line(p))
        operational = next((p for p in plans if p.get("engine") == "gpt_simcarrry6"), None)
        ptkt = next((p for p in plans if p.get("engine") == "gpt_simptkt"), None)
        if operational and ptkt and operational.get("operational_target") != ptkt.get("operational_target"):
            lines.append(
                f"Mốc operational ưu tiên để theo dõi là {_fmt(operational.get('operational_target'))} từ forecast band/SIMCARRRY6; "
                f"mốc {_fmt(ptkt.get('operational_target'))} là target PTKT mở rộng để tham khảo nếu xu hướng tiếp diễn."
            )
            lines.append("Không tự gộp hai target và không tự suy ra tỷ lệ chốt từng phần nếu hệ chưa xuất quy tắc đó.")
        elif len({p.get("operational_target") for p in plans}) > 1:
            lines.append("Các target khác nhau phản ánh nhiều lớp mô hình. Không gộp chúng thành một mức giả tạo.")
        return "\n".join(lines)

    if intent == "RISK":
        lines = [header]
        lines.extend(_warning_lines(context, include_clear=True))
        for p in plans:
            lines.append(_plan_line(p))
        lines.append("Schema 13 cột không có stop-loss chuẩn. Trợ lý không được tự bịa điểm cắt lỗ; chỉ nêu đúng RiskAction/R5 nếu file cung cấp.")
        return "\n".join(lines)

    if intent == "HISTORY":
        lines = [header, "Kết quả gần đây (không cộng chồng các engine/horizon thành một danh mục):"]
        seen = 0
        for r in history:
            if seen >= 6:
                break
            label = _engine_display(r.get("engine", ""), r.get("profile", ""))
            pnl = r.get("pnl_points")
            diagnostic = " (settle suy diễn từ OHLC)" if r.get("kind") == "HISTORY_DERIVED" and r.get("derived_settlement", {}).get("diagnostic_only") else ""
            lines.append(
                f"- {r.get('date')} | {label} {r.get('horizon')} | {r.get('direction')} | "
                f"{r.get('action_or_outcome')} | PnL {_fmt(pnl)} điểm{diagnostic}."
            )
            seen += 1
        return "\n".join(lines)

    if intent == "EXPLAIN":
        lines = [header]
        lines.extend(_warning_lines(context))
        lines.append("Hai engine có thể cùng direction nhưng khác target vì mô hình hóa khác nhau:")
        for p in plans:
            lines.append(
                f"- {p.get('engine')}: {p.get('engine_role')}; target {_fmt(p.get('operational_target'))}; "
                f"rule {p.get('target_rule') or 'không ghi'}; nguồn hướng {p.get('direction_source') or 'không ghi'}.")
        return "\n".join(lines)

    con = context["consensus"]
    lines = [header]
    lines.extend(_warning_lines(context))
    if con["is_unanimous"]:
        lines.append(f"Kèo chính: đồng thuận {con['direction']} trên {con['count']} kế hoạch.")
    else:
        lines.append(f"Kèo hiện tại chưa đồng thuận tuyệt đối; hướng đa số {con['direction']}.")
    for p in plans:
        state = states_by_key.get(_plan_key(p))
        lines.append(_plan_line(p, state))
    if latest and plans:
        entries = [p.get("operational_entry") for p in plans if p.get("operational_entry") is not None]
        if entries and con.get("direction") == "SHORT" and latest.get("close") < min(entries):
            lines.append(
                f"Vì Close gần nhất {_fmt(latest.get('close'))} đang thấp hơn vùng entry khoảng {_fmt(min(entries))}, "
                "đây là kèo chờ giá hồi lên entry để SHORT, không phải chỉ dẫn SHORT đuổi ở giá thấp."
            )
        elif entries and con.get("direction") == "LONG" and latest.get("close") > max(entries):
            lines.append(
                f"Vì Close gần nhất {_fmt(latest.get('close'))} đang cao hơn vùng entry khoảng {_fmt(max(entries))}, "
                "đây là kèo chờ giá điều chỉnh về entry để LONG, không phải chỉ dẫn LONG đuổi ở giá cao."
            )
    lines.append("Đây là kế hoạch có điều kiện theo entry, không phải lệnh thị trường tự động.")
    return "\n".join(lines)


def llm_payload(question: str, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "system": (
            "Bạn là trợ lý đọc dữ liệu BFXPS. Chỉ dùng context; phân biệt HISTORY/FORWARD; "
            "đánh giá đồng thuận/xung đột giữa engine; phân biệt chưa khớp/đã khớp/target hit; "
            "không bịa stop-loss, xác suất hoặc giá thị trường; không cộng trùng PnL của nhiều engine/horizon; "
            "luôn nêu ngày dữ liệu và không tự phát lệnh giao dịch; "
            "phải đưa BLOCKER/CRITICAL/HIGH từ context.warnings lên trước entry-target; "
            "không tự tạo cảnh báo ngoài warning catalog."
        ),
        "question": question,
        "intent": _intent(question),
        "context": context,
        "response_format": {
            "as_of": "dd/mm/YYYY",
            "status": "PRE_OPEN|WAIT_ENTRY|FILLED_ACTIVE|TARGET_HIT|NO_PLAN",
            "consensus": "LONG|SHORT|MIXED|NA",
            "summary": "string",
            "plans": [
                {
                    "engine": "string",
                    "horizon": "string",
                    "direction": "LONG|SHORT",
                    "entry": "number|null",
                    "target": "number|null",
                    "fill_state": "string",
                }
            ],
            "risk_note": "string",
            "warnings": [
                {
                    "id": "string",
                    "level": "BLOCKER|CRITICAL|HIGH|MEDIUM|NOTICE|INFO",
                    "title": "string",
                    "detail": "string",
                    "evidence_scope": "string",
                    "mitigated": "boolean"
                }
            ],
            "evidence": ["string"],
        },
    }


def render_live_ohlc(ohlc_path: str | Path, trades_path: str | Path, out_png: str | Path, as_of: str | None = None) -> Path:
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    bars = read_ohlc(ohlc_path).tail(80).reset_index(drop=True)
    context = build_context(trades_path, ohlc_path=ohlc_path, as_of=as_of)
    plans = context.get("active_plans", [])

    fig, ax = plt.subplots(figsize=(15, 8))
    for i, r in bars.iterrows():
        up = r["Close"] >= r["Open"]
        color = "#2E7D32" if up else "#C62828"
        ax.vlines(i, r["Low"], r["High"], color=color, linewidth=1)
        low_body = min(r["Open"], r["Close"])
        height = max(abs(r["Close"] - r["Open"]), 0.05)
        ax.add_patch(Rectangle((i - 0.32, low_body), 0.64, height, facecolor=color, edgecolor=color, alpha=0.8))

    line_styles = ["--", "-.", ":", (0, (5, 1))]
    for idx, p in enumerate(plans):
        if p.get("operational_entry") is not None:
            ax.axhline(p["operational_entry"], linestyle=line_styles[idx % len(line_styles)], linewidth=1.8,
                       label=f"{p['engine']} Entry {_fmt(p['operational_entry'])}")
        if p.get("operational_target") is not None:
            ax.axhline(p["operational_target"], linestyle=line_styles[(idx + 1) % len(line_styles)], linewidth=1.6,
                       label=f"{p['engine']} Target {_fmt(p['operational_target'])}")

    con = context.get("consensus", {})
    title = f"BFXPS LIVE OHLC | {context.get('as_of', '')} | Consensus {con.get('direction') or 'NA'}"
    ax.set_title(title, loc="left", fontweight="bold")
    tick_idx = list(range(0, len(bars), max(1, len(bars) // 8)))
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([bars.loc[i, "Timestamp"].strftime("%d/%m") for i in tick_idx], rotation=30, ha="right")
    ax.set_ylabel("VN30F1M")
    ax.grid(axis="y", alpha=0.25)
    if plans:
        ax.legend(loc="best")
    fig.tight_layout()

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_png.with_name(out_png.stem + ".tmp.png")
    fig.savefig(tmp, dpi=160)
    plt.close(fig)
    os.replace(tmp, out_png)
    return out_png


def _snapshot_from_args(args: argparse.Namespace) -> SessionSnapshot:
    return SessionSnapshot(
        as_of=getattr(args, "as_of", None),
        live_price=getattr(args, "live_price", None),
        session_open=getattr(args, "session_open", None),
        session_high=getattr(args, "session_high", None),
        session_low=getattr(args, "session_low", None),
    )


def _add_market_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ohlc")
    parser.add_argument("--as-of", help="Ngày kế hoạch dd/mm/YYYY")
    parser.add_argument("--live-price", type=float)
    parser.add_argument("--session-open", type=float)
    parser.add_argument("--session-high", type=float)
    parser.add_argument("--session-low", type=float)
    parser.add_argument("--warning-catalog", help="JSON catalog cảnh báo đã qua backtest")


def main() -> None:
    p = argparse.ArgumentParser(description="BFXPS 13-column customer advisory bridge v3")
    sub = p.add_subparsers(dest="cmd", required=True)

    summary = sub.add_parser("summary")
    summary.add_argument("--trades", required=True)
    _add_market_args(summary)

    ask = sub.add_parser("ask")
    ask.add_argument("--trades", required=True)
    ask.add_argument("--question", required=True)
    ask.add_argument("--json", action="store_true", help="Xuất payload để gửi sang LLM bất kỳ")
    _add_market_args(ask)

    render = sub.add_parser("render")
    render.add_argument("--trades", required=True)
    render.add_argument("--ohlc", required=True)
    render.add_argument("--as-of")
    render.add_argument("--out", default="outputs/pics/STRATEGY_OHLC_1D_LIVE_DAILY_RANK1_BEST_ENGINE.png")

    watch = sub.add_parser("watch")
    watch.add_argument("--trades", required=True)
    watch.add_argument("--ohlc", required=True)
    watch.add_argument("--as-of")
    watch.add_argument("--out", default="outputs/pics/STRATEGY_OHLC_1D_LIVE_DAILY_RANK1_BEST_ENGINE.png")
    watch.add_argument("--seconds", type=int, default=5)

    args = p.parse_args()
    if args.cmd == "summary":
        ctx = build_context(args.trades, ohlc_path=args.ohlc, as_of=args.as_of, snapshot=_snapshot_from_args(args), warning_catalog_path=args.warning_catalog)
        print(json.dumps(ctx, ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "ask":
        extracted = _extract_price(args.question)
        if args.live_price is None and extracted is not None:
            args.live_price = extracted
        ctx = build_context(args.trades, ohlc_path=args.ohlc, as_of=args.as_of, snapshot=_snapshot_from_args(args), warning_catalog_path=args.warning_catalog)
        if args.json:
            print(json.dumps(llm_payload(args.question, ctx), ensure_ascii=False, indent=2, default=str))
        else:
            print(answer_rule_based(args.question, ctx))
    elif args.cmd == "render":
        print(render_live_ohlc(args.ohlc, args.trades, args.out, as_of=args.as_of))
    elif args.cmd == "watch":
        while True:
            try:
                print(render_live_ohlc(args.ohlc, args.trades, args.out, as_of=args.as_of))
            except Exception as exc:
                print(f"[WARN] {exc}")
            time.sleep(max(1, args.seconds))


if __name__ == "__main__":
    main()

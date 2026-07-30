from __future__ import annotations

from pathlib import Path
from datetime import datetime
import hashlib
import math
import re
import threading
import unicodedata

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import pandas as pd

# Python/Matplotlib only. No image-generation application is used anywhere here.
CHART_TERMS = (
    "chart", "charts", "on chart", "biểu đồ", "đồ thị", "vẽ nến", "ve nen",
    "ohlc zoom", "đưa lên hình", "đặt lên hình",
)
PERF_TERMS = (
    "hiệu suất", "pnl", "equity", "drawdown", "wr", "win rate", "performance",
    "lời lỗ", "lai lo", "trades gần nhất", "trade gần nhất", "lệnh gần nhất",
)
FORECAST_TERMS = (
    "forecast", "kèo", "keo", "entry", "target", "tp", "ohlc", "giá live",
    "gia live", "horizon", "engine", "fill", "wait_entry", "khớp", "gap",
)
BAND_TERMS = (
    "expected low", "expected high", "center", "centre", "biên", "bien",
    "low/high", "low high", "all days", "toàn bộ ngày", "tat ca ngay",
    "giá kỳ vọng", "gia ky vong", "chart biên all", "chart bien all",
)
R5_TERMS = (
    "r5", "keep", "cancel", "flip_hint", "flip hint", "cho đánh", "cho danh",
    "được đánh", "duoc danh", "cảnh báo", "canh bao", "warning", "no trade",
)

_CHART_LOCK = threading.Lock()


def _fold(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).replace("đ", "d")
    return re.sub(r"\s+", " ", raw).strip()


def wants_chart(question: str) -> bool:
    q = _fold(question)
    return any(_fold(t) in q for t in CHART_TERMS)


def _read(path: Path) -> pd.DataFrame:
    if not path or not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, sep="\t")
    except Exception:
        return pd.DataFrame()


def _num(series_or_value):
    return pd.to_numeric(series_or_value, errors="coerce")


def _note_value(note: str, key: str) -> str:
    m = re.search(rf"(?:^|;)\s*{re.escape(key)}=([^;]+)", str(note or ""), flags=re.I)
    return m.group(1).strip() if m else ""


def _engine_label(row: pd.Series) -> str:
    label = str(row.get("EngineChartLabel", "")).strip()
    if label:
        return label
    note = str(row.get("Ghi chú HybridV3", ""))
    eng = _note_value(note, "Engine") or "UNKNOWN"
    prof = _note_value(note, "Profile")
    return f"{eng}:{prof}" if prof else eng


def _canonical_engine(row: pd.Series) -> str:
    text = _fold(_engine_label(row) + " " + str(row.get("Ghi chú HybridV3", "")))
    if "simcarrry6" in text or "simcarry6" in text:
        return "SIMCARRRY6"
    if "12k_allday" in text or "12k allday" in text:
        return "12K_AllDay"
    if "alldaysladder" in text or "all days ladder" in text:
        return "AllDaysLadder_CAP0.3"
    prof = _note_value(str(row.get("Ghi chú HybridV3", "")), "Profile")
    return prof or _engine_label(row)


def _is_engine5(row: pd.Series) -> bool:
    t = _fold(_engine_label(row) + " " + str(row.get("Ghi chú HybridV3", "")))
    return "engine5" in t or _canonical_engine(row) in {"12K_AllDay", "AllDaysLadder_CAP0.3"}


def _risk_policy(row: pd.Series) -> str:
    note = str(row.get("Ghi chú HybridV3", ""))
    for key in ("RiskAction", "R5Overlay"):
        v = _note_value(note, key)
        if v:
            return v.upper()
    comp = str(row.get("So sánh OHLC", "")).upper().strip()
    if comp.startswith("TAKE_"):
        return comp
    return ""


def _r5_authority(row: pd.Series) -> dict:
    if not _is_engine5(row):
        policy = _risk_policy(row)
        return {
            "state": "N/A",
            "level": "INFO",
            "allowed": None,
            "text": f"SIM policy: {policy or 'theo plan'}; R5 không sở hữu kèo này",
        }
    blob = " | ".join([
        str(row.get("R5Action", "")), str(row.get("Win/Loss", "")),
        str(row.get("So sánh OHLC", "")), str(row.get("Ghi chú HybridV3", "")),
    ]).upper()
    if "CANCEL" in blob:
        return {"state": "CANCEL", "level": "RED", "allowed": False, "text": "CẢNH BÁO ĐỎ: R5 CANCEL — NO TRADE"}
    if "FLIP_HINT" in blob or "FLIP HINT" in blob:
        return {"state": "FLIP_HINT", "level": "RED", "allowed": False, "text": "CẢNH BÁO ĐỎ: FLIP_HINT — không chạy kèo cũ"}
    if re.search(r"(?:^|[|; ])KEEP(?:$|[|; ])", blob):
        return {"state": "KEEP", "level": "GREEN", "allowed": True, "text": "ĐƯỢC ĐÁNH: R5 KEEP — theo đúng entry/size của outputs"}
    if "PRE_OHLCV" in blob or "PENDING_OHLCV" in blob or str(row.get("RowKind", "")).upper() == "FORWARD":
        return {"state": "PRE_OPEN", "level": "RED", "allowed": False, "text": "CẢNH BÁO ĐỎ: R5 PRE_OPEN — chưa phải lệnh cuối"}
    return {"state": "NO_SIGNAL", "level": "RED", "allowed": False, "text": "CẢNH BÁO ĐỎ: chưa có quyền R5 hợp lệ"}


def _safe_name(signature: str, kind: str) -> str:
    h = hashlib.sha1(signature.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"DYNAMIC_{kind}_{h}.png"


def _mtime(path: Path) -> int:
    return path.stat().st_mtime_ns if path and path.exists() else 0


def _history_union(last3_tsv: Path, recent_tsv: Path) -> pd.DataFrame:
    frames = []
    for path, source, rank in ((recent_tsv, "RECENT30", 0), (last3_tsv, "LAST3", 1)):
        d = _read(path)
        if d.empty:
            continue
        if "RowKind" in d.columns:
            d = d[d["RowKind"].astype(str).str.upper().eq("HISTORY")].copy()
        d["HistorySource"] = source
        d["SourceRank"] = rank
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True, sort=False)
    for c in ("Entry", "Exit", "Forecast", "PNL points"):
        if c not in d.columns:
            d[c] = math.nan
        d[c] = _num(d[c])
    d["engine_key"] = d.apply(_canonical_engine, axis=1)
    d["engine_label"] = d.apply(_engine_label, axis=1)
    d["date"] = pd.to_datetime(d.get("Ngày entry"), dayfirst=True, errors="coerce")
    d["direction"] = d.get("Loại lệnh", pd.Series("", index=d.index)).astype(str).str.upper()
    d["r5_state"] = d.apply(lambda r: _r5_authority(r)["state"], axis=1)
    d = d.sort_values(["SourceRank", "date"]).drop_duplicates(
        ["engine_key", "date", "Entry", "Exit"], keep="first"
    )
    d["center"] = d["Forecast"]
    d.loc[d["center"].isna(), "center"] = (d["Entry"] + d["Exit"]) / 2.0
    d["observed_low"] = d[["Entry", "Exit", "center"]].min(axis=1)
    d["observed_high"] = d[["Entry", "Exit", "center"]].max(axis=1)
    return d.reset_index(drop=True)


def _walk_forward_bands(hist: pd.DataFrame) -> pd.DataFrame:
    if hist.empty:
        return hist.copy()
    rows = []
    for engine, g in hist.groupby("engine_key", sort=False):
        g = g.sort_values("date").copy()
        low_offsets: list[float] = []
        high_offsets: list[float] = []
        for _, r in g.iterrows():
            center = float(r["center"])
            if len(low_offsets) >= 3:
                low = center + float(pd.Series(low_offsets[-30:]).median())
                high = center + float(pd.Series(high_offsets[-30:]).median())
                source = f"WF_PRIOR_MEDIAN_n{min(30, len(low_offsets))}"
            else:
                low = float(r["observed_low"])
                high = float(r["observed_high"])
                source = "WARMUP_ROW_SPAN"
            if low > high:
                low, high = high, low
            out = r.to_dict()
            out.update({"expected_low": low, "expected_high": high, "band_source": source})
            rows.append(out)
            low_offsets.append(float(r["observed_low"]) - center)
            high_offsets.append(float(r["observed_high"]) - center)
    return pd.DataFrame(rows)


def _empirical_band(forward_row: pd.Series, hist: pd.DataFrame) -> dict:
    entry = float(forward_row["Entry"])
    target = float(forward_row["Exit"])
    forecast = _num(pd.Series([forward_row.get("Forecast")])).iloc[0]
    center = float(forecast) if pd.notna(forecast) else (entry + target) / 2.0
    key = _canonical_engine(forward_row)
    sample = hist[hist["engine_key"].eq(key)].dropna(subset=["Entry", "Exit", "center"]).copy() if not hist.empty else pd.DataFrame()
    if not sample.empty:
        low_offsets = sample["observed_low"] - sample["center"]
        high_offsets = sample["observed_high"] - sample["center"]
        low = center + float(low_offsets.median())
        high = center + float(high_offsets.median())
        source = "HISTORY_MEDIAN"
        n = int(len(sample))
    else:
        low = min(entry, target, center)
        high = max(entry, target, center)
        source = "SAME_ROW_SPAN"
        n = 0
    if low > high:
        low, high = high, low
    auth = _r5_authority(forward_row)
    return {
        "engine_key": key,
        "center": center,
        "expected_low": low,
        "expected_high": high,
        "sample_n": n,
        "band_source": source,
        "authority": auth,
        "risk_policy": _risk_policy(forward_row),
    }


def _parse_ohlc_from_question(question: str) -> dict[str, float]:
    q = str(question or "")
    out: dict[str, float] = {}
    pats = {
        "session_open": r"(?:^|\s)O\s*[=:]?\s*([0-9]+(?:[.,][0-9]+)?)",
        "session_high": r"(?:^|\s)H\s*[=:]?\s*([0-9]+(?:[.,][0-9]+)?)",
        "session_low": r"(?:^|\s)L\s*[=:]?\s*([0-9]+(?:[.,][0-9]+)?)",
        "live_price": r"(?:^|\s)(?:P|C)\s*[=:]?\s*([0-9]+(?:[.,][0-9]+)?)",
    }
    for k, p in pats.items():
        m = re.search(p, q, re.I)
        if m:
            try:
                out[k] = float(m.group(1).replace(",", "."))
            except ValueError:
                pass
    return out


def _load_ohlc_history(path: Path, n: int = 12) -> pd.DataFrame:
    if not path or not path.exists():
        return pd.DataFrame()
    try:
        d = pd.read_csv(path)
    except Exception:
        try:
            d = pd.read_csv(path, sep="\t")
        except Exception:
            return pd.DataFrame()
    cols = {str(c).lower().strip(): c for c in d.columns}

    def pick(names):
        for x in names:
            if x in cols:
                return cols[x]
        return None

    dc = pick(["date", "datetime", "ngày", "ngay"])
    oc = pick(["open", "o"])
    hc = pick(["high", "h"])
    lc = pick(["low", "l"])
    cc = pick(["close", "c", "price"])
    if not all([oc, hc, lc, cc]):
        return pd.DataFrame()
    out = pd.DataFrame({
        "date": pd.to_datetime(d[dc], errors="coerce", dayfirst=True) if dc else pd.RangeIndex(len(d)),
        "Open": _num(d[oc]), "High": _num(d[hc]), "Low": _num(d[lc]), "Close": _num(d[cc]),
    }).dropna(subset=["Open", "High", "Low", "Close"])
    return out.tail(n).reset_index(drop=True)


def _forward_rows(forward_tsv: Path, last3_tsv: Path, recent_tsv: Path) -> tuple[pd.DataFrame, list[dict], pd.DataFrame]:
    f = _read(forward_tsv)
    if f.empty or "RowKind" not in f.columns:
        return pd.DataFrame(), [], pd.DataFrame()
    f = f[f["RowKind"].astype(str).str.upper().eq("FORWARD")].copy()
    for c in ("Entry", "Exit", "Forecast"):
        if c not in f.columns:
            f[c] = math.nan
        f[c] = _num(f[c])
    f = f.dropna(subset=["Entry", "Exit"])
    if f.empty:
        return f, [], pd.DataFrame()
    f["date"] = pd.to_datetime(f.get("Ngày entry"), dayfirst=True, errors="coerce")
    f["label"] = f.apply(_engine_label, axis=1)
    f["engine_key"] = f.apply(_canonical_engine, axis=1)
    notes = f.get("Ghi chú HybridV3", pd.Series("", index=f.index)).astype(str)
    f["horizon"] = notes.str.extract(r"Horizon=([^;]+)", expand=False).fillna("")
    hist = _history_union(last3_tsv, recent_tsv)
    bands: list[dict] = []
    for idx, row in f.sort_values(["date", "label"]).iterrows():
        b = _empirical_band(row, hist)
        b.update({
            "idx": int(idx),
            "date": row["date"],
            "label": row["label"],
            "horizon": row["horizon"],
            "direction": str(row.get("Loại lệnh", "")).upper(),
            "entry": float(row["Entry"]),
            "target": float(row["Exit"]),
        })
        bands.append(b)
    return f, bands, hist


def _authority_palette(level: str) -> tuple[str, str]:
    if level == "GREEN":
        return "#d8f3dc", "#146c2e"
    if level == "RED":
        return "#ffe2e2", "#b00020"
    return "#e8eef8", "#274c77"


def _global_r5_summary(bands: list[dict]) -> dict:
    relevant = [b["authority"] for b in bands if b["authority"]["state"] != "N/A"]
    if not relevant:
        return {"level": "INFO", "text": "R5: không có engine5 forward trong database", "state": "N/A"}
    states = {x["state"] for x in relevant}
    if "CANCEL" in states:
        return {"level": "RED", "text": "CẢNH BÁO ĐỎ: R5 CANCEL — ENGINE5 NO TRADE", "state": "CANCEL"}
    if "FLIP_HINT" in states:
        return {"level": "RED", "text": "CẢNH BÁO ĐỎ: R5 FLIP_HINT — KHÔNG CHẠY KÈO CŨ", "state": "FLIP_HINT"}
    if "PRE_OPEN" in states or "NO_SIGNAL" in states:
        return {"level": "RED", "text": "CẢNH BÁO ĐỎ: R5 PRE_OPEN/NO_SIGNAL — CHƯA LÀ LỆNH CUỐI", "state": "PRE_OPEN"}
    return {"level": "GREEN", "text": "ĐƯỢC ĐÁNH ENGINE5: R5 KEEP — THEO ĐÚNG OUTPUTS", "state": "KEEP"}


def build_forward_forecast_chart(
    forward_tsv: Path,
    last3_tsv: Path,
    recent_tsv: Path,
    out_png: Path,
    *,
    ohlc_tsv: Path | None = None,
    live_price=None,
    session_open=None,
    session_high=None,
    session_low=None,
) -> tuple[Path | None, dict]:
    f, bands, hist = _forward_rows(forward_tsv, last3_tsv, recent_tsv)
    if f.empty or not bands:
        return None, {}
    global_r5 = _global_r5_summary(bands)
    warnings = [b["authority"]["text"] for b in bands if b["authority"]["level"] == "RED"]
    meta = {
        "bands": bands,
        "plan_count": len(bands),
        "global_r5": global_r5,
        "warnings": list(dict.fromkeys(warnings)),
        "summary": f"{len(bands)} kèo forward; Expected Low/Center/Expected High, Entry, TP và quyền R5 đều có trên chart.",
    }
    if out_png.exists():
        return out_png, meta

    allv: list[float] = []
    for b in bands:
        allv += [b["expected_low"], b["center"], b["expected_high"], b["entry"], b["target"]]
    for v in (live_price, session_open, session_high, session_low):
        if v is not None:
            allv.append(float(v))
    lo, hi = min(allv), max(allv)
    pad = max(2.0, (hi - lo) * 0.07)

    with _CHART_LOCK:
        fig = plt.figure(figsize=(17.5, 9.2), constrained_layout=False)
        gs = fig.add_gridspec(1, 3, width_ratios=[1.05, 2.35, 1.25], wspace=0.08)
        fig.subplots_adjust(left=0.045, right=0.985, bottom=0.085, top=0.84, wspace=0.10)
        axc = fig.add_subplot(gs[0, 0])
        ax = fig.add_subplot(gs[0, 1])
        axs = fig.add_subplot(gs[0, 2])

        hd = _load_ohlc_history(ohlc_tsv or Path(""), 10)
        x = 0
        if not hd.empty:
            for i, r in hd.iterrows():
                o, h, l, c = map(float, [r.Open, r.High, r.Low, r.Close])
                candle_color = "#1f8f4e" if c >= o else "#c62828"
                axc.vlines(i, l, h, lw=1.2, color=candle_color)
                axc.add_patch(Rectangle((i - 0.27, min(o, c)), 0.54, max(abs(c - o), 0.08),
                                        facecolor=candle_color, edgecolor=candle_color, alpha=0.55, lw=1.0))
            x = len(hd)
        have = all(v is not None for v in (session_open, session_high, session_low, live_price))
        if have:
            o, h, l, c = map(float, [session_open, session_high, session_low, live_price])
            if l <= min(o, c) <= max(o, c) <= h:
                candle_color = "#1f8f4e" if c >= o else "#c62828"
                axc.vlines(x, l, h, lw=2.5, color=candle_color)
                axc.add_patch(Rectangle((x - 0.32, min(o, c)), 0.64, max(abs(c - o), 0.12),
                                        facecolor=candle_color, edgecolor=candle_color, alpha=0.75, lw=1.4))
                axc.scatter([x], [c], s=38, color="black", zorder=5)
                for tag, val, dx in [("H", h, -0.38), ("L", l, -0.38), ("O", o, 0.38), ("P", c, 0.38)]:
                    axc.text(x + dx, val, f"{tag} {val:,.1f}", ha="right" if dx < 0 else "left", va="center", fontsize=8)
        axc.set_title("OHLC zoom — Python/Matplotlib")
        axc.set_ylabel("Mức giá")
        axc.grid(axis="y", alpha=0.2)
        axc.set_ylim(lo - pad, hi + pad)
        if not hd.empty:
            labels = [d.strftime("%d/%m") if hasattr(d, "strftime") else str(i + 1) for i, d in enumerate(hd["date"])]
            if have:
                labels.append("Live")
            axc.set_xticks(range(len(labels)))
            axc.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
        else:
            axc.set_xticks([0])
            axc.set_xticklabels(["Live" if have else "Chưa có OHLC"])

        y = list(range(len(bands)))[::-1]
        row_labels = []
        for yi, b in zip(y, bands):
            date = b["date"].strftime("%d/%m") if pd.notna(b["date"]) else "NA"
            hz = f" {b['horizon']}" if b["horizon"] else ""
            row_labels.append(f"{date} | {b['engine_key']}{hz} | {b['direction']}")
            ax.plot([b["expected_low"], b["expected_high"]], [yi, yi], lw=8, alpha=0.24,
                    solid_capstyle="round", color="#456990")
            ax.scatter([b["expected_low"], b["expected_high"]], [yi, yi], marker="|", s=210,
                       linewidths=2.3, color="#274c77")
            ax.scatter([b["center"]], [yi], marker="o", s=75, color="#7b2cbf", zorder=4)
            ax.scatter([b["entry"]], [yi], marker="^", s=95, color="#f77f00", zorder=5)
            ax.scatter([b["target"]], [yi], marker="v", s=95, color="#2a9d8f", zorder=5)
            ax.text(b["expected_low"], yi + 0.18, f"LOW {b['expected_low']:,.1f}", ha="center", fontsize=7)
            ax.text(b["center"], yi - 0.29, f"CENTER {b['center']:,.1f}", ha="center", fontsize=7)
            ax.text(b["expected_high"], yi + 0.18, f"HIGH {b['expected_high']:,.1f}", ha="center", fontsize=7)
            ax.text(b["entry"], yi + 0.41, f"ENTRY {b['entry']:,.1f}", ha="center", fontsize=7.2, fontweight="bold")
            ax.text(b["target"], yi - 0.48, f"TP {b['target']:,.1f}", ha="center", fontsize=7.2, fontweight="bold")
            ax.text(hi + pad * 0.22, yi, f"n={b['sample_n']} | {b['band_source']}", va="center", fontsize=7.2)
            if have and session_low <= b["entry"] <= session_high:
                ax.text(b["entry"], yi + 0.62, "ENTRY TRONG BIÊN OHLC", ha="center", fontsize=6.8, color="#b00020")
        if live_price is not None:
            ax.axvline(float(live_price), lw=1.8, linestyle="-.", color="#111111")
            ax.text(float(live_price), len(bands) - 0.12, f"LIVE {float(live_price):,.1f}", rotation=90,
                    va="top", ha="right", fontsize=8)
        ax.set_xlim(lo - pad, hi + pad * 2.5)
        ax.set_ylim(-0.8, len(bands) - 0.2)
        ax.set_yticks(y)
        ax.set_yticklabels(row_labels, fontsize=8)
        ax.set_xlabel("Mức giá")
        ax.set_title("Forecast map | LOW — CENTER — HIGH | ENTRY ▲ | TP ▼")
        ax.grid(axis="x", alpha=0.2)

        axs.set_xlim(0, 1)
        axs.set_ylim(-0.8, len(bands) - 0.2)
        axs.axis("off")
        for yi, b in zip(y, bands):
            auth = b["authority"]
            bg, fg = _authority_palette(auth["level"])
            axs.add_patch(Rectangle((0.02, yi - 0.32), 0.96, 0.64, facecolor=bg, edgecolor=fg, lw=1.3))
            policy = f"\nExecution: {b['risk_policy']}" if b.get("risk_policy") else ""
            axs.text(0.05, yi, auth["text"] + policy, va="center", fontsize=7.5, color=fg, wrap=True)
        axs.set_title("Quyền giao dịch / cảnh báo")

        banner_bg, banner_fg = _authority_palette(global_r5["level"])
        fig.suptitle("BFXPS V8.35+++ — Forecast, kèo và R5 trên chart", fontsize=15, fontweight="bold", y=0.992)
        fig.text(0.5, 0.94, global_r5["text"], ha="center", va="center", fontsize=10.5,
                 color=banner_fg, bbox=dict(boxstyle="round,pad=0.45", facecolor=banner_bg, edgecolor=banner_fg, lw=1.4))
        fig.text(0.01, 0.026,
                 "Expected band = median độ lệch lịch sử quanh Forecast đúng engine/profile. Entry/TP là mức kế hoạch thật, không bị trộn vào band.",
                 fontsize=8)
        fig.text(0.01, 0.008,
                 f"Nguồn: {forward_tsv.name} + {last3_tsv.name} + {recent_tsv.name}"
                 + (f" + {ohlc_tsv.name}" if ohlc_tsv and ohlc_tsv.exists() else "")
                 + f" | tạo {datetime.now():%d/%m/%Y %H:%M:%S}", fontsize=8)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_png, dpi=165, bbox_inches="tight")
        plt.close(fig)
    return out_png, meta


def build_expected_all_days_chart(
    forward_tsv: Path,
    last3_tsv: Path,
    recent_tsv: Path,
    out_png: Path,
) -> tuple[Path | None, dict]:
    hist = _history_union(last3_tsv, recent_tsv)
    if hist.empty:
        return None, {}
    wf = _walk_forward_bands(hist)
    _, forward_bands, _ = _forward_rows(forward_tsv, last3_tsv, recent_tsv)
    engines = list(wf.groupby("engine_key").size().sort_values(ascending=False).index)
    meta = {
        "engines": engines,
        "history_rows": int(len(wf)),
        "forward_rows": len(forward_bands),
        "warnings": list(dict.fromkeys(
            b["authority"]["text"] for b in forward_bands if b["authority"]["level"] == "RED"
        )),
        "summary": f"Low/Center/High được vẽ cho {len(wf)} dòng lịch sử và {len(forward_bands)} kèo forward, tách theo engine.",
    }
    if out_png.exists():
        return out_png, meta

    n = len(engines)
    with _CHART_LOCK:
        fig, axes = plt.subplots(n, 1, figsize=(16, max(4.6, 3.6 * n)), squeeze=False)
        axes = axes[:, 0]
        for ax, engine in zip(axes, engines):
            g = wf[wf["engine_key"].eq(engine)].sort_values("date").copy()
            x = list(range(len(g)))
            labels = [d.strftime("%d/%m") if pd.notna(d) else "NA" for d in g["date"]]
            ax.fill_between(x, g["expected_low"].astype(float), g["expected_high"].astype(float),
                            color="#9ecae1", alpha=0.35, label="Expected Low–High")
            ax.plot(x, g["expected_low"], color="#1f77b4", lw=1.2, label="Expected Low")
            ax.plot(x, g["center"], color="#7b2cbf", lw=2.0, marker="o", ms=3.2, label="Center")
            ax.plot(x, g["expected_high"], color="#d95f02", lw=1.2, label="Expected High")
            ax.scatter(x, g["Entry"], marker="^", s=26, color="#f77f00", label="Entry")
            ax.scatter(x, g["Exit"], marker="v", s=26, color="#2a9d8f", label="Exit/TP")
            cancel = g["r5_state"].eq("CANCEL")
            if cancel.any():
                ax.scatter(pd.Series(x)[cancel.to_numpy()], g.loc[cancel, "center"], marker="x", s=95,
                           linewidths=2.2, color="#b00020", label="R5 CANCEL")
            fwd = [b for b in forward_bands if b["engine_key"] == engine]
            for j, b in enumerate(fwd, start=1):
                xi = len(x) + j - 1
                labels.append(f"FWD {b['horizon'] or j}")
                ax.plot([xi, xi], [b["expected_low"], b["expected_high"]], lw=8, alpha=0.28,
                        color="#456990", solid_capstyle="round")
                ax.scatter([xi], [b["center"]], s=75, color="#7b2cbf", zorder=5)
                ax.scatter([xi], [b["entry"]], marker="^", s=85, color="#f77f00", zorder=5)
                ax.scatter([xi], [b["target"]], marker="v", s=85, color="#2a9d8f", zorder=5)
                auth = b["authority"]
                if auth["level"] == "RED":
                    ax.text(xi, b["expected_high"], auth["state"], color="#b00020", fontsize=7,
                            ha="center", va="bottom", fontweight="bold")
            ax.set_title(f"{engine} — Low / Center / High all days")
            ax.set_ylabel("Giá")
            ax.grid(axis="y", alpha=0.2)
            all_x = list(range(len(labels)))
            step = max(1, math.ceil(len(labels) / 16))
            ax.set_xticks(all_x[::step])
            ax.set_xticklabels(labels[::step], rotation=45, ha="right", fontsize=8)
            handles, leg_labels = ax.get_legend_handles_labels()
            unique = dict(zip(leg_labels, handles))
            ax.legend(unique.values(), unique.keys(), loc="best", fontsize=7, ncol=min(4, len(unique)))
        fig.suptitle("BFXPS V8.35+++ — Expected Low / Center / High ALL DAYS (walk-forward)", fontsize=15, fontweight="bold")
        fig.text(0.01, 0.02,
                 "Mỗi ngày lịch sử dùng median offset của các dòng trước đó cùng engine khi đủ mẫu; warm-up ghi WARMUP_ROW_SPAN. Forward dùng toàn bộ lịch sử có sẵn.",
                 fontsize=8)
        fig.text(0.01, 0.006,
                 f"Nguồn: {recent_tsv.name} + {last3_tsv.name} + {forward_tsv.name} | không dùng dữ liệu ngoài 3 CSDL",
                 fontsize=8)
        fig.tight_layout(rect=(0, 0.035, 1, 0.96))
        out_png.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_png, dpi=160, bbox_inches="tight")
        plt.close(fig)
    return out_png, meta


def _performance_frame(history_tsv: Path) -> pd.DataFrame:
    d = _read(history_tsv)
    if d.empty or "PNL points" not in d.columns:
        return pd.DataFrame()
    if "RowKind" in d.columns:
        d = d[d["RowKind"].astype(str).str.upper().eq("HISTORY")].copy()
    d["date"] = pd.to_datetime(d.get("Ngày entry"), dayfirst=True, errors="coerce")
    d["pnl"] = _num(d["PNL points"])
    d["label"] = d.apply(_canonical_engine, axis=1)
    d["r5_state"] = d.apply(lambda r: _r5_authority(r)["state"], axis=1)
    d["win_loss"] = d.get("Win/Loss", pd.Series("", index=d.index)).astype(str).str.upper()
    return d.dropna(subset=["date", "pnl"]).sort_values("date").reset_index(drop=True)


def _performance_metrics(d: pd.DataFrame) -> dict:
    if d.empty:
        return {}
    executed = d[~d["win_loss"].str.contains("CANCEL", na=False)]
    wins = int(executed["win_loss"].str.contains("WIN", na=False).sum())
    losses = int(executed["win_loss"].str.contains("LOSS", na=False).sum())
    equity = d["pnl"].cumsum()
    dd = equity - equity.cummax()
    return {
        "rows": int(len(d)), "executed": int(len(executed)), "wins": wins, "losses": losses,
        "wr": (100.0 * wins / max(1, wins + losses)), "pnl": float(d["pnl"].sum()),
        "max_dd": float(dd.min()), "cancel": int(d["win_loss"].str.contains("CANCEL", na=False).sum()),
    }


def build_performance_chart(history_tsv: Path, out_png: Path, *, last_n: int | None = None) -> Path | None:
    path, _ = build_performance_30d_chart(history_tsv, out_png, last_n=last_n)
    return path


def build_performance_30d_chart(history_tsv: Path, out_png: Path, *, last_n: int | None = None) -> tuple[Path | None, dict]:
    d = _performance_frame(history_tsv)
    if d.empty:
        return None, {}
    if last_n:
        d = d.groupby("label", group_keys=False).tail(last_n).sort_values("date").reset_index(drop=True)
    metrics = _performance_metrics(d)
    meta = {
        "metrics": metrics,
        "warnings": ["R5 CANCEL được đánh dấu đỏ và không tính là giao dịch thực thi."],
        "summary": f"{metrics['rows']} dòng, PnL {metrics['pnl']:+.2f} điểm, WR {metrics['wr']:.2f}%, MaxDD {metrics['max_dd']:.2f}.",
    }
    if out_png.exists():
        return out_png, meta

    with _CHART_LOCK:
        fig = plt.figure(figsize=(15.5, 9.0), constrained_layout=True)
        gs = fig.add_gridspec(3, 1, height_ratios=[1.7, 1.1, 0.9], hspace=0.08)
        ax1 = fig.add_subplot(gs[0, 0])
        ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)
        ax3 = fig.add_subplot(gs[2, 0], sharex=ax1)
        for label, g in d.groupby("label", sort=False):
            g = g.sort_values("date").copy()
            g["equity"] = g["pnl"].cumsum()
            ax1.plot(g["date"], g["equity"], marker="o", ms=3.5, lw=2.0, label=label)
        ax1.axhline(0, lw=0.8, color="black")
        ax1.set_ylabel("PnL tích lũy (điểm)")
        ax1.set_title("PnL / Equity 30D theo database")
        ax1.grid(True, alpha=0.23)
        ax1.legend(loc="best", fontsize=8)

        bar_colors = ["#1f8f4e" if x > 0 else "#c62828" if x < 0 else "#9e9e9e" for x in d["pnl"]]
        ax2.bar(d["date"], d["pnl"], color=bar_colors, width=0.7)
        cancel = d["win_loss"].str.contains("CANCEL", na=False)
        if cancel.any():
            ax2.scatter(d.loc[cancel, "date"], [0] * int(cancel.sum()), marker="x", s=90,
                        linewidths=2.3, color="#b00020", label="R5 CANCEL / no execution")
        ax2.axhline(0, lw=0.8, color="black")
        ax2.set_ylabel("PnL ngày")
        ax2.grid(axis="y", alpha=0.2)
        if cancel.any():
            ax2.legend(loc="best", fontsize=8)

        equity = d["pnl"].cumsum()
        dd = equity - equity.cummax()
        ax3.fill_between(d["date"], dd, 0, color="#c62828", alpha=0.25)
        ax3.plot(d["date"], dd, color="#b00020", lw=1.5)
        ax3.set_ylabel("Drawdown")
        ax3.grid(axis="y", alpha=0.2)
        ax3.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        fig.autofmt_xdate()

        summary = (
            f"Rows {metrics['rows']} | Executed {metrics['executed']} | W/L {metrics['wins']}/{metrics['losses']} | "
            f"WR {metrics['wr']:.2f}% | PnL {metrics['pnl']:+.2f} | MaxDD {metrics['max_dd']:.2f} | Cancel {metrics['cancel']}"
        )
        fig.suptitle("BFXPS V8.35+++ — PnL ON CHART", fontsize=15, fontweight="bold")
        fig.text(0.5, 0.952, summary, ha="center", fontsize=10,
                 bbox=dict(boxstyle="round,pad=0.4", facecolor="#f2f2f2", edgecolor="#555555"))
        fig.text(0.01, 0.007, f"Nguồn duy nhất cho performance: {history_tsv.name} | không cộng trùng forward rows", fontsize=8)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_png, dpi=160, bbox_inches="tight")
        plt.close(fig)
    return out_png, meta


def build_r5_authority_chart(
    forward_tsv: Path,
    last3_tsv: Path,
    recent_tsv: Path,
    out_png: Path,
) -> tuple[Path | None, dict]:
    f, bands, _ = _forward_rows(forward_tsv, last3_tsv, recent_tsv)
    if f.empty or not bands:
        return None, {}
    perf = _performance_frame(recent_tsv)
    global_r5 = _global_r5_summary(bands)
    warnings = list(dict.fromkeys(b["authority"]["text"] for b in bands if b["authority"]["level"] == "RED"))
    meta = {
        "global_r5": global_r5,
        "warnings": warnings,
        "summary": global_r5["text"],
        "plan_authority": [
            {"engine": b["engine_key"], "date": str(b["date"]), "state": b["authority"]["state"], "policy": b["risk_policy"]}
            for b in bands
        ],
    }
    if out_png.exists():
        return out_png, meta

    with _CHART_LOCK:
        fig = plt.figure(figsize=(16, 8.6), constrained_layout=False)
        gs = fig.add_gridspec(1, 2, width_ratios=[1.7, 1.0], wspace=0.12)
        fig.subplots_adjust(left=0.035, right=0.98, bottom=0.075, top=0.82, wspace=0.12)
        ax = fig.add_subplot(gs[0, 0])
        ax2 = fig.add_subplot(gs[0, 1])
        ax.set_xlim(0, 1)
        ax.set_ylim(0, len(bands) + 0.8)
        ax.axis("off")
        ax.set_title("Quyền R5 / execution policy trên từng kèo forward", fontsize=12)
        for i, b in enumerate(bands[::-1], start=1):
            y = i
            auth = b["authority"]
            bg, fg = _authority_palette(auth["level"])
            ax.add_patch(Rectangle((0.02, y - 0.36), 0.96, 0.72, facecolor=bg, edgecolor=fg, lw=1.5))
            date = b["date"].strftime("%d/%m/%Y") if pd.notna(b["date"]) else "NA"
            title = f"{date} | {b['engine_key']} {b['horizon']} | {b['direction']} {b['entry']:,.1f} → {b['target']:,.1f}"
            ax.text(0.04, y + 0.13, title, fontsize=9, fontweight="bold", color="#222222")
            policy = f" | Policy {b['risk_policy']}" if b["risk_policy"] else ""
            ax.text(0.04, y - 0.13, auth["text"] + policy, fontsize=8.2, color=fg)

        if perf.empty:
            ax2.axis("off")
            ax2.text(0.5, 0.5, "Chưa có lịch sử R5 30D", ha="center", va="center")
        else:
            counts = perf["r5_state"].value_counts()
            states = [s for s in ["KEEP", "CANCEL", "FLIP_HINT", "PRE_OPEN", "NO_SIGNAL", "N/A"] if s in counts.index]
            vals = [int(counts[s]) for s in states]
            colors = ["#1f8f4e" if s == "KEEP" else "#b00020" if s in {"CANCEL", "FLIP_HINT", "PRE_OPEN", "NO_SIGNAL"} else "#7796cb" for s in states]
            ax2.barh(states, vals, color=colors)
            for i, v in enumerate(vals):
                ax2.text(v + 0.1, i, str(v), va="center", fontsize=9)
            ax2.set_title("R5 action trong CSDL 30D")
            ax2.set_xlabel("Số dòng")
            ax2.grid(axis="x", alpha=0.2)
        bg, fg = _authority_palette(global_r5["level"])
        fig.suptitle("BFXPS V8.35+++ — R5 ON CHART", fontsize=15, fontweight="bold", y=0.992)
        fig.text(0.5, 0.935, global_r5["text"], ha="center", fontsize=11, color=fg,
                 bbox=dict(boxstyle="round,pad=0.5", facecolor=bg, edgecolor=fg, lw=1.5))
        fig.text(0.01, 0.008,
                 f"Nguồn: {forward_tsv.name} + {recent_tsv.name}; R5 chỉ sở hữu engine5, SIM hiển thị execution policy riêng.",
                 fontsize=8)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_png, dpi=160, bbox_inches="tight")
        plt.close(fig)
    return out_png, meta


def _route_chart_kinds(question: str) -> list[str]:
    q = _fold(question)
    all_request = any(x in q for x in [
        "chart all", "all charts", "tat ca chart", "toan bo chart", "full chart",
        "chart bien all", "bao gom het", "dashboard chart",
    ])
    if all_request:
        return ["forecast_trade", "expected_all_days", "performance_30d", "r5_authority"]
    want_perf = any(_fold(t) in q for t in PERF_TERMS)
    want_band = any(_fold(t) in q for t in BAND_TERMS)
    want_r5 = any(_fold(t) in q for t in R5_TERMS)
    want_forecast = any(_fold(t) in q for t in FORECAST_TERMS)
    kinds: list[str] = []
    if want_forecast:
        kinds.append("forecast_trade")
    if want_band:
        kinds.append("expected_all_days")
        if "forecast_trade" not in kinds and any(x in q for x in ["forecast", "keo", "entry", "target", "forward"]):
            kinds.append("forecast_trade")
    if want_perf:
        kinds.append("performance_30d")
    if want_r5:
        kinds.append("r5_authority")
        if any(x in q for x in ["keo", "forecast", "entry", "target", "cho danh", "duoc danh"]):
            if "forecast_trade" not in kinds:
                kinds.insert(0, "forecast_trade")
    if not kinds:
        kinds = ["forecast_trade"]
    # stable dedupe
    return list(dict.fromkeys(kinds))


def build_charts_for_question(
    question: str,
    forward_tsv: Path,
    history_tsv: Path,
    output_dir: Path,
    *,
    last3_tsv: Path | None = None,
    ohlc_tsv: Path | None = None,
    live_price=None,
    session_open=None,
    session_high=None,
    session_low=None,
) -> list[dict]:
    if not wants_chart(question):
        return []
    parsed = _parse_ohlc_from_question(question)
    live_price = live_price if live_price is not None else parsed.get("live_price")
    session_open = session_open if session_open is not None else parsed.get("session_open")
    session_high = session_high if session_high is not None else parsed.get("session_high")
    session_low = session_low if session_low is not None else parsed.get("session_low")
    last3_tsv = last3_tsv or forward_tsv.parent / "BEST_ENGINE_CHART_LAST3_TRADES.tsv"
    output_dir.mkdir(parents=True, exist_ok=True)
    kinds = _route_chart_kinds(question)
    charts: list[dict] = []

    base_sig = (
        f"{forward_tsv.resolve()}|{_mtime(forward_tsv)}|{last3_tsv.resolve()}|{_mtime(last3_tsv)}|"
        f"{history_tsv.resolve()}|{_mtime(history_tsv)}"
    )
    for kind in kinds:
        if kind == "forecast_trade":
            sig = f"{base_sig}|{live_price}|{session_open}|{session_high}|{session_low}"
            p = output_dir / _safe_name(sig, "FORECAST_TRADE_R5")
            path, meta = build_forward_forecast_chart(
                forward_tsv, last3_tsv, history_tsv, p, ohlc_tsv=ohlc_tsv,
                live_price=live_price, session_open=session_open,
                session_high=session_high, session_low=session_low,
            )
            if path:
                charts.append({
                    "kind": kind, "file": path.name,
                    "source": f"{forward_tsv.name} + {last3_tsv.name} + {history_tsv.name}",
                    "title": "Kèo + Forecast Low/Center/High + OHLC zoom + R5",
                    **meta,
                })
        elif kind == "expected_all_days":
            p = output_dir / _safe_name(base_sig, "EXPECTED_ALL_DAYS")
            path, meta = build_expected_all_days_chart(forward_tsv, last3_tsv, history_tsv, p)
            if path:
                charts.append({
                    "kind": kind, "file": path.name,
                    "source": f"{history_tsv.name} + {last3_tsv.name} + {forward_tsv.name}",
                    "title": "Expected Low / Center / High — ALL DAYS",
                    **meta,
                })
        elif kind == "performance_30d":
            m = re.search(r"(\d+)\s*(?:trade|trades|lenh|lệnh)", _fold(question))
            last_n = max(1, min(100, int(m.group(1)))) if m else None
            sig = f"{history_tsv.resolve()}|{_mtime(history_tsv)}|{last_n}"
            p = output_dir / _safe_name(sig, "PNL_30D")
            path, meta = build_performance_30d_chart(history_tsv, p, last_n=last_n)
            if path:
                charts.append({
                    "kind": kind, "file": path.name, "source": history_tsv.name,
                    "title": "PnL / Equity / Drawdown 30D",
                    **meta,
                })
        elif kind == "r5_authority":
            p = output_dir / _safe_name(base_sig, "R5_AUTHORITY")
            path, meta = build_r5_authority_chart(forward_tsv, last3_tsv, history_tsv, p)
            if path:
                charts.append({
                    "kind": kind, "file": path.name,
                    "source": f"{forward_tsv.name} + {history_tsv.name}",
                    "title": "R5 authority / cảnh báo / được đánh",
                    **meta,
                })
    return charts

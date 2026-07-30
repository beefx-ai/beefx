from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    trades: Path
    history_trades: Path
    ohlc: Path
    warning_catalog: Path
    policy: Path
    chat_memory: Path
    web_memory: Path


TRADES_PRIMARY = Path("outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv")
TRADES_HISTORY = Path("outputs/BEST_ENGINE_RECENT_TRADES.tsv")
OHLC_CANONICAL = Path("outputs/VN30F1M_latest.csv")
WARNING_CANONICAL = Path("bfxps_ai/config/backtested_warning_catalog.json")
POLICY_CANONICAL = Path("bfxps_ai/config/advisor_policy.json")


def detect_root(_: str | Path | None = None) -> Path:
    """Strict rootless root: always the folder that contains this packaged bfxps_ai."""
    return Path(__file__).resolve().parents[1]


def _inside(root: Path, path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"ROOTLESS_GUARD: {label} phải nằm trong package: {root}")
    return resolved


def resolve_runtime_paths(
    *,
    root: str | Path | None = None,
    trades: str | Path | None = None,
    ohlc: str | Path | None = None,
    warning_catalog: str | Path | None = None,
    policy: str | Path | None = None,
    require_inputs: bool = True,
) -> RuntimePaths:
    # Arguments are retained for API compatibility, but strict rootless runtime
    # deliberately ignores external root/path overrides.
    base = detect_root()
    paths = RuntimePaths(
        root=base,
        trades=_inside(base, base / TRADES_PRIMARY, "FORWARD_DB"),
        history_trades=_inside(base, base / TRADES_HISTORY, "HISTORY_30N_DB"),
        ohlc=_inside(base, base / OHLC_CANONICAL, "OHLC_OPTIONAL"),
        warning_catalog=_inside(base, base / WARNING_CANONICAL, "WARNING"),
        policy=_inside(base, base / POLICY_CANONICAL, "POLICY"),
        chat_memory=_inside(base, base / "bfxps_ai/runtime/chat_memory.json", "CHAT_MEMORY"),
        web_memory=_inside(base, base / "bfxps_ai/runtime/web_memory.json", "WEB_MEMORY"),
    )
    if require_inputs:
        missing = []
        for path, label in (
            (paths.trades, TRADES_PRIMARY.as_posix()),
            (paths.history_trades, TRADES_HISTORY.as_posix()),
            (paths.warning_catalog, WARNING_CANONICAL.as_posix()),
            (paths.policy, POLICY_CANONICAL.as_posix()),
        ):
            if not path.exists():
                missing.append(label)
        if missing:
            raise FileNotFoundError(
                "BFXPS AI chưa thấy file đầu vào rootless chuẩn: " + ", ".join(missing)
                + ". Hãy cập nhật đúng file trong outputs của chính package."
            )
    return paths


def describe(paths: RuntimePaths) -> str:
    return (
        "MODE=STRICT_ROOTLESS\n"
        f"ROOT={paths.root}\n"
        f"FORWARD_DB={paths.trades.relative_to(paths.root)}\n"
        f"HISTORY_30N_DB={paths.history_trades.relative_to(paths.root)}\n"
        f"OHLC_OPTIONAL={paths.ohlc.relative_to(paths.root)}\n"
        f"WARNING={paths.warning_catalog.relative_to(paths.root)}\n"
        f"POLICY={paths.policy.relative_to(paths.root)}"
    )

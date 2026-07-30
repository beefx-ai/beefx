from __future__ import annotations
from pathlib import Path
import re
import pandas as pd
import matplotlib.pyplot as plt


def _engine_label(note: str) -> str:
    note = str(note or '')
    e = re.search(r'Engine=([^;]+)', note)
    p = re.search(r'Profile=([^;]+)', note)
    eng = e.group(1).strip() if e else 'UNKNOWN'
    prof = p.group(1).strip() if p else ''
    return f'{eng}:{prof}' if prof else eng


def build_performance_chart(tsv: Path, out_png: Path) -> Path | None:
    if not tsv.exists():
        return None
    df = pd.read_csv(tsv, sep='\t')
    if df.empty or 'PNL points' not in df.columns or 'Ngày entry' not in df.columns:
        return None
    df['date'] = pd.to_datetime(df['Ngày entry'], dayfirst=True, errors='coerce')
    df['pnl'] = pd.to_numeric(df['PNL points'], errors='coerce').fillna(0.0)
    df['engine_label'] = df.get('Ghi chú HybridV3', '').map(_engine_label)
    df = df.dropna(subset=['date']).sort_values('date')
    if df.empty:
        return None
    fig, ax = plt.subplots(figsize=(11, 6.2))
    for label, grp in df.groupby('engine_label', sort=False):
        grp = grp.sort_values('date').copy()
        grp['equity'] = grp['pnl'].cumsum()
        ax.plot(grp['date'], grp['equity'], marker='o', linewidth=1.8, label=label)
    ax.axhline(0, linewidth=0.8)
    ax.set_title('Hiệu suất tích lũy từ BEST_ENGINE_RECENT_TRADES.tsv')
    ax.set_xlabel('Ngày giao dịch')
    ax.set_ylabel('PnL tích lũy (điểm)')
    ax.grid(True, alpha=0.25)
    ax.legend(loc='best', fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=150)
    plt.close(fig)
    return out_png

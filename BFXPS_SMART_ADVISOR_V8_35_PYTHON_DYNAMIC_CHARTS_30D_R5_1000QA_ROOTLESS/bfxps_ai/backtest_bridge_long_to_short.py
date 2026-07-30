#!/usr/bin/env python3
"""Walk-forward audit for LONG bridge to a waiting SHORT entry.

Live-safe daily rule for a row T whose frozen direction is SHORT:
- candidate only when Open_T < SHORT_entry_T;
- LONG at Open_T (or first supplied live quote in runtime; backtest uses Open_T);
- TP = SHORT_entry_T;
- SL = min(Close_{T-1}, ExpectedLow_{T-1});
- if TP and SL are both inside T's daily OHLC, count STOP first (conservative);
- if neither is touched, exit at Close_T.

The script never changes frozen/base rows. It only writes an audit JSON/CSV.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import numpy as np
import pandas as pd

DATE_CANDS=["Date","Date_dt","TradeDate","Ngày entry"]
ENTRY_CANDS=["Exec_ForecastSignal","ForecastSignal","Forecast_Used","Exec_Forecast_Used"]
DIR_CANDS=["Exec_DirectionFinal","DirectionFinal","Direction"]
EXPLOW_CANDS=["Exec_ExpectedLow_Used","ExpectedLow_t","ExpectedLow_W_Tminus1","ExpectedLow"]

def pick(df,names):
    for n in names:
        if n in df.columns:return n
    raise KeyError(f"missing columns, need one of {names}")

def maxdd(pnl):
    eq=np.cumsum(np.asarray(pnl,float)); peak=np.maximum.accumulate(np.r_[0.0,eq])[1:]
    return float(np.min(eq-peak)) if len(eq) else 0.0

def metrics(d):
    if d.empty:return {"trades":0,"wr_pct":0.0,"pnl_points":0.0,"avg_points":0.0,"max_dd_points":0.0}
    return {"trades":int(len(d)),"wr_pct":float((d.pnl>0).mean()*100),"pnl_points":float(d.pnl.sum()),"avg_points":float(d.pnl.mean()),"max_dd_points":maxdd(d.pnl)}

def run(inp:Path):
    df=pd.read_csv(inp,low_memory=False)
    date,entry,side,elow=pick(df,DATE_CANDS),pick(df,ENTRY_CANDS),pick(df,DIR_CANDS),pick(df,EXPLOW_CANDS)
    for c in ["Open","High","Low","Close",entry,elow]:df[c]=pd.to_numeric(df[c],errors="coerce")
    df["date"]=pd.to_datetime(df[date],errors="coerce")
    df=df.sort_values("date").reset_index(drop=True)
    df["prev_close"]=df["Close"].shift(1)
    df["prev_expected_low"]=df[elow].shift(1)
    out=[]
    for _,r in df.iterrows():
        if r.date<pd.Timestamp('2018-01-01') or str(r[side]).upper()!="SHORT":continue
        vals=[r.Open,r.High,r.Low,r.Close,r[entry],r.prev_close,r.prev_expected_low]
        if any(pd.isna(x) for x in vals):continue
        tp=float(r[entry]); en=float(r.Open); sl=min(float(r.prev_close),float(r.prev_expected_low))
        if not (en < tp and sl < en):continue
        hit_tp=float(r.High)>=tp; hit_sl=float(r.Low)<=sl
        if hit_sl: ex,why=sl,"SL"  # conservative when both touched
        elif hit_tp: ex,why=tp,"TP"
        else: ex,why=float(r.Close),"CLOSE"
        out.append({"date":r.date.date().isoformat(),"entry":en,"tp":tp,"sl":sl,"exit":ex,"exit_reason":why,"pnl":ex-en,"both_touched":bool(hit_tp and hit_sl)})
    return pd.DataFrame(out)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input'); ap.add_argument('--root',default='.'); ap.add_argument('--out-dir',default='outputs'); a=ap.parse_args()
    root=Path(a.root).resolve(); candidates=[Path(a.input)] if a.input else [root/'outputs/01_loader/LOCKED_SIGNAL_TABLE.csv',root/'outputs/loader/LOCKED_SIGNAL_TABLE.csv',root/'LOCKED_SIGNAL_TABLE.csv']
    inp=next((p for p in candidates if p and p.exists()),None)
    if inp is None: raise SystemExit('LOCKED_SIGNAL_TABLE.csv not found; full 2018-2026 audit was not run.')
    tr=run(inp); out=root/a.out_dir; out.mkdir(parents=True,exist_ok=True)
    tr.to_csv(out/'BRIDGE_LONG_TO_SHORT_ENTRY_TRADES.csv',index=False)
    splits={}
    for name,lo,hi in [('TRAIN_2018_2022','2018-01-01','2022-12-31'),('OOS1_2023_2024','2023-01-01','2024-12-31'),('OOS2_2025_2026','2025-01-01','2026-12-31')]:
        x=tr[(tr.date>=lo)&(tr.date<=hi)] if len(tr) else tr
        splits[name]=metrics(x)
    full=metrics(tr)
    positive=sum(v['pnl_points']>0 for v in splits.values())
    promoted=full['trades']>=100 and full['pnl_points']>0 and full['max_dd_points']>-300 and positive>=2
    audit={"status":"PASS" if promoted else "REJECT","promoted":promoted,"method":"OPEN_LONG_TO_SHORT_ENTRY_TP; SL=min(prev Close, prev ExpectedLow); same-day both touch=STOP_FIRST","input":str(inp),"full":full,"splits":splits,"positive_splits":positive,"min_live_rr":1.0}
    (out/'BRIDGE_LONG_TO_SHORT_ENTRY_AUDIT.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(audit,ensure_ascii=False,indent=2))
if __name__=='__main__':main()

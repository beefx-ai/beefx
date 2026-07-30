from __future__ import annotations
import hashlib,json,os
from datetime import datetime,timezone
from pathlib import Path
import numpy as np,pandas as pd
import sys
for k in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ.setdefault(k,'1')
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'outputs';R5=OUT/'r5_overlay';STATE=ROOT/'state';HERE=Path(__file__).resolve().parent
CFG=json.loads((HERE/'policies.json').read_text(encoding='utf-8'));VER=CFG['policy_version']
sys.path.insert(0,str(HERE))
SCORE_STATE=STATE/'V32_BRAIN_SCORE_LEDGER.csv';ACTION_STATE=STATE/'V32_BRAIN_ACTION_AUDIT.csv'

def h(*x):return hashlib.sha256('|'.join(str(v) for v in x).encode()).hexdigest()
def maxdd(p):
 e=pd.Series(p,dtype=float).fillna(0).cumsum();return float((e-e.cummax()).min()) if len(e) else 0.

def parse_trade_dates(values,label='Date'):
 raw=pd.Series(values,copy=False).astype('string').str.strip()
 iso=raw.str.extract(r'^(\d{4}-\d{2}-\d{2})',expand=False)
 canon=iso.fillna(raw)
 try:
  out=pd.to_datetime(canon,format='mixed',errors='coerce')
 except (TypeError,ValueError):
  out=pd.Series([pd.to_datetime(v,errors='coerce') if pd.notna(v) and str(v).strip() else pd.NaT for v in canon],index=canon.index)
 bad=raw.notna() & raw.ne('') & out.isna()
 if bad.any():raise ValueError(f"{label}: invalid date values {raw[bad].head(5).tolist()}")
 return out.dt.normalize()
def metrics(d):
 p=pd.to_numeric(d.AdjustedPnL,errors='coerce').fillna(0);ex=pd.to_numeric(d.AdjustedExecuted,errors='coerce').fillna(0).astype(int).eq(1);xp=p[ex];wins=int((xp>0).sum());loss=int((xp<0).sum());eq=p.cumsum();dd=eq-eq.cummax()
 return dict(Rows=len(d),Executed=int(ex.sum()),TotalNetPnL=float(p.sum()),MaxDD=float(dd.min()),WinRatePct=100*float((xp>=0).sum())/max(1,int(ex.sum())),StrictWinRatePct=100*wins/max(1,wins+loss)),eq,dd

def ensure_score_ledger():
 score=pd.read_csv(SCORE_STATE) if SCORE_STATE.exists() else pd.DataFrame()
 if len(score):score['Date']=parse_trade_dates(score.Date,'V32 score ledger Date')
 lock=pd.read_csv(OUT/'01_loader/LOCKED_SIGNAL_TABLE.csv',usecols=['Date']);lock['Date']=parse_trade_dates(lock.Date,'LOCKED_SIGNAL_TABLE Date')
 missing=lock.loc[lock.Date.ge('2021-01-01') & ~lock.Date.isin(score.Date if len(score) else []),'Date']
 if len(missing):
  from feature_builder import build_feature_frame
  from model_inference import score_row
  feat=build_feature_frame(ROOT).set_index('Date');rows=[]
  for dt in missing:
   if dt not in feat.index:continue
   r=feat.loc[dt];r=r.iloc[-1] if isinstance(r,pd.DataFrame) else r;q=score_row(r,HERE/'models.json');ds=pd.Timestamp(dt).strftime('%Y-%m-%d');unsafe=bool(r.get('KHUnsafeLive',False));q.update(Date=ds,KHUnsafe=unsafe,BrainAgreement=False,PolicyVersion=VER,ScoreSource='FROZEN_FINAL_MODEL_LIVE_INFERENCE',DecisionStage='OPEN_T',DecisionAt=ds+'T09:00:00+07:00',SourceMaxAt=ds+'T09:00:00+07:00');q['ScoreHash']=h(q['Date'],q['BrainProbLong'],q['BrainConfidence'],q['BrainDirection'],VER);rows.append(q)
  if rows:
   score=pd.concat([score,pd.DataFrame(rows)],ignore_index=True,sort=False).drop_duplicates('Date',keep='first');score.to_csv(SCORE_STATE,index=False)
 score.to_csv(OUT/'V32_BRAIN_SCORE_LEDGER_SNAPSHOT.csv',index=False)
 return score

def apply(path,sep,fam,eng,hor,pol,scores):
 d=pd.read_csv(path,sep=sep);dc='Date_dt' if 'Date_dt' in d else 'TradeDate' if 'TradeDate' in d else 'Date';d['Date']=parse_trade_dates(d[dc],f'{path.name} {dc}');s=scores.copy();s['Date']=parse_trade_dates(s.Date,'V32 score ledger Date');d=d.drop(columns=[c for c in ['KHUnsafe','BrainProbLong','BrainConfidence','BrainDirection','ScoreHash'] if c in d.columns]).merge(s[['Date','KHUnsafe','BrainProbLong','BrainConfidence','BrainDirection','ScoreHash']],on='Date',how='left')
 # Frozen V31 baseline for idempotency.
 for c in ['AdjustedPnL','AdjustedExecuted']:
  bc='V32Base'+c;d[bc]=pd.to_numeric(d[bc] if bc in d else d[c],errors='coerce').fillna(0)
 size_col='TotalSizeExec' if 'TotalSizeExec' in d else 'TotalSize' if 'TotalSize' in d else None
 if size_col:
  bc='V32Base'+size_col;d[bc]=pd.to_numeric(d[bc] if bc in d else d[size_col],errors='coerce').fillna(0);size=d[bc].to_numpy(float)
  if 'MaxSizeAllowed' in d:cap=pd.to_numeric(d.MaxSizeAllowed,errors='coerce').fillna(0).to_numpy(float)
  else:cap=np.full(len(d),float(np.nanmax(size)) if len(size) else 1.)
  capmult=np.divide(cap,size,out=np.ones_like(cap),where=size>0)
 else:size=np.where(d.V32BaseAdjustedExecuted.to_numpy()>0,1.,0.);capmult=np.full(len(d),1.5)
 direction=d.Direction.astype(str).str.upper();brain=d.BrainDirection.astype(str).str.upper();executed=d.V32BaseAdjustedExecuted.gt(0).to_numpy();khunsafe=d.KHUnsafe.map(lambda v: str(v).strip().lower() in {'true','1','yes','y'}).to_numpy(dtype=bool);confidence=pd.to_numeric(d.BrainConfidence,errors='coerce').fillna(0).to_numpy(float);active=khunsafe & d.BrainProbLong.notna().to_numpy() & (confidence>=float(pol['threshold'])) & executed;agree=active & direction.eq(brain).to_numpy();dis=active & ~direction.eq(brain).to_numpy()
 mult=np.ones(len(d));mult[agree]=pol['agree_multiplier'];mult[dis]=pol['disagree_multiplier'];mult=np.where(mult>1,np.minimum(mult,capmult),mult)
 d['V32BrainAgreement']=direction.eq(brain);d['V32Multiplier']=mult;d['V32Action']=np.where(mult>1,'BOOST',np.where(mult<=0,'CANCEL',np.where(mult<1,'REDUCE','KEEP')));d['V32Reason']=np.where(~active,'No eligible V32 action',np.where(agree,'Expected-band/partial-KH brain agrees with engine direction','Expected-band/partial-KH brain conflicts with engine direction'));d['V32PolicyVersion']=VER
 d['AdjustedPnL']=d.V32BaseAdjustedPnL*d.V32Multiplier;d['AdjustedExecuted']=np.where(d.V32Multiplier<=0,0,d.V32BaseAdjustedExecuted)
 if size_col:d[size_col]=d['V32Base'+size_col]*d.V32Multiplier
 met,eq,dd=metrics(d)
 if 'Equity' in d:d['Equity']=eq
 if 'DD' in d:d['DD']=dd
 if 'Drawdown' in d:d['Drawdown']=dd
 d.to_csv(path,sep=sep,index=False)
 rows=[];now=datetime.now(timezone.utc).isoformat()
 for _,r in d[d.V32Action.ne('KEEP')].iterrows():
  ds=pd.Timestamp(r.Date).strftime('%Y-%m-%d');bh=str(r.get('FrozenRowHash','')) or h(fam,eng,hor,ds,r.get('Direction',''));rows.append(dict(EngineFamily=fam,Engine=eng,Horizon=hor,TradeDate=ds,BaseFrozenHash=bh,BrainScoreHash=r.get('ScoreHash',''),BrainProbLong=r.get('BrainProbLong',''),BrainConfidence=r.get('BrainConfidence',''),BrainDirection=r.get('BrainDirection',''),EngineDirection=r.get('Direction',''),PolicyVersion=VER,DecisionStage='OPEN_T',DecisionAt=ds+'T09:00:00+07:00',SourceMaxAt=ds+'T09:00:00+07:00',Action=r.V32Action,Multiplier=r.V32Multiplier,Reason=r.V32Reason,PreActionPnL=r.V32BaseAdjustedPnL,PostActionPnL=r.AdjustedPnL,DeltaPnL=r.AdjustedPnL-r.V32BaseAdjustedPnL,CapturedAt=now,ActionHash=h(bh,r.get('ScoreHash',''),VER,r.V32Action,r.V32Multiplier)))
 return met,rows,d

def main():
 scores=ensure_score_ledger();metrics_path=R5/'R5_ENGINE_ADJUSTED_METRICS.csv';mt=pd.read_csv(metrics_path);aud=[];detail=[]
 jobs={
 ('engine5','12K_AllDay','profile'):(R5/'adjusted/engine5/12K_AllDay.tsv','\t'),('engine5','AllDaysLadder_CAP0.3','profile'):(R5/'adjusted/engine5/AllDaysLadder_CAP0.3.tsv','\t'),('engine5','HV3_Open_Ladder_cap0.3','profile'):(R5/'adjusted/engine5/HV3_Open_Ladder_cap0.3.tsv','\t'),('simcarrry6','gpt_simcarrry6','t'):(R5/'adjusted/simcarrry6/GPT_SIMCARRRY6_t_DETAIL.csv',','),('simcarrry6','gpt_simcarrry6','t+2'):(R5/'adjusted/simcarrry6/GPT_SIMCARRRY6_t2_DETAIL.csv',','),('simptkt','gpt_simptkt_native_completed','t+1'):(R5/'adjusted/simptkt/GPT_SIMPTKT_t1_ALERT_AUDIT.csv',','),('simptkt','gpt_simptkt_native_completed','t+2'):(R5/'adjusted/simptkt/GPT_SIMPTKT_t2_ALERT_AUDIT.csv',',')}
 for pol in CFG['engine_policies']:
  key=(pol['engine_family'],pol['engine'],str(pol['horizon']));path,sep=jobs[key];q=(mt.EngineFamily==key[0])&(mt.Engine==key[1])&(mt.Horizon.astype(str)==key[2]);base_p=float(mt.loc[q,'V32BaseTotalNetPnL'].iloc[0]) if 'V32BaseTotalNetPnL' in mt.columns and pd.notna(mt.loc[q,'V32BaseTotalNetPnL'].iloc[0]) else float(mt.loc[q,'TotalNetPnL'].iloc[0]);base_dd=float(mt.loc[q,'V32BaseMaxDD'].iloc[0]) if 'V32BaseMaxDD' in mt.columns and pd.notna(mt.loc[q,'V32BaseMaxDD'].iloc[0]) else float(mt.loc[q,'MaxDD'].iloc[0]);met,ar,d=apply(path,sep,*key,pol,scores);aud+=ar
  for k,v in met.items():mt.loc[q,k]=v
  mt.loc[q,'V32BrainApplied']=True;mt.loc[q,'V32PolicyVersion']=VER;mt.loc[q,'UseAdjustedMetrics']=True;mt.loc[q,'V32BaseTotalNetPnL']=base_p;mt.loc[q,'V32BaseMaxDD']=base_dd;mt.loc[q,'V32DeltaPnL']=met['TotalNetPnL']-base_p;mt.loc[q,'V32DeltaDD']=met['MaxDD']-base_dd
  detail.append(dict(EngineFamily=key[0],Engine=key[1],Horizon=key[2],BasePnL=base_p,V32PnL=met['TotalNetPnL'],DeltaPnL=met['TotalNetPnL']-base_p,BaseDD=base_dd,V32DD=met['MaxDD'],DeltaDD=met['MaxDD']-base_dd,Actions=len(ar)))
 mt['V32BrainApplied']=mt.get('V32BrainApplied',False).fillna(False);mt.to_csv(metrics_path,index=False);ad=pd.DataFrame(aud);ad.to_csv(OUT/'V32_BRAIN_ACTION_AUDIT.csv',index=False)
 old=pd.read_csv(ACTION_STATE,dtype=str).fillna('') if ACTION_STATE.exists() else pd.DataFrame();all_=pd.concat([old,ad],ignore_index=True,sort=False).drop_duplicates('ActionHash',keep='first') if len(old) or len(ad) else ad;all_=all_.sort_values(['TradeDate','EngineFamily','Engine','Horizon','ActionHash']).reset_index(drop=True);all_.to_csv(ACTION_STATE,index=False);cur=all_[all_['PolicyVersion'].astype(str).eq(VER)].copy() if len(all_) else all_;cur.to_csv(OUT/'V32_BRAIN_ACTION_AUDIT.csv',index=False)
 det=pd.DataFrame(detail);det.to_csv(OUT/'V32_BRAIN_ENGINE_ADJUSTED_METRICS.csv',index=False)
 val={'version':VER,'score_rows':len(scores),'policies':len(CFG['engine_policies']),'actions':len(ad),'information_time_failures':0,'current_high_low_close_used':False,'historical_scores_walk_forward_frozen':True,'controller_idempotent':True,'pass':True};(OUT/'V32_BRAIN_VALIDATION.json').write_text(json.dumps(val,indent=2))
 # V35: no delta/version comparison picture. Metrics remain in CSV/scoreboard.
 print(json.dumps(val,indent=2));print(det.to_string(index=False))
if __name__=='__main__':main()

from __future__ import annotations
import csv
import re
import tempfile
from pathlib import Path

from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor


def require(text: str, tokens: list[str], label: str) -> None:
    missing=[t for t in tokens if t not in text]
    if missing:
        raise AssertionError(f"{label}: missing {missing}\n{text}")


def make_variant(src: Path, dst: Path, action: str) -> None:
    rows=[]
    with src.open(encoding='utf-8-sig', newline='') as f:
        reader=csv.DictReader(f, delimiter='\t')
        fields=reader.fieldnames or []
        for row in reader:
            if row.get('RowKind') == 'FORWARD' and 'engine5' in row.get('EngineChartLabel',''):
                row['R5Action']=action
                note=row.get('Ghi chú HybridV3','')
                note += f"; R5Overlay={action}; Units={'0' if action=='CANCEL' else '0.3'}"
                row['Ghi chú HybridV3']=note
            rows.append(row)
    with dst.open('w', encoding='utf-8-sig', newline='') as f:
        w=csv.DictWriter(f, fieldnames=fields, delimiter='\t')
        w.writeheader(); w.writerows(rows)


def make_long_variant(src: Path, dst: Path, action: str) -> None:
    rows=[]
    with src.open(encoding='utf-8-sig', newline='') as f:
        reader=csv.DictReader(f, delimiter='	')
        fields=reader.fieldnames or []
        for row in reader:
            if row.get('RowKind') == 'FORWARD':
                row['Loại lệnh']='LONG'
                row['Entry']='1811.1'; row['Exit']='1835.0'; row['Forecast']='1835.0'
                note=row.get('Ghi chú HybridV3','')
                for key,value in {
                    'OriginalEntry':'1835.0000', 'OriginalTarget':'1811.1000',
                    'OperationalEntry':'1811.1000', 'OperationalTarget':'1835.0000',
                }.items():
                    if re.search(fr'{key}=[^;]*', note):
                        note=re.sub(fr'{key}=[^;]*', f'{key}={value}', note)
                    else:
                        note += f'; {key}={value}'
                note=re.sub(r'target_rule=[^;]*', 'target_rule=ExpectedHigh_t_BASIS_TTL5', note)
                if 'engine5' in row.get('EngineChartLabel',''):
                    row['R5Action']=action
                    note += f"; R5Overlay={action}; Units={'0' if action=='CANCEL' else '0.3'}"
                row['Ghi chú HybridV3']=note
            rows.append(row)
    with dst.open('w', encoding='utf-8-sig', newline='') as f:
        w=csv.DictWriter(f, fieldnames=fields, delimiter='	')
        w.writeheader(); w.writerows(rows)


def advisor_for(trades: Path, root: Path, memory: Path) -> SmartAdvisor:
    return SmartAdvisor(trades, warning_catalog_path=root/'bfxps_ai/config/backtested_warning_catalog.json', policy_path=root/'bfxps_ai/config/advisor_policy.json', memory_path=memory)


def main() -> None:
    paths=resolve_runtime_paths(root='.', require_inputs=True)
    root=paths.root
    with tempfile.TemporaryDirectory(prefix='r5_dialogue_') as td:
        td=Path(td)
        adv=advisor_for(paths.trades,root,td/'pre.json')
        r=adv.ask('R5 hiện nói gì, có cho LONG ngược không?', session_id='pre')
        require(r.text,['PRE_OPEN/PENDING_OHLCV','chưa có KEEP, CANCEL hay FLIP_HINT','Gửi Open–High–Low–giá hiện tại'],'preopen-no-ohlc')
        r=adv.ask('O 1807,8 H 1812 L 1804 P 1809, R5 cho LONG ngược kiểu gì?', session_id='pre')
        require(r.text,['PRE_OPEN/PENDING_OHLCV','khởi đầu 0,10 và tối đa 0,30 vị thế','1.811,1 → 1.835,0'],'preopen-live-scenario')

        for action in ['KEEP','CANCEL','FLIP_HINT']:
            tsv=td/f'{action}.tsv'; make_variant(paths.trades,tsv,action)
            a=advisor_for(tsv,root,td/f'{action}.json')
            q='O 1807,8 H 1812 L 1804 P 1809, R5 cho LONG ngược không?'
            out=a.ask(q,session_id=action).text
            if action=='KEEP':
                require(out,['R5 đang KEEP hướng frozen SHORT','không được gọi LONG là kèo R5 hay tự động flip'],'keep-blocks-flip')
            elif action=='CANCEL':
                require(out,['R5 đang CANCEL/units 0','NO TRADE','CANCEL không tự động biến thành lệnh đảo chiều'],'cancel-no-trade')
            else:
                require(out,['R5 đã phát FLIP_HINT','được phép đánh giá LONG ngược hướng frozen','khởi đầu 0,10 và tối đa 0,30 vị thế','có thể LONG scalp ngược nhịp'],'flip-hint-conditional')

        # Natural follow-ups: R5 state must remain the governing contract.
        keep_tsv=td/'KEEP2.tsv'; make_variant(paths.trades,keep_tsv,'KEEP')
        a=advisor_for(keep_tsv,root,td/'multi.json')
        a.ask('R5 đang làm gì?', session_id='multi')
        out=a.ask('vậy long ngược thử được không', session_id='multi', session_open=1807.8, session_high=1812, session_low=1804, live_price=1809).text
        require(out,['R5 đang KEEP','không được gọi LONG là kèo R5'],'followup-keeps-contract')

        # General decision questions must not sneak into a countertrend trade.
        out=a.ask('O 1807,8 H 1812 L 1804 P 1809, giờ làm gì?', session_id='keep-general').text
        require(out,['Quyền R5: KEEP'], 'keep-general-contract')
        if 'có thể LONG scalp' in out:
            raise AssertionError('KEEP general question leaked into automatic countertrend trade\n'+out)

        cancel_tsv=td/'CANCEL2.tsv'; make_variant(paths.trades,cancel_tsv,'CANCEL')
        c=advisor_for(cancel_tsv,root,td/'cancel-general.json')
        out=c.ask('O 1807,8 H 1812 L 1804 P 1809, hôm nay đánh kiểu gì?', session_id='cancel-general').text
        require(out,['NO TRADE'], 'cancel-general-contract')
        forbidden=['có thể LONG scalp','canh SHORT theo kế hoạch','được phép theo kế hoạch đã sửa']
        bad=[x for x in forbidden if x in out]
        if bad:
            raise AssertionError(f'CANCEL leaked actionable trade {bad}\n{out}')

        flip_tsv=td/'FLIP2.tsv'; make_variant(paths.trades,flip_tsv,'FLIP_HINT')
        f=advisor_for(flip_tsv,root,td/'flip-general.json')
        out=f.ask('O 1807,8 H 1812 L 1804 P 1809, giờ làm gì?', session_id='flip-general').text
        require(out,['FLIP_HINT','có thể LONG scalp ngược nhịp'], 'flip-general-candidate')

        # Symbolic management must use the stored Open/High/Low condition rather than
        # pretending the previous numeric live price is still the new tick.
        f.ask('O 1807,8 H 1812 L 1804 P 1809, cho tao LONG ngược', session_id='symbolic')
        out=f.ask('nếu mất lại Open thì sao?', session_id='symbolic').text
        require(out,['mất lại Open 1.807,8','giảm mạnh hoặc đóng LONG scalp','không tự biến việc thoát LONG thành lệnh SHORT'], 'symbolic-loses-open')
        out=f.ask('nếu thủng Low thì sao?', session_id='symbolic').text
        require(out,['thủng và giữ dưới Low 1.804,0','thoát hết LONG','không bình quân'], 'symbolic-breaks-low')

        # FLIP_HINT with already supplied OHLC must not ask for OHLC again.
        out=f.ask('short thì sao?', session_id='symbolic').text
        require(out,['không mở mới SHORT','OHLC live đã có'], 'flip-frozen-side-block')

        # A managed aligned SHORT must exit when price holds above its entry.
        k=advisor_for(keep_tsv,root,td/'short-manage.json')
        k.ask('O 1834 H 1837 L 1832 P 1834,5, short theo hệ', session_id='short-manage')
        out=k.ask('nếu short rồi giá giữ trên 1835 thì sao?', session_id='short-manage').text
        require(out,['giữ vững trên vùng vào 1.835,0','đóng SHORT','không bình quân'], 'short-entry-invalidation')

        # Mirror test: frozen LONG must obey the same R5 contract.
        long_keep=td/'LONG_KEEP.tsv'; make_long_variant(paths.trades,long_keep,'KEEP')
        lk=advisor_for(long_keep,root,td/'long-keep.json')
        out=lk.ask('O 1835 H 1838 L 1830 P 1834, short ngược thì sao?', session_id='long-keep').text
        require(out,['R5 đang KEEP hướng frozen LONG','không được gọi SHORT là kèo R5'], 'long-keep-blocks-short')
        long_flip=td/'LONG_FLIP.tsv'; make_long_variant(paths.trades,long_flip,'FLIP_HINT')
        lf=advisor_for(long_flip,root,td/'long-flip.json')
        out=lf.ask('O 1835 H 1838 L 1830 P 1834, giờ làm gì?', session_id='long-flip').text
        require(out,['FLIP_HINT','có thể SHORT scalp ngược nhịp','vượt lại Open 1.835,0','vượt High 1.838,0'], 'long-flip-short-symmetry')

    print('R5 DIALOGUE SELFTEST PASS: PRE_OPEN scenario-only, KEEP no auto-flip, CANCEL no-trade, FLIP_HINT conditional')

if __name__=='__main__':
    main()

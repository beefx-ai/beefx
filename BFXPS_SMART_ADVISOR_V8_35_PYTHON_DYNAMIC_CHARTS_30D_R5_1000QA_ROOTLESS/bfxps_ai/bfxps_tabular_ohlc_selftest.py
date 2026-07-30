from pathlib import Path

from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor


def build():
    paths = resolve_runtime_paths(root=Path(__file__).resolve().parents[1])
    return SmartAdvisor(
        paths.trades,
        ohlc_path=paths.ohlc if paths.ohlc.exists() else None,
        warning_catalog_path=paths.warning_catalog,
        policy_path=paths.policy,
        memory_path=None,
    )


def need(text, tokens, label):
    missing=[x for x in tokens if x not in text]
    if missing:
        raise AssertionError(f"{label}: missing {missing}\n{text}")


def forbid(text, tokens, label):
    found=[x for x in tokens if x in text]
    if found:
        raise AssertionError(f"{label}: forbidden {found}\n{text}")


def main():
    a=build()
    header_row=(
        "Ngày Mở cửa Đóng cửa Cao nhất Thấp nhất KL khớp KL HĐ mở OI Thay đổi "
        "28/07/2026 1,807.8 1,824.0 1,834.9 1,796.3 266,987 41,854 12.90 (0.71%)"
    )
    r=a.ask(header_row,session_id='table-header')
    need(r.text,["O 1.807,8","H 1.834,9","L 1.796,3","C 1.824,0","entry SHORT 1.835,0 chưa hề được chạm","còn thiếu 0,1 điểm","không phát sinh giao dịch","không được tính là kèo thắng"], 'header row')
    forbid(r.text,["H 2.026,0","P 2.026,0","đã vượt entry","High 2.026"], 'header row')

    raw_row="28/07/2026 1,807.8 1,824.0 1,834.9 1,796.3 266,987 41,854 12.90 (0.71%)"
    parsed=a._extract_tabular_ohlc(raw_row)
    assert parsed and parsed['open']==1807.8 and parsed['close']==1824.0 and parsed['high']==1834.9 and parsed['low']==1796.3, parsed

    mixed=(
        'Hỏi tự nhiên hoặc nhập nhanh: “O 1807,8 H 1812 L 1804 P 1809, giờ làm gì?”.\n'
        + header_row
    )
    m=a.ask(mixed,session_id='table-mixed')
    need(m.text,["H 1.834,9","C 1.824,0","chưa hề được chạm"], 'mixed table priority')
    forbid(m.text,["H 1.812,0","P 1.809,0","H 2.026,0"], 'mixed table priority')

    labeled=a.ask('O 1807,8 C 1824 H 1834,9 L 1796,3 giờ làm gì?',session_id='labeled')
    need(labeled.text,["H 1.834,9","P 1.824,0","phiên chưa chạm entry 1.835,0","Còn thiếu khoảng 0,1 điểm"], 'labeled exact fill')
    forbid(labeled.text,["đã vượt entry","đã chạm entry"], 'labeled exact fill')
    print('TABULAR OHLC SELFTEST PASS: table/date parsing, no year-volume leakage, exact 1834.9 < 1835 fill')


if __name__=='__main__':
    main()

from __future__ import annotations
import tempfile
from pathlib import Path
from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor


def must(text: str, tokens: list[str], label: str) -> None:
    missing = [x for x in tokens if x not in text]
    if missing:
        raise AssertionError(f"{label}: missing {missing}\n{text}")


def main() -> None:
    paths = resolve_runtime_paths(root='.', require_inputs=True)
    with tempfile.TemporaryDirectory(prefix='bfxps_exec_') as td:
        a = SmartAdvisor(paths.trades, warning_catalog_path=paths.warning_catalog,
                         policy_path=paths.policy, memory_path=Path(td)/'memory.json')
        r = a.ask('giá đang 1833 tao muốn long quá, tao long sl 28 được không', session_id='risk')
        must(r.text, ['LONG ngược', 'SL 1.828,0', 'R/R xấp xỉ 0.40', 'không đáng vào mới'], 'long-1833-sl28')

        r = a.ask('dính cutloss 1804 rồi chán quá, lên 1835 gãy lại 1828 mà tao ko sọc kịp, tao sọc atc có đúng hd của kèo ko', session_id='atc')
        must(r.text, ['Đừng dùng lệnh kế tiếp để gỡ', 'SHORT ATC phù hợp hướng kèo', 'không all-in ATC'], 'short-atc-path')

        a.ask('O 1807.8 H 1833 L 1804 P 1828', session_id='fill')
        r = a.ask('hệ đang có những kèo gì nay khớp lệnh ko', session_id='fill')
        must(r.text, ['không engine nào', 'SIMCARRRY6', 'Kèo chính còn hiệu lực'], 'engine-fill')

        r = a.ask('hay tôi long 1810 nhé được không', session_id='entry')
        must(r.text, ['Mức 1.810,0', 'LONG ngược', 'dư địa', 'Quyền R5'], 'long-1810')

        variants = [
            'giờ 1832.5 long được không sl 1827',
            'tôi định mua 1830 nhưng hệ chờ short 1835, có đáng không',
            'giá lên 1835 rồi rơi lại 1829, short cuối phiên được chứ',
            'vừa cắt long xong giờ short gỡ có nên không',
            'high 1834.9 chưa chạm 1835 thì tính fill chưa',
            'hôm nay engine nào thực sự vào được lệnh',
            'short 1820 bây giờ có phải bán đuổi không',
            'long 1811 bắt hồi được không',
            'nếu low 1804 rồi reclaim open thì long scalp sao',
            'nếu vượt 1844 rồi đóng trên đó có còn short không',
            'tôi đang long 1833 giờ về 1828 xử lý thế nào',
            'đã short 1835 rồi giá 1828 thì giữ hay chốt',
            'ATC 1828 mà high đã 1835 thì kèo short có đúng thứ tự không',
            'giá 1810 nhưng target short 1811.1 đã qua, còn short được không',
            'tôi bỏ lỡ cú short 1835, giờ 1828 có nên đuổi theo không',
            'hệ có kèo ladder nào khớp chưa',
        ]
        for i, q in enumerate(variants):
            rr = a.ask(q, session_id=f'v{i}')
            if not rr.text.strip():
                raise AssertionError(f'empty answer: {q}')
            if 'Traceback' in rr.text or 'internal_error' in rr.text:
                raise AssertionError(f'crash-like answer: {q}\n{rr.text}')

    print('TRADER EXECUTION CONVERSATION SELFTEST PASS: shorthand SL, regret/cutloss, narrative path, ATC, proposed entry, fill audit, 16 flexible variants')


if __name__ == '__main__':
    main()

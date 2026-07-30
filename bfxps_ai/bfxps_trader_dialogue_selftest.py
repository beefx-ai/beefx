from __future__ import annotations
import tempfile
from pathlib import Path

from bfxps_customer_bridge import SessionSnapshot
from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor


def require(text: str, tokens: list[str], label: str) -> None:
    missing = [x for x in tokens if x not in text]
    if missing:
        raise AssertionError(f"{label}: missing {missing}\n{text}")


def main() -> None:
    paths = resolve_runtime_paths(root='.', require_inputs=True)
    with tempfile.TemporaryDirectory(prefix='bfxps_dialogue_') as td:
        advisor = SmartAdvisor(
            paths.trades,
            warning_catalog_path=paths.warning_catalog,
            policy_path=paths.policy,
            memory_path=Path(td) / 'memory.json',
        )

        # Investor dialogue 1: far below SHORT entry -> confirmed rebound -> LONG bridge -> exit at SHORT zone.
        r1 = advisor.ask('hôm nay đánh kiểu gì', session_id='investor-1')
        require(r1.text, ['Chưa thể nói vào ngay', 'SHORT 1.835,0 → 1.811,1', 'scalp LONG nhỏ'], 'dialogue1-turn1')
        r2 = advisor.ask('O 1807,8 H 1812 L 1804 P 1809, giờ làm gì', session_id='investor-1')
        require(r2.text, ['có thể LONG scalp ngược nhịp', 'khởi đầu 0,10 và tối đa 0,30 vị thế', '1.811,1 → 1.835,0', 'thủng Low 1.804,0 thì thoát'], 'dialogue1-turn2')
        r3 = advisor.ask('vậy long được chưa', session_id='investor-1')
        require(r3.text, ['có thể LONG scalp ngược nhịp', 'không coi là kèo chính'], 'dialogue1-turn3')
        r4 = advisor.ask('nếu lên 1812 thì sao', session_id='investor-1')
        require(r4.text, ['Nếu đã có LONG scalp thì chốt bớt', '1.811,1'], 'dialogue1-turn4')
        r5 = advisor.ask('nếu thủng 1804 thì sao', session_id='investor-1')
        require(r5.text, ['Chưa LONG', 'reclaim Open', 'Low mới dưới 1.804,0'], 'dialogue1-turn5')
        r6 = advisor.ask('nếu hồi lên 1835 thì làm gì', session_id='investor-1')
        require(r6.text, ['LONG ngược nhịp đã tới vùng thoát 1.835,0', 'chốt hết', 'từ chối vùng này'], 'dialogue1-turn6')

        # Investor dialogue 2: SHORT entry touched and rejected -> aligned trade becomes actionable.
        advisor.ask('kèo này ổn không', session_id='investor-2')
        r7 = advisor.ask('open 1807,8 high 1836 low 1804 hiện tại 1833,8 thì sao', session_id='investor-2')
        require(r7.text, ['OHLC đã thỏa điều kiện kích hoạt SHORT', 'High 1.836,0', 'mốc chốt 1.811,1'], 'dialogue2-turn2')
        r8 = advisor.ask('long tiếp được không', session_id='investor-2')
        require(r8.text, ['LONG ngược nhịp đã tới vùng thoát', 'không mở mới'], 'dialogue2-turn3')
        r9 = advisor.ask('short luôn chưa', session_id='investor-2')
        require(r9.text, ['OHLC đã thỏa điều kiện kích hoạt SHORT', 'chỉ triển khai SHORT theo ladder'], 'dialogue2-turn4')
        r10 = advisor.ask('nếu vượt 1844,3 thì sao', session_id='investor-2')
        require(r10.text, ['đã vượt entry SHORT', 'không bình quân bán lên'], 'dialogue2-turn5')

        # Investor dialogue 3: breakout above entry, then loss of entry confirms a fresh SHORT setup.
        advisor.ask('hôm nay cảnh báo gì và có cửa nào đánh được', session_id='investor-3')
        r11 = advisor.ask('mở cửa 1837 high 1846 low 1834 giá hiện tại 1845 thì sao', session_id='investor-3')
        require(r11.text, ['OHLC đang vi phạm đúng điều kiện kích hoạt', 'giá hiện tại 1.845,0 vẫn nằm trên', 'kèo SHORT bị vô hiệu'], 'dialogue3-turn2')
        advisor.ask('short thì sao', session_id='investor-3')
        r12 = advisor.ask('nếu mất lại 1835 thì sao', session_id='investor-3')
        require(r12.text, ['có thể canh SHORT theo kế hoạch đã hiệu chỉnh', 'giá hiện lùi xuống 1.835,0'], 'dialogue3-turn4')

        # Symmetry guard: when the database is LONG, the same brain must invert roles cleanly.
        long_plan = {
            'direction': 'LONG',
            'operational_entry': 1800.0,
            'operational_target': 1825.0,
            'forecast': 1832.0,
            'entry_target_swap_applied': True,
        }
        long_context = {'active_plans': [long_plan], 'r5_control': {'current_action': 'FLIP_HINT', 'max_position': 0.3}}
        counter = advisor._intraday_action_lines(
            long_context,
            long_plan,
            SessionSnapshot(live_price=1827.0, session_open=1828.0, session_high=1832.0, session_low=1824.0),
            requested_side='SHORT',
        )
        counter_text = '\n'.join(counter)
        require(counter_text, ['có thể SHORT scalp ngược nhịp', 'khởi đầu 0,10 và tối đa 0,30 vị thế', '1.825,0 → 1.800,0'], 'long-database-counter-short')

        long_context['r5_control'] = {'current_action': 'KEEP', 'max_position': 0.3}
        aligned = advisor._intraday_action_lines(
            long_context,
            long_plan,
            SessionSnapshot(live_price=1802.0, session_open=1801.0, session_high=1805.0, session_low=1798.0),
            requested_side='LONG',
        )
        aligned_text = '\n'.join(aligned)
        require(aligned_text, ['OHLC đã thỏa điều kiện kích hoạt LONG', 'Low 1.798,0', 'mốc chốt 1.825,0'], 'long-database-aligned-long')

    print('TRADER DIALOGUE SELFTEST PASS: multi-turn memory, tactical switching, LONG/SHORT symmetry, invalidation')


if __name__ == '__main__':
    main()

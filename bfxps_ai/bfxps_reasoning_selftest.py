from __future__ import annotations
from bfxps_runtime import resolve_runtime_paths
from bfxps_smart_advisor import SmartAdvisor

CASES = [
    ("Kèo hiện tại là gì?", ["Chưa thể nói vào ngay", "Cảnh báo cụ thể:", "Cách xử lý"]),
    ("Hôm nay sao?", ["Cảnh báo cụ thể:", "bỏ hai mức gốc bị đảo", "chỉ SHORT khi"]),
    ("Kèo này chất lượng ra sao?", ["gọi 1.835,0 là đáy kỳ vọng", "cặp giá gốc thua", "cặp đã sửa dương"]),
    ("Có nên vào không?", ["Chưa thể nói vào ngay", "Gửi bốn mức đó"]),
    ("Giá đang 1809 nên làm gì?", ["không SHORT đuổi", "WAIT_ENTRY", "Open–High–Low"]),
    ("Mở cửa 1807,8, hiện tại 1809 thì sao?", ["mở cửa 1.807,8", "chưa có High–Low đầy đủ", "không SHORT đuổi"]),
    ("O 1807,8 H 1812 L 1804 P 1809, giờ làm gì", ["Quyền R5: PRE_OPEN", "có thể LONG scalp ngược nhịp", "1.811,1 → 1.835,0", "thủng Low 1.804,0 thì thoát"]),
    ("Cho tao kèo ngược hệ, tao không thích SHORT hôm nay", ["PRE_OPEN/PENDING_OHLCV", "mọi kèo ngược chỉ là kịch bản chuẩn bị", "Gửi Open–High–Low–giá hiện tại"]),
    ("Cho tao LONG lên điểm chờ SHORT", ["PRE_OPEN/PENDING_OHLCV", "chưa phải lệnh được R5 xác nhận"]),
    ("long thì sao", ["PRE_OPEN/PENDING_OHLCV", "kịch bản chuẩn bị"]),
    ("short thì sao", ["cùng hướng kèo chính", "cung cấp giá live"]),
    ("cho tao cái chart forecast", ["Chart forecast dạng thang giá", "Khung tổng"]),
    ("dự báo biên hôm nay thế nào", ["Biên forecast", "Biên tác chiến chính"]),
    ("Kèo đó target bao nhiêu?", ["Mốc chốt của kế hoạch đã sửa là 1.811,1", "không dùng mức chốt gốc bị đảo 1.835,0"]),
]

def main():
    paths=resolve_runtime_paths(root='.', require_inputs=True)
    adv=SmartAdvisor(paths.trades, ohlc_path=paths.ohlc if paths.ohlc.exists() else None,
                     warning_catalog_path=paths.warning_catalog, policy_path=paths.policy)
    for i,(q,tokens) in enumerate(CASES,1):
        r=adv.ask(q, session_id=f'reasoning-{i}')
        miss=[t for t in tokens if t not in r.text]
        if miss:
            raise AssertionError(f'{q}: missing {miss}\n{r.text}')
        print(f'[{i}] {q}\n{r.text}\n')
    print('REASONING SELFTEST PASS: decision-first, warning-aware, actionable with OHLC')

if __name__=='__main__':
    main()

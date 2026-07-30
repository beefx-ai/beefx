from pathlib import Path
import sys, json
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'bfxps_ai'))
from bfxps_smart_advisor import SmartAdvisor
adv=SmartAdvisor(ROOT/'outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv', warning_catalog_path=ROOT/'bfxps_ai/config/backtested_warning_catalog.json', policy_path=ROOT/'bfxps_ai/config/advisor_policy.json')
cases=[
('Các nấc chung nhau giữa các engines là gì?', ['1.835,0','1.844,3']),
('Có nên bình quân giá từng nấc không?', ['Ladder gốc','0.10','1.00']),
('O 1808 H 1812 L 1804 P 1809 gap down dứt khoát thì làm gì?', ['không SHORT đuổi','WAIT_ENTRY']),
('O 1832 H 1836 L 1831 P 1834 vượt 1835 rồi gãy lại thì sao?', ['cú vượt đã thất bại','SHORT']),
('O 1832 H 1834.9 L 1830 P 1831 có được tính fill 1835 không?', ['chưa chạm entry','chưa short']),
('O 1845 H 1848 L 1842 P 1846 gap up qua toàn bộ nấc, tăng full size nhé?', ['không SHORT','không bình quân']),
('O 1834 H 1837.4 L 1833 P 1837.4 bình quân từng nấc thế nào?', ['không SHORT','không bình quân']),
('O 1834 H 1844.3 L 1833 P 1843 bình quân đến đâu?', ['không SHORT','không bình quân']),
('Giá xuống 1829 sau khi short 1835 có bình quân thêm không?', ['không thêm ở phía có lợi']),
('Vượt 1844.3 rồi giữ trên đó có nên tăng khối lượng?', ['không cộng vượt cap']),
]
rows=[]; ok=True
for i,(q,expects) in enumerate(cases):
    r=adv.ask(q,session_id=f'qa{i}')
    text=r.text
    passed=all(e.lower() in text.lower() for e in expects)
    ok &= passed
    rows.append({'q':q,'intent':r.intent,'pass':passed,'answer':text})
print(json.dumps(rows,ensure_ascii=False,indent=2))
print('LEVEL_GAP_AVERAGING_SELFTEST', 'PASS' if ok else 'FAIL')
raise SystemExit(0 if ok else 1)

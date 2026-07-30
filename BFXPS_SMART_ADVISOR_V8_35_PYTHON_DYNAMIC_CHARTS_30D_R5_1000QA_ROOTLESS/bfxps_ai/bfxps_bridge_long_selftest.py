from pathlib import Path
from bfxps_smart_advisor import SmartAdvisor
ROOT=Path(__file__).resolve().parents[1]
mem=ROOT/'bfxps_ai/runtime/_bridge_test_v815.json'
try: mem.unlink()
except FileNotFoundError: pass
a=SmartAdvisor(ROOT/'outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv', memory_path=mem)
r=a.ask('O 1808 H 1812 L 1804 P 1809. Sao ko cho long đến điểm chờ short?', session_id='s1')
assert 'Chưa LONG' in r.text and '1.812,1' in r.text and '1.811,1' in r.text and '1.835,0' in r.text, r.text
r=a.ask('O 1808 H 1816 L 1804 P 1815. Mình muốn kèo long của hệ', session_id='s2')
assert 'Đã reclaim' in r.text and 'LONG scalp' in r.text and '0.30' in r.text and 'đóng ATC' in r.text, r.text
r=a.ask('O 1815 H 1818 L 1810 P 1816. Long tới điểm short được không?', session_id='s3')
assert 'Không kích hoạt' in r.text and 'Open 1.815,0 cao hơn Lower 1.811,1' in r.text, r.text
print('BRIDGE_LONG_SELFTEST PASS')

from pathlib import Path
from bfxps_smart_advisor import SmartAdvisor

root = Path(__file__).resolve().parents[1]
advisor = SmartAdvisor(root / 'outputs' / 'BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv')
questions = [
    'biên dự kiến hôm nay bao nhiêu',
    'biên high low dự kiến',
    'giá đang 1833 thì biên còn lại lên xuống bao nhiêu',
]
for i, q in enumerate(questions):
    r = advisor.ask(q, session_id=f'range-truth-{i}')
    assert 'Biên dự kiến của đúng SIMCARRRY6 t+1: 1.811,1 – 1.835,0' in r.text, (q, r.text)
    assert 'không ghép với engine khác hay T+ khác' in r.text, (q, r.text)
    assert '1.844,3 – 1.835,0' not in r.text, (q, r.text)
print('FORECAST_RANGE_OUTPUT_TRUTH_SELFTEST_PASS')

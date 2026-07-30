from bfxps_smart_advisor import SmartAdvisor
from bfxps_customer_bridge import SessionSnapshot

adv=SmartAdvisor("outputs/BEST_ENGINE_CHART_LAST3_PLUS_FORWARD_TRADES.tsv")
adv._bridge_short_basis_levels=lambda plan:(2010.0,2020.0)
plan={"direction":"LONG","horizon":"t+1","operational_entry":2000.0,"date":"29/07/2026"}

def check(price, low, expected):
    snap=SessionSnapshot(session_open=2025.0,session_high=2028.0,session_low=low,live_price=price)
    text="\n".join(adv._answer_bridge_short_to_long({},plan,snap))
    assert expected in text, text

check(2022.0,2021.0,"Chưa SHORT")
check(2018.0,2018.0,"Đã reclaim xuống")
check(2021.0,2018.0,"Nhịp reclaim đã thất bại")
print("RECLAIM_BOTH_SIDES_SELFTEST PASS")

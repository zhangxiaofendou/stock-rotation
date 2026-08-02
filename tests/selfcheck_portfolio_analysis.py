"""持仓收益与左侧交易分析逻辑回归测试。"""

import sys
import pandas as pd

sys.path.insert(0, ".")
from dashboard.pages.portfolio import _build_position_analysis  # noqa: E402

positions = pd.DataFrame([
    {"security_code": "600001", "security_name": "A", "sector_code": "S1", "sector_name": "板块一", "quantity": 100, "avg_cost": 10, "cost_amount": 1000, "stop_loss": 8},
    {"security_code": "600002", "security_name": "B", "sector_code": "S2", "sector_name": "板块二", "quantity": 100, "avg_cost": 10, "cost_amount": 1000, "stop_loss": None},
    {"security_code": "600003", "security_name": "C", "sector_code": "S3", "sector_name": "板块三", "quantity": 100, "avg_cost": 10, "cost_amount": 1000, "stop_loss": None},
])
quotes = pd.DataFrame([
    {"security_code": "600001", "market_price": 7.5, "quote_sector_name": "板块一"},
    {"security_code": "600002", "market_price": 11.0, "quote_sector_name": "板块二"},
])
states = pd.DataFrame([
    {"sector_code": "S1", "sector_name": "板块一", "state": "⑦持续杀跌", "trend": "下跌", "date": "2026-08-01"},
    {"sector_code": "S2", "sector_name": "板块二", "state": "⑨底背离", "trend": "横盘", "date": "2026-08-01"},
])

out = _build_position_analysis(positions, quotes, states)
assert list(out["security_code"]) == ["600001", "600002", "600003"]
assert list(out["priority"]) == ["尽快决策", "左侧观察", "数据不足"]
assert round(float(out.iloc[0]["profit_pct"]), 2) == -25.0
assert round(float(out.iloc[1]["profit_pct"]), 2) == 10.0
assert pd.isna(out.iloc[2]["market_price"])
assert out.iloc[0]["action"] in {"核验止损条件", "复核逻辑，暂不盲目补仓"}
assert "不对持仓方向做推断" in out.iloc[2]["reason"]
print("=== 自检汇总  结果：6 通过 / 0 失败 ===")

# -*- coding: utf-8 -*-
"""自检：record_trade 行业自动补全。

覆盖：
1) 导入式调用（不传行业）→ 自动反查行业名 + 881xxx 代码写入持仓；
2) 手动传行业 → 不被反查结果覆盖；
3) 反查抛异常 → 交易正常写入、行业留空（不阻塞）。
"""
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio import stock_lookup
from portfolio.holdings import PortfolioHoldings
from data.storage.sqlite_store import SQLiteStore

fails = []

# --- 准备临时库 ---
tmp = tempfile.mkdtemp()
store = SQLiteStore(db_path=os.path.join(tmp, "t.db"))


def make_service():
    return PortfolioHoldings(store=store, user_id="u1")


# --- Case 1: 不传行业 → 自动补全 ---
orig_lookup, orig_resolve = stock_lookup.lookup_stock_info, stock_lookup.resolve_sector
try:
    stock_lookup.lookup_stock_info = lambda code: {"name": "某股", "price": 10.0, "sector_name": "半导体"}
    stock_lookup.resolve_sector = lambda name: ("881272", name, "科技")

    svc = make_service()
    svc.record_trade(security_code="300474", security_name="景嘉微", side="BUY",
                     quantity=100, price=50.0, trade_date="2026-01-05")
    pos = store.get_portfolio_positions(user_id="u1")
    row = pos[pos["security_code"] == "300474"].iloc[0]
    if str(row["sector_name"]) != "半导体":
        fails.append(f"Case1 行业名应自动补全为半导体, 实得 {row['sector_name']!r}")
    if str(row["sector_code"]) != "881272":
        fails.append(f"Case1 行业代码应补全 881272, 实得 {row['sector_code']!r}")
finally:
    stock_lookup.lookup_stock_info, stock_lookup.resolve_sector = orig_lookup, orig_resolve

# --- Case 2: 手动传行业 → 不覆盖 ---
svc = make_service()
svc.record_trade(security_code="600519", security_name="贵州茅台", side="BUY",
                 quantity=100, price=1500.0, trade_date="2026-01-06",
                 sector_name="白酒", sector_code="881117")
row = store.get_portfolio_positions(user_id="u1")
row = row[row["security_code"] == "600519"].iloc[0]
if str(row["sector_name"]) != "白酒" or str(row["sector_code"]) != "881117":
    fails.append(f"Case2 手动行业应保留 白酒/881117, 实得 {row['sector_name']!r}/{row['sector_code']!r}")

# --- Case 3: 反查抛异常 → 写入成功、行业留空 ---
def _boom(code):
    raise RuntimeError("network down")

try:
    stock_lookup.lookup_stock_info = _boom
    svc = make_service()
    svc.record_trade(security_code="000001", security_name="平安银行", side="BUY",
                     quantity=100, price=12.0, trade_date="2026-01-07")
    row = store.get_portfolio_positions(user_id="u1")
    row = row[row["security_code"] == "000001"].iloc[0]
    if float(row["quantity"]) != 100:
        fails.append(f"Case3 交易应正常写入, 实得数量 {row['quantity']}")
except Exception as exc:
    fails.append(f"Case3 反查异常不应阻塞写入: {exc}")
finally:
    stock_lookup.lookup_stock_info, stock_lookup.resolve_sector = orig_lookup, orig_resolve

if fails:
    print("FAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("PASS: 行业自动补全 3 例全部通过")

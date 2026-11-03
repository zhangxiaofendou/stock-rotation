# -*- coding: utf-8 -*-
"""自检：record_trade 行业离线优先自动补全（取代旧的在线反查版）。

覆盖：
1) 不传行业 → 离线包反查行业名 + 881xxx 代码写入持仓（绿景嘉微 -> 国防军工/881166）；
2) 手动传行业 → 不被离线反查结果覆盖；
3) 离线包未覆盖的小众代码 → 交易正常写入、行业留空（绝不阻塞录入）。
"""
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio.holdings import PortfolioHoldings
from data.storage.sqlite_store import SQLiteStore

fails = []

# --- 准备临时库 ---
tmp = tempfile.mkdtemp()
store = SQLiteStore(db_path=os.path.join(tmp, "t.db"))


def make_service():
    return PortfolioHoldings(store=store, user_id="u1")


# --- Case 1: 不传行业 → 离线包补全 ---
svc = make_service()
svc.record_trade(security_code="300474", security_name="景嘉微", side="BUY",
                 quantity=100, price=50.0, trade_date="2026-01-05")
pos = store.get_portfolio_positions(user_id="u1")
row = pos[pos["security_code"] == "300474"].iloc[0]
if str(row["sector_name"]) != "国防军工":
    fails.append(f"Case1 行业名应离线补全为国防军工, 实得 {row['sector_name']!r}")
if str(row["sector_code"]) != "881166":
    fails.append(f"Case1 行业代码应补全 881166, 实得 {row['sector_code']!r}")

# --- Case 2: 手动传行业 → 不覆盖 ---
svc = make_service()
svc.record_trade(security_code="600519", security_name="贵州茅台", side="BUY",
                 quantity=100, price=1500.0, trade_date="2026-01-06",
                 sector_name="白酒", sector_code="881117")
row = store.get_portfolio_positions(user_id="u1")
row = row[row["security_code"] == "600519"].iloc[0]
if str(row["sector_name"]) != "白酒" or str(row["sector_code"]) != "881117":
    fails.append(f"Case2 手动行业应保留 白酒/881117, 实得 {row['sector_name']!r}/{row['sector_code']!r}")

# --- Case 3: 离线包未覆盖代码 → 写入成功、行业留空、不阻塞 ---
svc = make_service()
svc.record_trade(security_code="999999", security_name="测试小众股", side="BUY",
                 quantity=100, price=12.0, trade_date="2026-01-07")
pos = store.get_portfolio_positions(user_id="u1")
row = pos[pos["security_code"] == "999999"].iloc[0]
if float(row["quantity"]) != 100:
    fails.append(f"Case3 交易应正常写入, 实得数量 {row['quantity']}")
if str(row.get("sector_code") or "") not in ("", "nan"):
    fails.append(f"Case3 离线包未覆盖时应留空, 实得 {row['sector_code']!r}")

if fails:
    print("FAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("PASS: record_trade 离线优先补全 3 例全部通过")

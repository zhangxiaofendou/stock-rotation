# -*- coding: utf-8 -*-
"""自检：App 启动时的持仓行业自检补全 autofill_missing_sectors。

覆盖：
1) 有缺行业的持仓 → 反查并写回账本，返回补全数；
2) 行业齐全 → 返回 0，不做任何写入；
3) 只有部分缺 → 只补缺的，齐的不动。
"""
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dashboard.views.portfolio as P
from portfolio import stock_lookup
from portfolio.holdings import PortfolioHoldings
from data.storage.sqlite_store import SQLiteStore

fails = []

tmp = tempfile.mkdtemp()
store = SQLiteStore(db_path=os.path.join(tmp, "t.db"))

# patch service 构造：让 autofill 用临时库而非真实默认库
orig_service = P.PortfolioHoldings
P.PortfolioHoldings = lambda user_id="": PortfolioHoldings(store=store, user_id=user_id)

# --- mock 行情反查：300474→半导体，600519→白酒 ---
# 注意 portfolio.py 是 from-import 绑定，须 patch 其模块命名空间的名字
orig_lookup = P.lookup_stock_info
try:
    P.lookup_stock_info = (
        lambda code: {"name": "x", "price": 1.0,
                      "sector_name": {"300474": "半导体", "600519": "白酒"}.get(str(code), "")}
    )
    # 绕开 service 层的重复反查开销无所谓——直接造数据
    store.record_portfolio_transaction(
        trade_date="2026-01-05", security_code="300474", security_name="景嘉微",
        side="BUY", quantity=100, price=50.0, user_id="u1")
    store.record_portfolio_transaction(
        trade_date="2026-01-05", security_code="600519", security_name="贵州茅台",
        side="BUY", quantity=100, price=1500.0, user_id="u1",
        sector_name="白酒", sector_code="881117")

    # --- Case 1: 300474 缺行业 → 补全；600519 已有 → 不动 ---
    filled = P.autofill_missing_sectors("u1")
    if filled != 1:
        fails.append(f"Case1 应补全 1 个, 实得 {filled}")
    pos = store.get_portfolio_positions(user_id="u1")
    r1 = pos[pos["security_code"] == "300474"].iloc[0]
    r2 = pos[pos["security_code"] == "600519"].iloc[0]
    if str(r1["sector_name"]) != "半导体":
        fails.append(f"Case1 300474 行业应写回半导体, 实得 {r1['sector_name']!r}")
    if str(r2["sector_name"]) != "白酒":
        fails.append(f"Case1 600519 行业应保持白酒, 实得 {r2['sector_name']!r}")

    # --- Case 2: 全部齐全 → 返回 0 ---
    filled2 = P.autofill_missing_sectors("u1")
    if filled2 != 0:
        fails.append(f"Case2 行业齐全应返回 0, 实得 {filled2}")

    # --- Case 3: 不存在的用户 → 返回 0 不抛 ---
    filled3 = P.autofill_missing_sectors("nobody")
    if filled3 != 0:
        fails.append(f"Case3 空用户应返回 0, 实得 {filled3}")
finally:
    P.lookup_stock_info = orig_lookup
    P.PortfolioHoldings = orig_service

if fails:
    print("FAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("PASS: 启动行业自检 3 例全部通过")

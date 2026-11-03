# -*- coding: utf-8 -*-
"""自检：启动行业自检 autofill_missing_sectors（离线优先）与可见留痕。

覆盖：
1) 缺行业的持仓 → 离线解析并写回账本（name+code 都补），返回补全数；
2) 行业齐全 → 返回 0，不做任何写入；
3) 只有部分缺 → 只补缺的，齐的不动；
4) 已存在行业名但缺行业代码 → 离线补出 881xxx（这正是 advisor 误报「待补全」的根源）；
5) 每次检查/补全都落库 sector_fill_log（页面「检查留痕」可见）。
"""
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dashboard.views.portfolio as P
from portfolio import industry_resolver
from portfolio.holdings import PortfolioHoldings
from data.storage.sqlite_store import SQLiteStore

fails = []

tmp = tempfile.mkdtemp()
store = SQLiteStore(db_path=os.path.join(tmp, "t.db"))

# patch service 构造：让 autofill 用临时库而非真实默认库
orig_service = P.PortfolioHoldings
P.PortfolioHoldings = lambda user_id="": PortfolioHoldings(store=store, user_id=user_id)


def offline_expected(code: str):
    """用离线解析器算出期望的 (name, code)，与 autofill 实际结果对齐校验。"""
    return industry_resolver.resolve_stock_industry(str(code).strip(), online_fallback=False)


try:
    # --- Case 1: 300474 缺行业 → 离线补 name+code；600519 已有齐全 → 不动 ---
    store.record_portfolio_transaction(
        trade_date="2026-01-05", security_code="300474", security_name="景嘉微",
        side="BUY", quantity=100, price=50.0, user_id="u1")
    store.record_portfolio_transaction(
        trade_date="2026-01-05", security_code="600519", security_name="贵州茅台",
        side="BUY", quantity=100, price=1500.0, user_id="u1",
        sector_name="食品饮料", sector_code="881133")

    en, ec = offline_expected("300474")
    filled = P.autofill_missing_sectors("u1")
    if filled != 1:
        fails.append(f"Case1 应补全 1 个, 实得 {filled}")
    pos = store.get_portfolio_positions(user_id="u1")
    r1 = pos[pos["security_code"] == "300474"].iloc[0]
    r2 = pos[pos["security_code"] == "600519"].iloc[0]
    if str(r1["sector_name"]) != en:
        fails.append(f"Case1 300474 行业名应离线补为 {en!r}, 实得 {r1['sector_name']!r}")
    if str(r1["sector_code"]) != ec:
        fails.append(f"Case1 300474 行业代码应离线补为 {ec!r}, 实得 {r1['sector_code']!r}")
    if str(r2["sector_name"]) != "食品饮料":
        fails.append(f"Case1 600519 行业名应保持 食品饮料, 实得 {r2['sector_name']!r}")
    if str(r2["sector_code"]) != "881133":
        fails.append(f"Case1 600519 行业代码应保持 881133, 实得 {r2['sector_code']!r}")

    # --- Case 2: 全部齐全 → 返回 0 ---
    filled2 = P.autofill_missing_sectors("u1")
    if filled2 != 0:
        fails.append(f"Case2 行业齐全应返回 0, 实得 {filled2}")

    # --- Case 3: 不存在的用户 → 返回 0 不抛 ---
    filled3 = P.autofill_missing_sectors("nobody")
    if filled3 != 0:
        fails.append(f"Case3 空用户应返回 0, 实得 {filled3}")

    # --- Case 4 (核心修复): 已有行业名但缺行业代码 → 离线补出 881xxx ---
    # 这正是 advisor 一直误报「待补全」的根源：旧 autofill 只写 sector_name，漏了 sector_code。
    store.record_portfolio_transaction(
        trade_date="2026-02-01", security_code="300229", security_name="拓尔思",
        side="BUY", quantity=100, price=20.0, user_id="u4",
        sector_name="IT服务Ⅱ", sector_code="")  # 有名称无代码
    filled4 = P.autofill_missing_sectors("u4")
    if filled4 != 1:
        fails.append(f"Case4 应补全 1 个(补齐行业代码), 实得 {filled4}")
    pos4 = store.get_portfolio_positions(user_id="u4")
    r4 = pos4[pos4["security_code"] == "300229"].iloc[0]
    if str(r4["sector_name"]) != "IT服务Ⅱ":
        fails.append(f"Case4 行业名应保持 IT服务Ⅱ, 实得 {r4['sector_name']!r}")
    if str(r4["sector_code"]) != "881271":
        fails.append(f"Case4 行业代码应离线补为 881271, 实得 {r4['sector_code']!r}")

    # --- Case 5: 检查/补全留痕已落库（u1 至少 2 条：300474 补全 + 600519 已齐全）---
    logs = store.get_sector_fill_logs(user_id="u1")
    if logs.empty:
        fails.append("Case5 u1 应产生检查留痕，但 sector_fill_log 为空")
    else:
        codes_logged = set(logs["security_code"].tolist())
        if "300474" not in codes_logged:
            fails.append("Case5 300474 的检查留痕缺失")
        if "600519" not in codes_logged:
            fails.append("Case5 600519 的检查留痕缺失")
        # 600519 应标记为「已齐全」，300474 应标记为「补行业名+代码」之类
        acts = set(logs[logs["security_code"] == "300474"]["action"].tolist())
        if not any("补" in a for a in acts):
            fails.append(f"Case5 300474 动作应含「补」，实得 {acts}")
finally:
    P.PortfolioHoldings = orig_service

if fails:
    print("FAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("PASS: 启动行业自检 5 例全部通过（离线优先 + 缺代码补 881xxx + 留痕落库）")

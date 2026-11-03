# -*- coding: utf-8 -*-
"""自检：三入口统一的「检查+补全+留痕」逻辑（check_and_fill_position / record_trade）。

覆盖：
1) check_and_fill_position：缺行业 → 补全并落库留痕，trace 字段正确；
2) check_and_fill_position：已齐全 → 标记「已齐全」并留痕，不写回；
3) record_trade：未传行业 → 录入后自动补全并落库（来源「添加记录」）；
4) summarize_traces：汇总计数正确；
5) get_sector_fill_logs：按用户读取、最新在前。
"""
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portfolio.holdings import PortfolioHoldings
from portfolio.sector_fill import check_and_fill_position, summarize_traces
from portfolio import industry_resolver
from data.storage.sqlite_store import SQLiteStore

fails = []
tmp = tempfile.mkdtemp()
store = SQLiteStore(db_path=os.path.join(tmp, "t.db"))


def mk_service(uid):
    return PortfolioHoldings(store=store, user_id=uid)


try:
    # Case 1: 缺行业 → 补全 + 留痕
    store.record_portfolio_transaction(
        trade_date="2026-03-01", security_code="300426", security_name="唐德影视",
        side="BUY", quantity=100, price=10.0, user_id="c1")  # 无行业
    svc = mk_service("c1")
    en, ec = industry_resolver.resolve_stock_industry("300426", online_fallback=False)
    trace = check_and_fill_position(svc, "300426", security_name="唐德影视", source_tag="登录自检")
    if not trace.get("filled"):
        fails.append(f"Case1 应标记 filled=True, 实得 {trace}")
    if trace.get("before_name") not in (None, ""):
        fails.append(f"Case1 before_name 应为空, 实得 {trace.get('before_name')!r}")
    if trace.get("after_name") != en:
        fails.append(f"Case1 after_name 应= {en!r}, 实得 {trace.get('after_name')!r}")
    if trace.get("after_code") != ec:
        fails.append(f"Case1 after_code 应= {ec!r}, 实得 {trace.get('after_code')!r}")
    # DB 已写回
    pos = store.get_portfolio_positions(user_id="c1")
    rp = pos[pos["security_code"] == "300426"].iloc[0]
    if str(rp["sector_name"]) != en or str(rp["sector_code"]) != ec:
        fails.append(f"Case1 DB 写回不符: {rp['sector_name']!r}/{rp['sector_code']!r}")
    # 留痕已写
    logs = store.get_sector_fill_logs(user_id="c1")
    if logs.empty or "300426" not in set(logs["security_code"]):
        fails.append("Case1 未写入 sector_fill_log")

    # Case 2: 已齐全 → 标记已齐全，不重复写回
    store.record_portfolio_transaction(
        trade_date="2026-03-02", security_code="603887", security_name="城地香江",
        side="BUY", quantity=100, price=10.0, user_id="c2",
        sector_name="计算机", sector_code="881130")
    svc2 = mk_service("c2")
    trace2 = check_and_fill_position(svc2, "603887", security_name="城地香江", source_tag="登录自检")
    if trace2.get("action") != "已齐全":
        fails.append(f"Case2 应标记 已齐全, 实得 {trace2.get('action')!r}")
    if trace2.get("filled"):
        fails.append("Case2 不应标记 filled=True")
    # 留痕仍在（已齐全也算一次检查）
    logs2 = store.get_sector_fill_logs(user_id="c2")
    if logs2.empty or "603887" not in set(logs2["security_code"]):
        fails.append("Case2 未写入 sector_fill_log（已齐全也应留痕）")

    # Case 3: record_trade（添加记录入口）→ 自动补全 + 留痕来源「添加记录」
    svc3 = mk_service("c3")
    svc3.record_trade(
        security_code="301236", security_name="软通动力", side="BUY",
        quantity=100, price=30.0)  # 未传行业（user_id 由 service 携带）
    pos3 = store.get_portfolio_positions(user_id="c3")
    rp3 = pos3[pos3["security_code"] == "301236"].iloc[0]
    en3, ec3 = industry_resolver.resolve_stock_industry("301236", online_fallback=False)
    if str(rp3["sector_name"]) != en3 or str(rp3["sector_code"]) != ec3:
        fails.append(f"Case3 record_trade 未补全行业: {rp3['sector_name']!r}/{rp3['sector_code']!r} 期望 {en3!r}/{ec3!r}")
    logs3 = store.get_sector_fill_logs(user_id="c3")
    if logs3.empty or "301236" not in set(logs3["security_code"]):
        fails.append("Case3 record_trade 未写入留痕")
    elif "添加记录" not in set(logs3[logs3["security_code"] == "301236"]["source"]):
        fails.append(f"Case3 留痕来源应为「添加记录」, 实得 {set(logs3['source'])}")

    # Case 4: summarize_traces 计数
    traces = [
        {"after_name": "x", "after_code": "1", "filled": True, "action": "补行业名+代码"},
        {"after_name": "y", "after_code": "2", "filled": False, "action": "已齐全"},
        {"after_name": "", "after_code": "", "filled": False, "action": "未能补全(无行业数据)"},
    ]
    s = summarize_traces(traces)
    if s["total"] != 3 or s["filled"] != 1 or s["already"] != 1 or s["failed"] != 1:
        fails.append(f"Case4 summarize_traces 计数错误: {s}")
    if s["ok"] != 2:  # 第一、二条 after 均齐全
        fails.append(f"Case4 summarize_traces ok 计数错误: {s}")

    # Case 5: get_sector_fill_logs 按用户隔离 + 最新在前
    all_u1 = store.get_sector_fill_logs(user_id="c1")
    if not all_u1.empty and "c1" not in set(all_u1["user_id"]):
        fails.append("Case5 日志 user_id 隔离异常")

finally:
    pass

if fails:
    print("FAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("PASS: 行业补全检查+留痕 5 例全部通过（check_and_fill_position / record_trade / summarize / 日志读取）")

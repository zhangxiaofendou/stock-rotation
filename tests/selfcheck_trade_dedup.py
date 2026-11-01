"""导入去重自检：去重键规则 / 预览标记重复 / 确认写入跳过重复(含本批内重复) / 库内无重复堆积。

说明：去重是新增逻辑，无「旧实现」可复现 FAIL；这里用正向 + 边界用例确保
trade_dedup_key 规则正确，并端到端验证「历史已登记重复」与「本批内重复」都被跳过，
且不会在库里堆积重复行。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from portfolio.trade_import import trade_dedup_key, parse_trades, trades_to_records  # noqa: E402
from portfolio.holdings import PortfolioHoldings  # noqa: E402
from data.storage.sqlite_store import SQLiteStore  # noqa: E402
import dashboard.views.portfolio as pv  # noqa: E402  (_annotate_import_duplicates, _apply_import)
import pandas as pd  # noqa: E402


def _main() -> int:
    fails = []

    # ---- 1. 去重键规则 ----
    base = ("2026-01-05", "600519", "BUY", 100, 1680.00)
    if trade_dedup_key(*base) != trade_dedup_key("2026-01-05", "600519", "buy", 100, 1680.0):
        fails.append("去重键应忽略方向大小写")
    if trade_dedup_key(*base) != trade_dedup_key("2026-01-05", "SH600519", "BUY", 100.0, 1680.00):
        fails.append("去重键应忽略代码交易所前缀")
    if trade_dedup_key(*base) != trade_dedup_key("2026-01-05", "600519", "BUY", 100, 1680.001):
        fails.append("去重键应容忍价格浮点尾差(2位)")
    if trade_dedup_key(*base) == trade_dedup_key("2026-01-06", "600519", "BUY", 100, 1680.0):
        fails.append("不同日期应产生不同键")
    if trade_dedup_key(*base) == trade_dedup_key("2026-01-05", "600519", "SELL", 100, 1680.0):
        fails.append("不同方向应产生不同键")

    # ---- 准备临时库 + service ----
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        store = SQLiteStore(db_path=tmp.name)
        service = PortfolioHoldings(store=store, user_id="test")

        # 预写一笔已登记：600519 2026-01-05 BUY 100 @1680
        service.record_trade("600519", "贵州茅台", "BUY", 100, 1680.0, trade_date="2026-01-05", fee=0)

        # ---- 2. 预览标记重复 ----
        csv = (
            "日期,代码,名称,方向,数量,价格,费用\n"
            "2026-01-05,600519,贵州茅台,买入,100,1680.00,0\n"      # 与已登记重复
            "2026-01-10,000001,平安银行,买入,200,12.50,5\n"        # 新
            "2026-02-01,300750,宁德时代,买入,50,180.00,3\n"        # 新
        )
        trades, _ = parse_trades(raw_text=csv)
        records = trades_to_records(trades)
        dup = pv._annotate_import_duplicates(records, service)
        if dup != 1:
            fails.append(f"应标记1笔重复，实得{dup}")
        dup_codes = [r["security_code"] for r in records if r["duplicate"]]
        if dup_codes != ["600519"]:
            fails.append(f"重复的应是600519那笔，实得{dup_codes}")
        if any(r["duplicate"] for r in records if r["security_code"] != "600519"):
            fails.append("非重复记录不应被标为 duplicate")

        # ---- 3. 确认写入跳过重复（含本批内重复）----
        import_df = pd.DataFrame([
            {"trade_date": "2026-01-05", "security_code": "600519", "security_name": "贵州茅台",
             "side": "BUY", "quantity": 100, "price": 1680.0, "fee": 0},   # 历史重复
            {"trade_date": "2026-01-10", "security_code": "000001", "security_name": "平安银行",
             "side": "BUY", "quantity": 200, "price": 12.5, "fee": 5},     # 新
            {"trade_date": "2026-01-10", "security_code": "000001", "security_name": "平安银行",
             "side": "BUY", "quantity": 200, "price": 12.5, "fee": 5},     # 本批内重复
            {"trade_date": "2026-03-01", "security_code": "600519", "security_name": "贵州茅台",
             "side": "SELL", "quantity": 50, "price": 1700.0, "fee": 0},   # 新(不同方向)
        ])
        ok_count, skipped, apply_fails = pv._apply_import(service, import_df)
        if apply_fails:
            fails.append(f"写入不应失败: {apply_fails}")
        # 4 行中应：600519 BUY 历史重复→跳过；000001 BUY 新→写入；000001 BUY 本批重复→跳过；
        # 600519 SELL 新(不同方向)→写入。故成功 2 笔、跳过 2 笔。
        if ok_count != 2:
            fails.append(f"应成功写入2笔(000001买入 + 600519卖出)，实得{ok_count}")
        if skipped != 2:
            fails.append(f"应跳过2笔(历史重复1 + 本批内重复1)，实得{skipped}")
        existing = store.get_transactions_for_dedup("test")
        if len(existing) != 3:
            fails.append(f"库里应有3笔交易(1初始 + 2新)，实得{len(existing)}")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    if fails:
        print("❌ 失败:")
        for f in fails:
            print("  -", f)
        return 1
    print("✅ 全部通过：去重键规则 / 预览标记重复 / 确认写入跳过重复(含本批内重复) / 库内无重复堆积")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

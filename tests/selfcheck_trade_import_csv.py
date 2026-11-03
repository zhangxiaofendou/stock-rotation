"""用「真实交割单 CSV」跑通 App 的批量导入链路（解析 → 确认导入 → 行业离线补全 + 留痕）。

完全复用 dashboard 导入面板的同一套底层函数：
  parse_trades()        ← UI「解析」按钮
  _apply_import()       ← UI「确认导入」按钮
  record_trade()        ← 每笔调用，内部触发 check_and_fill_position（离线补全 + 留痕）

为安全起见，全程写入临时 SQLite（不碰真实云端账本）。最后校验：
  1) 22 行交割单全部解析成功、列映射正确；
  2) 写入后每只有持仓的股票都拿到了 行业名 + 同花顺 881xxx 代码（离线优先）；
  3) sector_fill_log 里留下了 source_tag='导入' 的检查留痕。
"""
import os
import sys
import tempfile

sys.path.insert(0, ".")

from portfolio.trade_import import parse_trades, trades_to_records, trade_dedup_key
from dashboard.views.portfolio import _apply_import
from portfolio.holdings import PortfolioHoldings
from data.storage.sqlite_store import SQLiteStore

CSV_PATH = os.environ.get(
    "CSV_PATH",
    r"C:\Users\张文颖\WorkBuddy\2026-08-01-11-37-13\逐笔导入_最终版_Excel可开.csv",
)
USER = "import_test_user"


def main():
    assert os.path.exists(CSV_PATH), f"CSV 不存在: {CSV_PATH}"
    with open(CSV_PATH, "rb") as f:
        file_bytes = f.read()

    # ---- 1) 解析（= UI「解析」） ----
    trades, errors = parse_trades(file_bytes=file_bytes, filename=os.path.basename(CSV_PATH))
    print(f"[1] 解析结果：{len(trades)} 笔，errors={errors}")
    assert len(trades) == 22, f"期望 22 笔，实得 {len(trades)}"
    assert not errors, f"解析报错: {errors}"

    # 列映射 & 方向校验
    bad_cols = [t for t in trades if not t.security_code or not t.side or t.price is None or t.quantity is None]
    assert not bad_cols, f"有解析缺陷的行: {bad_cols}"
    sides = sorted({t.side for t in trades})
    print(f"[1] 方向分布：{sides}，代码样本：{[t.security_code for t in trades[:3]]}")

    records = trades_to_records(trades)

    # ---- 2) 确认导入（= UI「确认导入」） ----
    tmp = tempfile.mkdtemp()
    store = SQLiteStore(db_path=os.path.join(tmp, "import_test.db"))
    service = PortfolioHoldings(store=store, user_id=USER)

    ok_count, skipped, fails = _apply_import(service, __import__("pandas").DataFrame(records))
    print(f"[2] 导入结果：成功 {ok_count} 笔，跳过 {skipped} 笔，失败 {len(fails)} 笔")
    assert ok_count == 22, f"期望成功 22 笔，实得 {ok_count}"
    assert not fails, f"导入失败: {fails}"

    # ---- 3) 行业离线补全校验 ----
    pos = service.positions()
    pos = pos[pos["quantity"] > 0]  # 只看有持仓的
    print(f"[3] 有持仓的股票 {len(pos)} 只，逐只核对 行业名/代码：")
    missing = []
    for _, r in pos.iterrows():
        nm = r.get("sector_name")
        cd = r.get("sector_code")
        flag = "✅" if (nm and str(nm).strip() and cd and str(cd).strip()) else "❌"
        if flag == "❌":
            missing.append(r["security_code"])
        print(f"    {flag} {r['security_code']} {r.get('security_name')}: {nm}/{cd}")
    assert not missing, f"以下代码补全失败: {missing}"

    # ---- 4) 留痕校验 ----
    logs = store.get_sector_fill_logs(user_id=USER)
    print(f"[4] sector_fill_log 共 {len(logs)} 条；按来源统计：")
    by_src = logs["source"].value_counts().to_dict() if not logs.empty else {}
    print("    ", by_src)
    assert not logs.empty, "没有留下任何行业补全检查留痕"
    assert (logs["source"] == "导入").any(), "留痕里没有 source='导入' 的记录"

    # 全部动作统计
    by_action = logs["action"].value_counts().to_dict() if not logs.empty else {}
    print("     动作统计：", by_action)

    print("\nPASS ✅  交割单导入全链路（解析→写入→离线补全行业→留痕）均正常。")


if __name__ == "__main__":
    main()

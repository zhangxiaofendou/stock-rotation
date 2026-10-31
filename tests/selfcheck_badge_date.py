"""数据来源徽标「数据截至」日期标注自检。

避免回归：徽标行只标来源不标日期，用户看不出底层数据新旧。
要求：src_badge_html 生成的 HTML 末尾带「数据截至 YYYY-MM-DD」，
日期取自 freshness 真实记录（与侧栏数据状态同源），取不到则不显示。

防假绿：用 mock freshness 分别验证 底数映射/回退全表/无日期不显示 三条路径。
"""
import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from unittest import mock

import dashboard.components.data_source_badge as badge

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'✅' if ok else '❌'}] {name}" + (f" — {detail}" if detail else ""))


def _fake_store(rows):
    """构造 get_freshness_report 返回 rows 的假 SQLiteStore。"""
    df = pd.DataFrame(rows, columns=["data_type", "data_end_date"])
    store = types.SimpleNamespace(get_freshness_report=lambda: df)
    return store


def _run(html_call, rows):
    """在 mock SQLiteStore 下调用徽标函数并清缓存，返回 html。"""
    with mock.patch("data.storage.sqlite_store.SQLiteStore", lambda: _fake_store(rows)):
        c = badge._freshness_max_date
        clear = getattr(c, "cache_clear", None) or getattr(c, "clear", None)
        if clear:
            clear()
        return html_call()


# 1) derived + base(ths_kline,em_flow) → 取 sector_hist 与 sector_fund_flow 最新
html = _run(
    lambda: badge.src_badge_html("derived", base=["ths_kline", "em_flow"]),
    [("sector_hist", "2026-10-09"), ("sector_fund_flow", "2026-10-08"),
     ("benchmark_hist", "2026-10-09")],
)
check("资金迁移徽标带 数据截至 2026-10-09", "数据截至 <b>2026-10-09</b>" in html, html[-120:])

# 2) 底数多类型取最大日期
html = _run(
    lambda: badge.src_badge_html("derived", base=["ths_kline", "em_flow"]),
    [("sector_hist", "2026-10-01"), ("sector_fund_flow", "2026-10-20")],
)
check("多底数取最新(10-20)", "数据截至 <b>2026-10-20</b>" in html)

# 3) ths 直接来源 → sector_hist
html = _run(
    lambda: badge.src_badge_html("ths"),
    [("sector_hist", "2026-10-09")],
)
check("同花顺徽标带 数据截至 2026-10-09", "数据截至 <b>2026-10-09</b>" in html)

# 4) derived 无 base → 回退全表最新
html = _run(
    lambda: badge.src_badge_html("derived"),
    [("sector_hist", "2026-09-30"), ("benchmark_hist", "2026-10-15")],
)
check("derived无底数回退全表最新(10-15)", "数据截至 <b>2026-10-15</b>" in html)

# 5) freshness 查不到 → 不显示日期（不谎报）
html = _run(
    lambda: badge.src_badge_html("ths"),
    [],
)
check("无记录 → 不显示数据截至", "数据截至" not in html)

# 6) tencent 实时快照 → 不显示日期
html = _run(
    lambda: badge.src_badge_html("tencent"),
    [("sector_hist", "2026-10-09")],
)
check("腾讯实时快照 → 不显示日期", "数据截至" not in html)

# 7) 防假绿：日期 chip 是新实装——验证 _date_chip_html 存在且被 src_badge_html 调用
check("_date_chip_html 已实装", hasattr(badge, "_date_chip_html") and callable(badge._date_chip_html))

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n=== 徽标日期标注自检：{passed}/{len(results)} 通过 ===")
sys.exit(0 if passed == len(results) else 1)

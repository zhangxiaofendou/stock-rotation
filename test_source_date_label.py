"""涨跌分布/来源日期标注回归测试。

验证点：
1) get_source_date_summary 覆盖全部来源维度，且中文名正确；
2) resolve_updown_data_date 应取 freshness 真实日期，而非被过期
   state_snapshot 误导（模拟快照 8/24、freshness 实际 10/09 的场景）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)


def test_source_date_summary_covers_all():
    from dashboard.app import get_source_date_summary
    df = get_source_date_summary()
    needed = ["基准指数(沪深300)", "RS相对强度", "板块行情K线",
              "板块资金流", "行业清单(同花顺)", "持仓数据"]
    for n in needed:
        assert n in df["来源"].values, f"缺少来源标注: {n}"
    assert (df["滞后"].astype(str).str.contains("天前") | df["滞后"].eq("—")).all()


def test_resolve_uses_freshness_not_snapshot(monkeypatch):
    """核心回归：freshness=10-09 但 state_df 来自旧快照 8-24 时，
    返回的日期必须是 10-09（而非被快照误导的 8-24）。
    """
    import pandas as pd
    import dashboard.views.market_temp as mt

    fake = pd.DataFrame([
        {"data_type": "sector_hist", "data_key": "881121",
         "data_end_date": "2026-10-09", "status": "ok"},
    ])

    class FakeStore:
        def get_freshness_report(self):
            return fake

    # 替换模块内的 SQLiteStore，使 resolve 取到的 freshness 是 10-09
    monkeypatch.setattr(mt, "SQLiteStore", lambda: FakeStore())
    try:
        mt.resolve_updown_data_date.clear()
    except Exception:
        pass

    # state_df 模拟来自旧快照（8/24）
    state_df = pd.DataFrame([{"date": "2026-08-24", "sector_code": "881121"}])

    got = mt.resolve_updown_data_date(state_df)
    assert got == "2026-10-09", f"应取真实日期 10-09，实际: {got}"
    assert got != "2026-08-24", "不应被旧快照误导"


def test_resolve_falls_back_to_state_df_when_no_freshness(monkeypatch):
    """freshness 缺失时回退到 state_df 快照日期（保证不崩、不返回空）。"""
    import pandas as pd
    import dashboard.views.market_temp as mt

    class FakeStoreEmpty:
        def get_freshness_report(self):
            return pd.DataFrame()

    monkeypatch.setattr(mt, "SQLiteStore", lambda: FakeStoreEmpty())
    try:
        mt.resolve_updown_data_date.clear()
    except Exception:
        pass
    state_df = pd.DataFrame([{"date": "2026-08-24", "sector_code": "881121"}])
    got = mt.resolve_updown_data_date(state_df)
    assert got == "2026-08-24", f"无 freshness 时应回退快照日期，实际: {got}"


if __name__ == "__main__":
    test_source_date_summary_covers_all()
    print("✅ test_source_date_summary_covers_all 通过")

    # 用 pytest 风格的 monkeypatch 手动模拟
    class Mp:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)
    mp = Mp()
    test_resolve_uses_freshness_not_snapshot(mp)
    print("✅ test_resolve_uses_freshness_not_snapshot 通过（旧快照不误导）")
    test_resolve_falls_back_to_state_df_when_no_freshness(mp)
    print("✅ test_resolve_falls_back_to_state_df_when_no_freshness 通过")
    print("ALL PASS")

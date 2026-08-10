"""
自检：九宫格状态历史（model/state_history.py）
=============================================
覆盖：
  1. 空输入 / 缺列 → 返回 []
  2. 单一状态全程 → 1 段，is_current=True
  3. 多段切换 → 段数、变化日期、交易日数、自然日跨度全部正确
  4. recent_state_runs(n_changes=3) → 最多返回 4 段（3 次变化）
  5. truncated_start 标记：段数不足时首段应标为「至少」
  6. STATE_CELL 覆盖 9 个状态，坐标唯一，且与 StateMachine 的映射语义一致
  7. change_dates 不把观察窗口起点当成一次变化
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model.state_history import (  # noqa: E402
    STATE_CELL,
    STATE_ORDER,
    compress_state_runs,
    recent_state_runs,
    format_runs_path,
    change_dates,
    state_cell,
)
from model.state_machine import StateMachine  # noqa: E402


PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  [PASS] {name}")
    except AssertionError as e:
        FAIL.append((name, str(e)))
        print(f"  [FAIL] {name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")


def _series(pairs):
    """[(date_str, state)] -> DataFrame"""
    return pd.DataFrame(
        {"date": [p[0] for p in pairs], "state": [p[1] for p in pairs]}
    )


# ------------------------------------------------------------
def test_empty_inputs():
    assert compress_state_runs(None) == [], "None 应返回 []"
    assert compress_state_runs(pd.DataFrame()) == [], "空表应返回 []"
    assert compress_state_runs(pd.DataFrame({"date": ["2026-01-01"]})) == [], \
        "缺 state 列应返回 []"
    assert compress_state_runs(pd.DataFrame({"state": ["⑤中性震荡"]})) == [], \
        "缺 date 列应返回 []"
    assert recent_state_runs(None) == [], "None 应返回 []"


def test_single_state_run():
    df = _series([
        ("2026-08-03", "⑤中性震荡"),
        ("2026-08-04", "⑤中性震荡"),
        ("2026-08-05", "⑤中性震荡"),
    ])
    runs = compress_state_runs(df)
    assert len(runs) == 1, f"应压缩为 1 段，实际 {len(runs)}"
    r = runs[0]
    assert r["state"] == "⑤中性震荡"
    assert r["start_date"] == "2026-08-03", r["start_date"]
    assert r["end_date"] == "2026-08-05", r["end_date"]
    assert r["trading_days"] == 3, f"交易日数应为 3，实际 {r['trading_days']}"
    assert r["calendar_days"] == 3, f"自然日应为 3，实际 {r['calendar_days']}"
    assert r["is_current"] is True, "最后一段应标记为当前"


def test_multi_run_dates_and_durations():
    """三段：⑧(3日) → ⑤(2日) → ⑥(4日)，含周末造成的自然日跨度差异。"""
    df = _series([
        ("2026-07-20", "⑧下跌中继"),
        ("2026-07-21", "⑧下跌中继"),
        ("2026-07-22", "⑧下跌中继"),
        ("2026-07-23", "⑤中性震荡"),
        ("2026-07-24", "⑤中性震荡"),
        # 周末跳过 7-25 / 7-26
        ("2026-07-27", "⑥弱转强"),
        ("2026-07-28", "⑥弱转强"),
        ("2026-07-29", "⑥弱转强"),
        ("2026-07-30", "⑥弱转强"),
    ])
    runs = compress_state_runs(df)
    assert len(runs) == 3, f"应压缩为 3 段，实际 {len(runs)}"

    assert [r["state"] for r in runs] == ["⑧下跌中继", "⑤中性震荡", "⑥弱转强"]
    assert [r["trading_days"] for r in runs] == [3, 2, 4], \
        f"交易日数应为 [3,2,4]，实际 {[r['trading_days'] for r in runs]}"

    # 变化日期 = 后续每段的起始日
    assert runs[1]["start_date"] == "2026-07-23", runs[1]["start_date"]
    assert runs[2]["start_date"] == "2026-07-27", runs[2]["start_date"]
    assert runs[0]["end_date"] == "2026-07-22", runs[0]["end_date"]

    # 自然日跨度：7-27 ~ 7-30 = 4 天
    assert runs[2]["calendar_days"] == 4, runs[2]["calendar_days"]
    # 只有最后一段是当前
    assert [r["is_current"] for r in runs] == [False, False, True]


def test_unsorted_input_is_sorted():
    """输入乱序时也要先排序再压缩，否则会切出假段。"""
    df = _series([
        ("2026-07-22", "⑧下跌中继"),
        ("2026-07-20", "⑧下跌中继"),
        ("2026-07-23", "⑤中性震荡"),
        ("2026-07-21", "⑧下跌中继"),
    ])
    runs = compress_state_runs(df)
    assert len(runs) == 2, f"排序后应为 2 段，实际 {len(runs)}（未排序会切出假段）"
    assert runs[0]["trading_days"] == 3, runs[0]["trading_days"]
    assert runs[0]["start_date"] == "2026-07-20", runs[0]["start_date"]


def test_recent_three_changes_returns_four_runs():
    """5 段历史，取最近 3 次变化 → 应只返回最后 4 段。"""
    pairs = []
    for d, s in [
        ("2026-06-01", "①领涨减速"),
        ("2026-06-02", "④强转弱"),
        ("2026-06-03", "⑦持续杀跌"),
        ("2026-06-04", "⑧下跌中继"),
        ("2026-06-05", "⑨底背离"),
    ]:
        pairs.append((d, s))
    runs = recent_state_runs(_series(pairs), n_changes=3)
    assert len(runs) == 4, f"3 次变化应返回 4 段，实际 {len(runs)}"
    assert [r["state"] for r in runs] == ["④强转弱", "⑦持续杀跌", "⑧下跌中继", "⑨底背离"], \
        [r["state"] for r in runs]
    # 被截断的是更早的 ①，保留的首段不是历史第一段 → truncated_start 应为 False
    assert runs[0]["truncated_start"] is False, "保留段之前仍有历史，首段不应标 truncated"
    assert runs[-1]["is_current"] is True


def test_truncated_start_when_history_short():
    """历史只有 2 段但要 3 次变化 → 首段起始日就是数据起点，应标 truncated。"""
    df = _series([
        ("2026-08-03", "⑤中性震荡"),
        ("2026-08-04", "⑥弱转强"),
    ])
    runs = recent_state_runs(df, n_changes=3)
    assert len(runs) == 2, f"历史只有 2 段，应返回 2 段，实际 {len(runs)}"
    assert runs[0]["truncated_start"] is True, "首段应标记为可能更早开始"
    assert runs[1]["truncated_start"] is False


def test_change_dates_excludes_window_start():
    df = _series([
        ("2026-07-20", "⑧下跌中继"),
        ("2026-07-21", "⑤中性震荡"),
        ("2026-07-22", "⑥弱转强"),
    ])
    runs = compress_state_runs(df)
    dates = change_dates(runs)
    assert dates == ["2026-07-21", "2026-07-22"], f"变化日期应排除窗口起点，实际 {dates}"
    assert change_dates([]) == []
    assert change_dates(runs[:1]) == [], "只有 1 段时没有发生过变化"


def test_format_runs_path():
    df = _series([
        ("2026-07-20", "⑧下跌中继"),
        ("2026-07-21", "⑧下跌中继"),
        ("2026-07-22", "⑥弱转强"),
    ])
    runs = recent_state_runs(df, n_changes=3)
    text = format_runs_path(runs)
    assert "⑧下跌中继" in text and "⑥弱转强" in text, text
    assert "→" in text, "多段应有箭头连接"
    assert "(当前)" in text, "当前段应标注"
    assert format_runs_path([]) == "—"


def test_state_cell_mapping_complete_and_unique():
    assert len(STATE_CELL) == 9, f"应覆盖 9 个状态，实际 {len(STATE_CELL)}"
    coords = list(STATE_CELL.values())
    assert len(set(coords)) == 9, f"9 个状态坐标必须互不相同，实际去重后 {len(set(coords))}"
    for (x, y) in coords:
        assert x in (0, 1, 2) and y in (0, 1, 2), f"坐标越界: {(x, y)}"
    assert set(STATE_ORDER) == set(STATE_CELL.keys()), "STATE_ORDER 与 STATE_CELL 状态集合应一致"
    assert state_cell("不存在的状态") is None
    assert state_cell(None) is None


def test_state_cell_matches_state_machine_semantics():
    """坐标必须与状态机 STATE_MAP 的 (RS方向, 趋势) 语义一致，否则读图会误判。"""
    x_of = {"减弱": 0, "走平": 1, "增强": 2}
    y_of = {"下跌": 0, "横盘": 1, "上涨": 2}
    for (rs_dir, trend), state in StateMachine.STATE_MAP.items():
        expect = (x_of[rs_dir], y_of[trend])
        got = STATE_CELL.get(state)
        assert got == expect, f"{state} 坐标应为 {expect}（{rs_dir}+{trend}），实际 {got}"


if __name__ == "__main__":
    print("=" * 60)
    print("自检：九宫格状态历史 model/state_history.py")
    print("=" * 60)
    check("空输入 / 缺列返回空", test_empty_inputs)
    check("单一状态压缩为 1 段", test_single_state_run)
    check("多段切换的日期与持续时间", test_multi_run_dates_and_durations)
    check("乱序输入先排序再压缩", test_unsorted_input_is_sorted)
    check("最近 3 次变化返回 4 段", test_recent_three_changes_returns_four_runs)
    check("历史不足时标记 truncated_start", test_truncated_start_when_history_short)
    check("变化日期排除窗口起点", test_change_dates_excludes_window_start)
    check("演进路径文本格式", test_format_runs_path)
    check("九宫格坐标完整且唯一", test_state_cell_mapping_complete_and_unique)
    check("坐标与状态机语义一致", test_state_cell_matches_state_machine_semantics)

    print("-" * 60)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        for n, e in FAIL:
            print(f"  FAILED: {n} -> {e}")
        sys.exit(1)
    print("全部通过")

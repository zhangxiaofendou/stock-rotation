"""
九宫格状态历史工具
==================
把 StateMachine.calc_state_series() 产出的「逐日状态序列」压缩成「连续状态段」，
用于回答三个问题：

  1. 这个行业最近换过哪几次状态？
  2. 每次变化发生在哪一天？
  3. 每个状态各持续了多久？

纯计算模块：不依赖 streamlit / plotly，便于单元测试与复用。
"""

from __future__ import annotations

from typing import List, Dict, Optional

import pandas as pd


# ============================================================
# 状态 -> 九宫格坐标
# ============================================================
# x = RS动量方向: 0=减弱  1=走平  2=增强
# y = 价格趋势:   0=下跌  1=横盘  2=上涨
#
# 注意：这里刻意「由 state 反推坐标」，而不是像旧的 state_grid 组件那样
# 由 rs_momentum_percentile 反推。原因是状态机存在降级逻辑
# （横截面领跑闸门 ③→②、绝对值斜率门槛 ③→② / ⑥→⑤）：
# 一个板块可能 rs_momentum_percentile > 70（算出来 x=增强），
# 但最终状态被降级为 ②稳健上行（本该 x=走平）。
# 若按分位数定位，散点会落在与其状态标签不一致的格子里，读图必然误判。
STATE_CELL: Dict[str, tuple] = {
    "①领涨减速": (0, 2),
    "②稳健上行": (1, 2),
    "③加速冲顶": (2, 2),
    "④强转弱":   (0, 1),
    "⑤中性震荡": (1, 1),
    "⑥弱转强":   (2, 1),
    "⑦持续杀跌": (0, 0),
    "⑧下跌中继": (1, 0),
    "⑨底背离":   (2, 0),
}

# 按九宫格编号排序的状态列表
STATE_ORDER: List[str] = [
    "①领涨减速", "②稳健上行", "③加速冲顶",
    "④强转弱", "⑤中性震荡", "⑥弱转强",
    "⑦持续杀跌", "⑧下跌中继", "⑨底背离",
]

X_LABELS = ["减弱", "走平", "增强"]   # RS动量方向
Y_LABELS = ["下跌", "横盘", "上涨"]   # 价格趋势


def state_cell(state: str) -> Optional[tuple]:
    """状态 -> (x, y) 网格坐标；未知状态返回 None（调用方决定丢弃或归位中心）。"""
    if state is None:
        return None
    return STATE_CELL.get(str(state).strip())


# ============================================================
# 状态段压缩
# ============================================================
def compress_state_runs(
    series: pd.DataFrame,
    date_col: str = "date",
    state_col: str = "state",
) -> List[Dict]:
    """把逐日状态序列压缩成连续状态段（run-length encoding）。

    参数:
        series: 含 date / state 两列的 DataFrame（其它列忽略）
        date_col / state_col: 列名，便于兼容不同来源

    返回:
        按时间升序的段列表，每段:
            state           状态名
            start_date      该状态首次出现日（= 状态变化发生日），"YYYY-MM-DD"
            end_date        该状态最后一日，"YYYY-MM-DD"
            trading_days    段内交易日数（数据行数）
            calendar_days   自然日跨度 (end - start).days + 1
            is_current      是否为当前仍在持续的最后一段
        输入为空 / 缺列时返回 []
    """
    if series is None:
        return []
    if not isinstance(series, pd.DataFrame) or series.empty:
        return []
    if date_col not in series.columns or state_col not in series.columns:
        return []

    df = series[[date_col, state_col]].dropna()
    if df.empty:
        return []

    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col).reset_index(drop=True)

    dates = df[date_col].tolist()
    states = [str(s) for s in df[state_col].tolist()]

    runs: List[Dict] = []
    start_i = 0
    n = len(states)
    for i in range(1, n + 1):
        if i == n or states[i] != states[start_i]:
            s_date, e_date = dates[start_i], dates[i - 1]
            runs.append({
                "state": states[start_i],
                "start_date": s_date.strftime("%Y-%m-%d"),
                "end_date": e_date.strftime("%Y-%m-%d"),
                "trading_days": i - start_i,
                "calendar_days": int((e_date - s_date).days) + 1,
                "is_current": False,
            })
            start_i = i

    if runs:
        runs[-1]["is_current"] = True
    return runs


def recent_state_runs(
    series: pd.DataFrame,
    n_changes: int = 3,
    date_col: str = "date",
    state_col: str = "state",
) -> List[Dict]:
    """返回覆盖「最近 n_changes 次状态变化」的状态段。

    n 次变化对应 n+1 个状态段（含当前段）。例如 n_changes=3 时返回最多 4 段：
        ⑧下跌中继 →(变化1) ⑤中性震荡 →(变化2) ⑥弱转强 →(变化3) ③加速冲顶(当前)

    首段额外带 ``truncated_start``：为 True 表示这一段的 start_date 就是历史
    数据的起点，该状态实际可能开始得更早，持续时间是「至少」而非「恰好」。
    """
    runs = compress_state_runs(series, date_col=date_col, state_col=state_col)
    if not runs:
        return []

    keep = max(1, int(n_changes) + 1)
    selected = [dict(r) for r in runs[-keep:]]
    # 只有当截取到了整段历史的第一段时，其起始日才可能被数据起点截断
    selected[0]["truncated_start"] = len(runs) <= keep
    for r in selected[1:]:
        r["truncated_start"] = False
    return selected


def format_duration(run: Dict) -> str:
    """把一段的持续时间格式化为易读文本，如 "12 个交易日" / "≥12 个交易日"。"""
    days = run.get("trading_days", 0)
    prefix = "≥" if run.get("truncated_start") else ""
    suffix = "（进行中）" if run.get("is_current") else ""
    return f"{prefix}{days} 个交易日{suffix}"


def format_runs_path(runs: List[Dict]) -> str:
    """把状态段列表拼成一行演进路径。

    形如: "⑧下跌中继 12日 → ⑤中性震荡 5日 → ⑥弱转强 3日(当前)"
    """
    if not runs:
        return "—"
    parts = []
    for r in runs:
        prefix = "≥" if r.get("truncated_start") else ""
        tag = "(当前)" if r.get("is_current") else ""
        parts.append(f"{r['state']} {prefix}{r['trading_days']}日{tag}")
    return " → ".join(parts)


def change_dates(runs: List[Dict]) -> List[str]:
    """从状态段列表中提取「状态变化发生的日期」。

    第一段的 start_date 不算一次变化（它是观察窗口的起点，不是切换点），
    因此返回 len(runs)-1 个日期。
    """
    if not runs or len(runs) < 2:
        return []
    return [r["start_date"] for r in runs[1:]]

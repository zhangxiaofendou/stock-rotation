"""
信号回放（只读历史视图）
======================
PRD §6.2：选定任意历史日期，展示当天市场全貌——
  所有板块九宫格状态分布 / 镜像对配对 / 系统动作建议 / RS与资金流实际值。
用途：实盘验证、开发调试、策略复盘。

本模块只做数据装配，渲染由 dashboard 页面负责。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from config.logger import get_logger

logger = get_logger(__name__)

# 九宫格九态 → 信号语义（用于动作建议）
STATE_ACTION = {
    "①领涨减速": "减仓",
    "②稳健上行": "持有",
    "③加速冲顶": "持有不追",
    "④强转弱": "清仓",
    "⑤中性震荡": "观望",
    "⑥弱转强": "买入",
    "⑦持续杀跌": "回避",
    "⑧下跌中继": "回避",
    "⑨底背离": "左侧关注",
}


def build_replay(date: str) -> Dict:
    """装配指定日期的回放数据。

    返回 dict：{ date, state_df, distribution, mirror_pairs, summary }
    """
    from model.state_machine import StateMachine
    from model.mirror_pair import MirrorPair
    from data.storage.parquet_store import ParquetStore
    from data.storage.sqlite_store import SQLiteStore

    sm = StateMachine(ParquetStore(), SQLiteStore())
    mp = MirrorPair(SQLiteStore(), sm)

    state_df = sm.calc_all_sectors_state(date=date)
    if state_df is None or state_df.empty:
        return {"date": date, "state_df": None, "distribution": {}, "mirror_pairs": [], "summary": "该日期无状态数据"}

    # 状态分布
    dist = state_df["state"].value_counts().to_dict()

    # 动作建议计数
    action_counts = {}
    for st in state_df["state"]:
        act = STATE_ACTION.get(st, "未知")
        action_counts[act] = action_counts.get(act, 0) + 1

    # 镜像对
    try:
        mirror_pairs = mp.find_mirror_pairs(date=date)
    except Exception as e:
        logger.warning("镜像对识别失败: %s", e)
        mirror_pairs = []

    # 信号统计（买入/卖出/持有/观望）
    from model.state_machine import StateMachine as _SM
    signal_counts = {}
    for st in state_df["state"]:
        sig = sm.get_signal(st)
        signal_counts[sig] = signal_counts.get(sig, 0) + 1

    summary = {
        "n_sectors": len(state_df),
        "action_counts": action_counts,
        "signal_counts": signal_counts,
        "n_mirror_pairs": len(mirror_pairs),
        "n_buy": signal_counts.get("买入", 0),
    }

    return {
        "date": date,
        "state_df": state_df,
        "distribution": dist,
        "mirror_pairs": mirror_pairs,
        "summary": summary,
    }

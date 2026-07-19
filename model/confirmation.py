"""
信号仲裁整合层
==============
把「九宫格主信号 + 资金流信号 + 研报共识」三路信号做仲裁，产出最终动作与置信度。

设计原则（与确认面板口径一致）：
  - 仅做确认 / 否决 / 降级，不改变九宫格状态、综合评分或原有操作建议的底层逻辑；
  - 资金流 / 研报信号缺失时按「中性」处理，仲裁退化为「默认确认」（不影响主信号）；
  - 所有取数失败都吞掉异常返回 None，保证仲裁层永不阻断看板 / 报告渲染。
"""

from typing import Dict, List, Optional

import pandas as pd
from config.logger import get_logger

logger = get_logger(__name__)

# 九宫格状态 → 默认动作（与 model.state_machine.STATE_SIGNAL_MAP 对齐：
# 买入→分批建仓（第一批），卖出→减仓，持有→持有，观望→观察）
DEFAULT_ACTION_BY_STATE = {
    "①领涨减速": "减仓",
    "②稳健上行": "持有",
    "③加速冲顶": "减仓",
    "④强转弱":   "减仓",
    "⑤中性震荡": "观察",
    "⑥弱转强":   "分批建仓（第一批）",
    "⑦持续杀跌": "观察",
    "⑧下跌中继": "观察",
    "⑨底背离":   "分批建仓（第一批）",
}


def get_fund_flow_signal_for(sector_code: str, date: str = None) -> Optional[str]:
    """读取某板块最新资金流信号（来自每日管线落盘）。无数据返回 None。"""
    try:
        from data.storage.sqlite_store import SQLiteStore
        df = SQLiteStore().get_sector_fund_flow(sector_code=sector_code, date=date)
        if df is not None and not df.empty:
            return df.iloc[0]["signal"]
    except Exception as e:
        logger.debug(f"读取资金流信号失败 {sector_code}: {e}")
    return None


def get_report_signal_for(sector_code: str) -> Optional[str]:
    """读取某板块研报共识方向，映射为仲裁用的 正向 / 中性 / 反向。无数据返回 None。"""
    try:
        from ai.consensus import compute_sector_consensus
        c = compute_sector_consensus(sector_code)
        if not c.get("has_data"):
            return None
        d = c["direction"]
        if d == "看多":
            return "正向"
        if d == "看空":
            return "反向"
        return "中性"
    except Exception as e:
        logger.debug(f"读取研报信号失败 {sector_code}: {e}")
    return None


def arbitrate_sector(sector_code: str, state: str, date: str = None) -> Dict:
    """对单个板块做信号仲裁。

    参数:
        sector_code: 板块代码
        state: 九宫格状态（如 "⑥弱转强"）
        date: 资金流数据日期（默认最新）
    返回:
        SignalArbitrator.arbitrate 的结果 + 原始状态/动作
    """
    from model.arbitrate import SignalArbitrator
    action = DEFAULT_ACTION_BY_STATE.get(state, "观察")
    state_signal = {"state": state, "action": action, "logic": ""}
    ff = get_fund_flow_signal_for(sector_code, date)
    rp = get_report_signal_for(sector_code)
    arb = SignalArbitrator().arbitrate(
        sector_code=sector_code,
        state_signal=state_signal,
        fund_flow_signal=ff,
        report_signal=rp,
    )
    arb["original_state"] = state
    return arb


def arbitrate_all(state_df: pd.DataFrame, date: str = None) -> Dict:
    """对一批板块做仲裁，返回每板块仲裁结果 + 计数汇总。

    参数:
        state_df: 含 sector_code / state 列的 DataFrame
                  （来自 StateMachine.calc_all_sectors_state）
    返回:
        {
            "results": [ {...arbitrate 结果...}, ... ],
            "counts":  {"强确认": n, "弱确认": n, "否决": n},
            "n": int,
        }
    """
    results: List[Dict] = []
    if state_df is None or state_df.empty:
        return {"results": [], "counts": {"强确认": 0, "弱确认": 0, "否决": 0}, "n": 0}
    for _, r in state_df.iterrows():
        code = r.get("sector_code")
        state = r.get("state")
        if not code or not state:
            continue
        try:
            results.append(arbitrate_sector(code, state, date))
        except Exception as e:
            logger.debug(f"仲裁失败 {code}: {e}")
    counts = {"强确认": 0, "弱确认": 0, "否决": 0}
    for res in results:
        conf = res.get("confidence")
        if conf in counts:
            counts[conf] += 1
    return {"results": results, "counts": counts, "n": len(results)}

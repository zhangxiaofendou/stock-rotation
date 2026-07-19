"""持仓待处理事项引擎。

仅汇总真实持仓、行业状态和用户预设风险边界，输出可追溯的待核验事项。
不读取或虚构个股实时价格，不自动执行交易，也不覆盖九宫格主信号。
"""

from typing import Optional

import pandas as pd

from model.state_machine import StateMachine
from portfolio.holdings import PortfolioHoldings


RISK_STATES = {"①领涨减速", "③加速冲顶", "④强转弱", "⑦持续杀跌", "⑧下跌中继"}
PRIORITY_ORDER = {"高": 0, "中": 1, "低": 2}


class PortfolioAdvisor:
    """生成持仓层面的条件化待处理事项。"""

    def __init__(self, holdings: Optional[PortfolioHoldings] = None):
        self.holdings = holdings or PortfolioHoldings()

    @staticmethod
    def _latest_sector_state_map(sector_states: pd.DataFrame) -> dict:
        """将状态快照规范成 {sector_code: {state, trend, date}}。"""
        if sector_states is None or sector_states.empty or "sector_code" not in sector_states.columns:
            return {}
        required = {"state", "sector_code"}
        if not required.issubset(sector_states.columns):
            return {}
        out = {}
        for row in sector_states.to_dict("records"):
            code = row.get("sector_code")
            if pd.notna(code):
                out[str(code)] = {
                    "state": row.get("state"),
                    "trend": row.get("trend"),
                    "date": str(row.get("date", ""))[:10],
                }
        return out

    @staticmethod
    def _item(priority: str, category: str, security_code: str, security_name: str, message: str, reason: str, sector_state: str = None) -> dict:
        return {
            "优先级": priority,
            "类别": category,
            "代码": security_code,
            "名称": security_name,
            "行业状态": sector_state or "—",
            "待处理事项": message,
            "依据": reason,
        }

    def build_pending_items(self, sector_states: pd.DataFrame = None) -> pd.DataFrame:
        """生成待处理事项，所有事项均是“核验/评估”而非自动交易命令。

        规则：
        1. 已设置止损价：提示核验最新价格，未接入实时价格时不判断是否触发；
        2. 行业状态属于风险状态：提示复核行业主信号和持仓理由；
        3. 单一标的成本占组合 >=30%：提示集中度复核；
        4. 同行业成本占组合 >=40%：提示行业暴露复核；
        5. 未配置行业或行业状态缺失：提示补全映射，不对信号做推断。
        """
        positions = self.holdings.positions()
        if positions.empty:
            return pd.DataFrame(columns=["优先级", "类别", "代码", "名称", "行业状态", "待处理事项", "依据"])

        positions = positions.copy()
        total_cost = float(positions["cost_amount"].sum())
        if total_cost <= 0:
            return pd.DataFrame(columns=["优先级", "类别", "代码", "名称", "行业状态", "待处理事项", "依据"])

        states = self._latest_sector_state_map(sector_states)
        items = []
        sector_costs = positions.groupby("sector_name", dropna=False)["cost_amount"].sum()

        for row in positions.to_dict("records"):
            code, name = str(row["security_code"]), str(row["security_name"])
            cost_ratio = float(row["cost_amount"]) / total_cost
            sector_code = row.get("sector_code")
            sector_name = row.get("sector_name")
            state_info = states.get(str(sector_code)) if pd.notna(sector_code) and sector_code else None
            state = state_info.get("state") if state_info else None

            if pd.notna(row.get("stop_loss")):
                items.append(self._item(
                    "高", "止损核验", code, name,
                    f"请以最新行情核验是否跌破预设止损价 {float(row['stop_loss']):.4f}。",
                    "当前未接入可靠个股实时价，因此不对止损是否触发做自动判断。",
                    state,
                ))

            if not sector_code or not sector_name:
                items.append(self._item(
                    "中", "行业映射", code, name,
                    "请补全所属行业与行业代码，才能关联九宫格状态进行持仓复核。",
                    "当前持仓缺少可关联的行业映射。",
                    state,
                ))
            elif not state_info:
                items.append(self._item(
                    "中", "状态数据", code, name,
                    "行业状态数据暂不可用，请先刷新行业数据后再评估。",
                    "未在当前九宫格状态快照中找到该行业。",
                    state,
                ))
            elif state in RISK_STATES:
                signal = StateMachine.STATE_SIGNAL_MAP.get(state, "观望")
                items.append(self._item(
                    "高" if signal == StateMachine.SIGNAL_SELL else "中",
                    "行业状态", code, name,
                    f"所属行业当前为 {state}（通用信号：{signal}），请复核持仓逻辑、风险预算与退出条件。",
                    "行业状态是通用研究信号，不替代个股价格、基本面或用户风险承受能力判断。",
                    state,
                ))

            if cost_ratio >= 0.30:
                items.append(self._item(
                    "高" if cost_ratio >= 0.50 else "中", "单一标的集中度", code, name,
                    f"该标的成本占组合 {cost_ratio:.1%}，请复核单一标的风险预算。",
                    "阈值：单一标的成本占组合不低于 30%。",
                    state,
                ))

            sector_cost = float(sector_costs.loc[sector_name])
            sector_ratio = sector_cost / total_cost
            if sector_name and sector_ratio >= 0.40:
                items.append(self._item(
                    "高" if sector_ratio >= 0.60 else "中", "行业集中度", code, name,
                    f"“{sector_name}”行业成本占组合 {sector_ratio:.1%}，请按行业整体而非单只股票复核暴露。",
                    "阈值：同一行业成本占组合不低于 40%。",
                    state,
                ))

        result = pd.DataFrame(items)
        if result.empty:
            return pd.DataFrame(columns=["优先级", "类别", "代码", "名称", "行业状态", "待处理事项", "依据"])
        return result.sort_values(
            by=["优先级", "类别", "代码"],
            key=lambda s: s.map(PRIORITY_ORDER) if s.name == "优先级" else s,
        ).reset_index(drop=True)

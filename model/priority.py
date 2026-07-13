"""
状态机优先级规则
================
当两个维度同时变化，可能导致多条路径同时触发时，按以下规则确定唯一状态。

仲裁规则：
  1. 每日只认一个状态：先判断绝对价格趋势变化，再判断相对强弱趋势变化
  2. 风险优先：两个维度同时变化且方向矛盾时，取风险更高的状态
  3. 以上一交易日状态为基准，判断当日最显著的变化方向
"""

from typing import Optional, Tuple
import numpy as np

from config.logger import get_logger

logger = get_logger(__name__)


class PriorityRules:
    """状态机优先级规则"""

    # 状态风险等级：数字越大风险越高
    STATE_RISK_MAP = {
        "①领涨减速": 3,
        "②稳健上行": 1,
        "③加速冲顶": 2,
        "④强转弱": 5,
        "⑤中性震荡": 4,
        "⑥弱转强": 2,
        "⑦持续杀跌": 9,
        "⑧下跌中继": 7,
        "⑨底背离": 6,
    }

    # 趋势变化方向：改善/恶化/不变
    TREND_DIRECTION = {
        ("上涨", "横盘"): "恶化",
        ("上涨", "下跌"): "恶化",
        ("横盘", "下跌"): "恶化",
        ("横盘", "上涨"): "改善",
        ("下跌", "横盘"): "改善",
        ("下跌", "上涨"): "改善",
    }

    def __init__(self):
        pass

    def get_risk_level(self, state: str) -> int:
        """
        获取状态的风险等级

        参数:
            state: 状态字符串

        返回:
            风险等级 (1-9，数字越大风险越高)
        """
        return self.STATE_RISK_MAP.get(state, 5)

    def resolve_conflict(
        self,
        from_state: str,
        trend_change: Optional[str] = None,
        rs_change: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        仲裁规则：当趋势和RS同时变化，确定唯一状态

        参数:
            from_state: 上一交易日状态
            trend_change: 趋势变化方向 "改善"/"恶化"/None(不变)
            rs_change: RS动量方向变化 "增强"/"减弱"/None(不变)

        返回:
            (最终状态, 仲裁理由)
        """
        logger.debug(
            f"优先级仲裁: from={from_state}, trend_change={trend_change}, rs_change={rs_change}"
        )

        # 规则1：每日只认一个状态，趋势优先于RS
        # 如果趋势变化，以趋势为准
        if trend_change is not None and rs_change is not None:
            # 两个维度同时变化
            if trend_change == "恶化" and rs_change == "增强":
                # 趋势恶化 + RS增强 = 底背离(⑨) vs 下跌中继(⑧)
                # 趋势恶化是主要风险，优先采纳
                reason = f"趋势恶化({trend_change})优先于RS增强({rs_change})，采纳趋势方向"
                logger.info(f"仲裁: {reason}")
                return (from_state, reason)

            if trend_change == "改善" and rs_change == "减弱":
                # 趋势改善 + RS减弱 = 领涨减速(①)
                reason = f"趋势改善({trend_change})优先于RS减弱({rs_change})，采纳趋势方向"
                logger.info(f"仲裁: {reason}")
                return (from_state, reason)

            # 同向变化，正常处理
            reason = f"趋势({trend_change})与RS({rs_change})同向，无冲突"
            logger.info(f"仲裁: {reason}")
            return (from_state, reason)

        if trend_change is not None:
            reason = f"仅趋势变化({trend_change})，采纳趋势方向"
            logger.info(f"仲裁: {reason}")
            return (from_state, reason)

        if rs_change is not None:
            reason = f"仅RS变化({rs_change})，采纳RS方向"
            logger.info(f"仲裁: {reason}")
            return (from_state, reason)

        reason = "无显著变化"
        logger.info(f"仲裁: {reason}")
        return (from_state, reason)

    def pick_riskier_state(self, state_a: str, state_b: str) -> str:
        """
        规则2：两个维度矛盾时，取风险更高的状态

        参数:
            state_a: 状态A
            state_b: 状态B

        返回:
            风险更高的状态
        """
        risk_a = self.get_risk_level(state_a)
        risk_b = self.get_risk_level(state_b)

        if risk_a >= risk_b:
            logger.debug(f"取风险更高的状态: {state_a}(风险{risk_a}) >= {state_b}(风险{risk_b})")
            return state_a
        else:
            logger.debug(f"取风险更高的状态: {state_b}(风险{risk_b}) > {state_a}(风险{risk_a})")
            return state_b

    def determine_significant_change(
        self,
        prev_trend: str,
        prev_rs_percentile: float,
        curr_trend: str,
        curr_rs_percentile: float,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        规则3：以上一交易日为基准，判断当日最显著的变化方向

        参数:
            prev_trend: 上一日趋势
            prev_rs_percentile: 上一日RS动量分位数
            curr_trend: 当前趋势
            curr_rs_percentile: 当前RS动量分位数

        返回:
            (趋势变化方向, RS变化方向)，无变化返回None
        """
        # 趋势变化
        trend_change = None
        if prev_trend != curr_trend:
            trend_change = self.TREND_DIRECTION.get((prev_trend, curr_trend))

        # RS动量分位数变化（超过10个百分点视为显著变化）
        rs_change = None
        if prev_rs_percentile is not None and curr_rs_percentile is not None:
            if not np.isnan(prev_rs_percentile) and not np.isnan(curr_rs_percentile):
                delta = curr_rs_percentile - prev_rs_percentile
                if delta > 10:
                    rs_change = "增强"
                elif delta < -10:
                    rs_change = "减弱"

        logger.debug(
            f"变化判断: 趋势 {prev_trend}→{curr_trend}({trend_change}), "
            f"RS分位 {prev_rs_percentile:.1f}→{curr_rs_percentile:.1f}({rs_change})"
        )
        return (trend_change, rs_change)

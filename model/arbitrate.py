"""
信号仲裁机制
============
九宫格状态是主信号，其他信号体系作为确认/否决因子。

仲裁规则：
  - 九宫格+资金流正向+研报正向 → 强确认，正常执行
  - 九宫格+资金流反向 → 弱确认，降级为"观察"
  - 九宫格+研报反向 → 弱确认，降级为"观察"
  - 九宫格+资金流反向+研报反向 → 否决，不输出
"""

from typing import Optional, Dict, List

from config.logger import get_logger

logger = get_logger(__name__)


class SignalArbitrator:
    """信号仲裁机制"""

    # 信号等级
    CONFIRM_STRONG = "强确认"
    CONFIRM_WEAK = "弱确认"
    CONFIRM_VETO = "否决"

    # 降级动作映射：原动作 → 降级动作
    DOWNGRADE_MAP = {
        "加仓": "观察",
        "加仓（第二批）": "观察",
        "分批建仓（第一批）": "观察",
        "分批建仓": "观察",
        "持有": "观察",
        "持有，不追": "观察",
        "持有，设止盈": "观察",
        "减仓": "清仓",
        "减仓→观望": "清仓",
    }

    def __init__(self):
        pass

    # ============================================================
    # 信号仲裁
    # ============================================================
    def arbitrate(
        self,
        sector_code: str,
        state_signal: Dict,
        fund_flow_signal: Optional[str] = None,
        report_signal: Optional[str] = None,
    ) -> Dict:
        """
        仲裁规则：
        - 九宫格+资金流正向+研报正向 → 强确认，正常执行
        - 九宫格+资金流反向 → 弱确认，降级为"观察"
        - 九宫格+研报反向 → 弱确认，降级为"观察"
        - 九宫格+资金流反向+研报反向 → 否决，不输出

        参数:
            sector_code: 板块代码
            state_signal: 九宫格状态信号 dict: {state, action, logic}
            fund_flow_signal: 资金流信号 "正向"/"中性"/"反向"
            report_signal: 研报信号 "正向"/"中性"/"反向"

        返回:
            dict: {
                'sector_code': 板块代码,
                'original_action': 原始动作,
                'final_action': 最终动作,
                'confidence': '强确认'/'弱确认'/'否决',
                'factors': {
                    'state': 九宫格状态详情,
                    'fund_flow': 资金流信号,
                    'report': 研报信号,
                },
                'reason': 仲裁原因,
            }
        """
        logger.info(
            f"信号仲裁: {sector_code}, state={state_signal}, "
            f"fund_flow={fund_flow_signal}, report={report_signal}"
        )

        original_action = state_signal.get("action", "观察")
        original_state = state_signal.get("state", "未知")

        # 收集各信号因子
        factors = {
            "state": {
                "signal": original_state,
                "action": original_action,
                "logic": state_signal.get("logic", ""),
            },
            "fund_flow": fund_flow_signal or "未获取",
            "report": report_signal or "未获取",
        }

        # 判断各信号方向
        flow_is_negative = fund_flow_signal == "反向"
        flow_is_positive = fund_flow_signal == "正向"
        report_is_negative = report_signal == "反向"
        report_is_positive = report_signal == "正向"

        # 仲裁逻辑
        if flow_is_negative and report_is_negative:
            # 两个辅助信号都反向 → 否决
            confidence = self.CONFIRM_VETO
            final_action = "否决，不输出"
            reason = f"资金流反向({fund_flow_signal})且研报反向({report_signal})，否决九宫格信号"

        elif flow_is_negative:
            # 资金流反向 → 弱确认，降级
            confidence = self.CONFIRM_WEAK
            final_action = self._downgrade_action(original_action)
            reason = f"资金流反向({fund_flow_signal})，信号降级: {original_action} → {final_action}"

        elif report_is_negative:
            # 研报反向 → 弱确认，降级
            confidence = self.CONFIRM_WEAK
            final_action = self._downgrade_action(original_action)
            reason = f"研报反向({report_signal})，信号降级: {original_action} → {final_action}"

        elif flow_is_positive and report_is_positive:
            # 两个辅助信号都正向 → 强确认
            confidence = self.CONFIRM_STRONG
            final_action = original_action
            reason = f"资金流正向且研报正向，强确认九宫格信号"

        elif flow_is_positive:
            # 仅资金流正向 → 强确认
            confidence = self.CONFIRM_STRONG
            final_action = original_action
            reason = f"资金流正向，确认九宫格信号"

        elif report_is_positive:
            # 仅研报正向 → 强确认
            confidence = self.CONFIRM_STRONG
            final_action = original_action
            reason = f"研报正向，确认九宫格信号"

        else:
            # 都中性 → 默认确认
            confidence = self.CONFIRM_STRONG
            final_action = original_action
            reason = "辅助信号中性，默认确认九宫格信号"

        result = {
            "sector_code": sector_code,
            "original_action": original_action,
            "final_action": final_action,
            "confidence": confidence,
            "factors": factors,
            "reason": reason,
        }

        logger.info(f"仲裁结果: {sector_code} {confidence} -> {final_action}, 原因: {reason}")
        return result

    # ============================================================
    # 降级处理
    # ============================================================
    def _downgrade_action(self, original_action: str) -> str:
        """
        将原始动作降级

        参数:
            original_action: 原始动作

        返回:
            降级后的动作
        """
        downgraded = self.DOWNGRADE_MAP.get(original_action, "观察")
        logger.debug(f"动作降级: {original_action} → {downgraded}")
        return downgraded

    # ============================================================
    # 批量仲裁
    # ============================================================
    def arbitrate_batch(
        self,
        signals: List[Dict],
    ) -> List[Dict]:
        """
        批量仲裁多个板块的信号

        参数:
            signals: 信号列表，每项含 sector_code, state_signal, fund_flow_signal, report_signal

        返回:
            仲裁结果列表
        """
        results = []
        for sig in signals:
            result = self.arbitrate(
                sector_code=sig.get("sector_code", "未知"),
                state_signal=sig.get("state_signal", {}),
                fund_flow_signal=sig.get("fund_flow_signal"),
                report_signal=sig.get("report_signal"),
            )
            results.append(result)

        # 统计
        strong = sum(1 for r in results if r["confidence"] == self.CONFIRM_STRONG)
        weak = sum(1 for r in results if r["confidence"] == self.CONFIRM_WEAK)
        veto = sum(1 for r in results if r["confidence"] == self.CONFIRM_VETO)
        logger.info(f"批量仲裁完成: 强确认={strong}, 弱确认={weak}, 否决={veto}")

        return results

    # ============================================================
    # 获取可执行信号（过滤否决的）
    # ============================================================
    def get_executable_signals(self, arbitration_results: List[Dict]) -> List[Dict]:
        """
        从仲裁结果中提取可执行的信号（过滤否决的）

        参数:
            arbitration_results: 仲裁结果列表

        返回:
            可执行的信号列表
        """
        executable = [
            r for r in arbitration_results
            if r["confidence"] != self.CONFIRM_VETO
        ]

        logger.info(f"可执行信号: {len(executable)}/{len(arbitration_results)} 条")
        return executable

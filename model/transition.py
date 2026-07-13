"""
状态转换路径与动作表
====================
完整实现PRD中的72条转换路径。
左侧交易风格：买入在⑨⑥，卖出在①④，不在③追、不在⑦砍。

九宫格状态：
  ①领涨减速  ②稳健上行  ③加速冲顶
  ④强转弱    ⑤中性震荡  ⑥弱转强
  ⑦持续杀跌  ⑧下跌中继  ⑨底背离
"""

from config.logger import get_logger

logger = get_logger(__name__)


class TransitionRules:
    """状态转换路径与动作表"""

    # ============================================================
    # 动作类型常量
    # ============================================================
    ACTION_ADD = "加仓"
    ACTION_ADD_BATCH2 = "加仓（第二批）"
    ACTION_BUILD = "分批建仓（第一批）"
    ACTION_HOLD = "持有"
    ACTION_HOLD_SELL = "持有，不追"
    ACTION_REDUCE = "减仓"
    ACTION_CLEAR = "清仓"
    ACTION_STOP_LOSS = "止损"
    ACTION_OBSERVE = "观察"
    ACTION_WATCH = "重点关注"
    ACTION_REDUCE_WATCH = "减仓→观望"
    ACTION_HOLD_STOP = "持有，设止盈"

    # ============================================================
    # 状态缩写映射（便于构建转换表）
    # ============================================================
    STATE_1 = "①领涨减速"
    STATE_2 = "②稳健上行"
    STATE_3 = "③加速冲顶"
    STATE_4 = "④强转弱"
    STATE_5 = "⑤中性震荡"
    STATE_6 = "⑥弱转强"
    STATE_7 = "⑦持续杀跌"
    STATE_8 = "⑧下跌中继"
    STATE_9 = "⑨底背离"

    ALL_STATES = [STATE_1, STATE_2, STATE_3, STATE_4, STATE_5, STATE_6, STATE_7, STATE_8, STATE_9]

    def __init__(self):
        self._transitions = None

    # ============================================================
    # 构建完整的72条转换路径
    # ============================================================
    def _build_transition_table(self) -> dict:
        """
        构建完整的72条转换路径映射表

        返回:
            dict: {(from_state, to_state): (action, logic)}
        """
        transitions = {}

        # ========================================================
        # →③加速冲顶
        # ========================================================
        transitions[(self.STATE_6, self.STATE_3)] = (self.ACTION_HOLD_SELL, "右侧确认但已晚，不追")
        transitions[(self.STATE_2, self.STATE_3)] = (self.ACTION_HOLD, "稳健上行转为加速，持有")
        transitions[(self.STATE_5, self.STATE_3)] = (self.ACTION_OBSERVE, "中性震荡跳转加速，观察确认")
        transitions[(self.STATE_1, self.STATE_3)] = (self.ACTION_HOLD, "减速后重新加速，持有")
        transitions[(self.STATE_9, self.STATE_3)] = (self.ACTION_HOLD_STOP, "底背离反转后加速，设止盈")
        transitions[(self.STATE_8, self.STATE_3)] = (self.ACTION_OBSERVE, "下跌中继跳转加速，观察")
        transitions[(self.STATE_4, self.STATE_3)] = (self.ACTION_HOLD, "弱转强后加速冲顶，持有")
        transitions[(self.STATE_7, self.STATE_3)] = (self.ACTION_OBSERVE, "杀跌后跳转加速，观察")

        # ========================================================
        # →⑥弱转强
        # ========================================================
        transitions[(self.STATE_9, self.STATE_6)] = (self.ACTION_ADD_BATCH2, "底背离确认，加仓第二批")
        transitions[(self.STATE_5, self.STATE_6)] = (self.ACTION_OBSERVE, "震荡转强，观察")
        transitions[(self.STATE_8, self.STATE_6)] = (self.ACTION_OBSERVE, "下跌中继转强，观察")
        transitions[(self.STATE_7, self.STATE_6)] = (self.ACTION_OBSERVE, "杀跌转强，观察")
        transitions[(self.STATE_1, self.STATE_6)] = (self.ACTION_REDUCE, "领涨减速转弱，减仓")
        transitions[(self.STATE_2, self.STATE_6)] = (self.ACTION_OBSERVE, "稳健上行转弱，观察")
        transitions[(self.STATE_3, self.STATE_6)] = (self.ACTION_REDUCE, "加速冲顶后回落，减仓")
        transitions[(self.STATE_4, self.STATE_6)] = (self.ACTION_OBSERVE, "强转弱后反转，观察")

        # ========================================================
        # →⑨底背离
        # ========================================================
        transitions[(self.STATE_8, self.STATE_9)] = (self.ACTION_BUILD, "下跌中继出现底背离，分批建仓第一批")
        transitions[(self.STATE_7, self.STATE_9)] = (self.ACTION_BUILD, "杀跌中底背离，分批建仓第一批")
        transitions[(self.STATE_5, self.STATE_9)] = (self.ACTION_BUILD, "震荡转底背离，分批建仓")
        transitions[(self.STATE_6, self.STATE_9)] = (self.ACTION_OBSERVE, "弱转强回到底背离，观望")
        transitions[(self.STATE_4, self.STATE_9)] = (self.ACTION_OBSERVE, "强转弱继续恶化，观望")
        transitions[(self.STATE_2, self.STATE_9)] = (self.ACTION_REDUCE_WATCH, "稳健上行转底背离，减仓→观望")
        transitions[(self.STATE_1, self.STATE_9)] = (self.ACTION_REDUCE, "领涨减速恶化，减仓")
        transitions[(self.STATE_3, self.STATE_9)] = (self.ACTION_REDUCE, "加速冲顶后回到底背离，减仓")

        # ========================================================
        # →①领涨减速
        # ========================================================
        transitions[(self.STATE_3, self.STATE_1)] = (self.ACTION_HOLD_STOP, "加速冲顶后减速，设止盈线")
        transitions[(self.STATE_2, self.STATE_1)] = (self.ACTION_REDUCE, "稳健上行转减速，减仓")
        transitions[(self.STATE_4, self.STATE_1)] = (self.ACTION_OBSERVE, "强转弱后反转为减速，观察")
        transitions[(self.STATE_6, self.STATE_1)] = (self.ACTION_HOLD, "弱转强后减速，持有")
        transitions[(self.STATE_5, self.STATE_1)] = (self.ACTION_OBSERVE, "震荡转领涨减速，观察")
        transitions[(self.STATE_9, self.STATE_1)] = (self.ACTION_HOLD, "底背离后回升，持有")
        transitions[(self.STATE_8, self.STATE_1)] = (self.ACTION_OBSERVE, "下跌中继转减速上涨，观察")
        transitions[(self.STATE_7, self.STATE_1)] = (self.ACTION_OBSERVE, "杀跌后转减速上涨，观察")

        # ========================================================
        # →②稳健上行
        # ========================================================
        transitions[(self.STATE_1, self.STATE_2)] = (self.ACTION_HOLD, "减速企稳为稳健，持有")
        transitions[(self.STATE_3, self.STATE_2)] = (self.ACTION_HOLD, "加速后回归稳健，持有")
        transitions[(self.STATE_6, self.STATE_2)] = (self.ACTION_OBSERVE, "弱转强进入稳健上行，观察")
        transitions[(self.STATE_5, self.STATE_2)] = (self.ACTION_HOLD, "震荡转稳健上行，持有")
        transitions[(self.STATE_4, self.STATE_2)] = (self.ACTION_OBSERVE, "强转弱转稳健，观察")
        transitions[(self.STATE_9, self.STATE_2)] = (self.ACTION_HOLD, "底背离反转进入稳健上行，持有")
        transitions[(self.STATE_8, self.STATE_2)] = (self.ACTION_OBSERVE, "下跌中继转稳健上行，观察")
        transitions[(self.STATE_7, self.STATE_2)] = (self.ACTION_OBSERVE, "杀跌后转稳健上行，观察")

        # ========================================================
        # →④强转弱
        # ========================================================
        transitions[(self.STATE_1, self.STATE_4)] = (self.ACTION_REDUCE, "领涨减速恶化，减仓")
        transitions[(self.STATE_3, self.STATE_4)] = (self.ACTION_REDUCE, "加速冲顶后转弱，减仓")
        transitions[(self.STATE_2, self.STATE_4)] = (self.ACTION_REDUCE, "稳健上行转弱，减仓")
        transitions[(self.STATE_5, self.STATE_4)] = (self.ACTION_OBSERVE, "震荡转弱，观望")
        transitions[(self.STATE_6, self.STATE_4)] = (self.ACTION_OBSERVE, "弱转强失败，观望")
        transitions[(self.STATE_9, self.STATE_4)] = (self.ACTION_OBSERVE, "底背离转弱，观望")
        transitions[(self.STATE_8, self.STATE_4)] = (self.ACTION_OBSERVE, "下跌中继转弱，观望")
        transitions[(self.STATE_7, self.STATE_4)] = (self.ACTION_OBSERVE, "杀跌转弱，观望")

        # ========================================================
        # →⑤中性震荡（所有路径统一为观望）
        # ========================================================
        for from_state in self.ALL_STATES:
            if from_state != self.STATE_5:
                transitions[(from_state, self.STATE_5)] = (self.ACTION_OBSERVE, "进入中性震荡，观望")

        # ========================================================
        # →⑧下跌中继
        # ========================================================
        transitions[(self.STATE_4, self.STATE_8)] = (self.ACTION_CLEAR, "强转弱恶化为下跌中继，清仓")
        transitions[(self.STATE_1, self.STATE_8)] = (self.ACTION_REDUCE, "领涨减速恶化为下跌中继，减仓")
        transitions[(self.STATE_2, self.STATE_8)] = (self.ACTION_REDUCE, "稳健上行转下跌中继，减仓")
        transitions[(self.STATE_5, self.STATE_8)] = (self.ACTION_OBSERVE, "震荡转下跌中继，观望")
        transitions[(self.STATE_6, self.STATE_8)] = (self.ACTION_OBSERVE, "弱转强失败转下跌中继，观望")
        transitions[(self.STATE_9, self.STATE_8)] = (self.ACTION_OBSERVE, "底背离转下跌中继，观望不加仓")
        transitions[(self.STATE_7, self.STATE_8)] = (self.ACTION_OBSERVE, "杀跌转中继，观望")
        transitions[(self.STATE_3, self.STATE_8)] = (self.ACTION_CLEAR, "加速冲顶后急转下跌中继，清仓")

        # ========================================================
        # →⑦持续杀跌
        # ========================================================
        transitions[(self.STATE_8, self.STATE_7)] = (self.ACTION_CLEAR, "下跌中继恶化，清仓")
        transitions[(self.STATE_4, self.STATE_7)] = (self.ACTION_CLEAR, "强转弱恶化为杀跌，清仓")
        transitions[(self.STATE_1, self.STATE_7)] = (self.ACTION_CLEAR, "领涨减速恶化，清仓")
        transitions[(self.STATE_2, self.STATE_7)] = (self.ACTION_CLEAR, "稳健上行转杀跌，清仓")
        transitions[(self.STATE_5, self.STATE_7)] = (self.ACTION_OBSERVE, "震荡转杀跌，观望")
        transitions[(self.STATE_6, self.STATE_7)] = (self.ACTION_OBSERVE, "弱转强失败转杀跌，观望")
        transitions[(self.STATE_9, self.STATE_7)] = (self.ACTION_STOP_LOSS, "底背离失败转杀跌，止损")
        transitions[(self.STATE_3, self.STATE_7)] = (self.ACTION_CLEAR, "加速冲顶后急转杀跌，清仓")

        return transitions

    # ============================================================
    # 获取转换动作
    # ============================================================
    def get_transition_action(self, from_state: str, to_state: str) -> tuple:
        """
        根据状态转换返回动作

        参数:
            from_state: 上一状态
            to_state: 当前状态

        返回:
            (动作, 逻辑说明)
        """
        if self._transitions is None:
            self._transitions = self._build_transition_table()

        # 同一状态不变
        if from_state == to_state:
            return ("维持", f"状态不变，维持当前操作")

        action = self._transitions.get((from_state, to_state))
        if action is None:
            logger.warning(f"未定义的转换路径: {from_state} → {to_state}")
            return (self.ACTION_OBSERVE, f"未定义路径: {from_state} → {to_state}")

        logger.info(f"转换路径: {from_state} → {to_state}, 动作={action[0]}, 逻辑={action[1]}")
        return action

    # ============================================================
    # 获取所有转换路径
    # ============================================================
    def get_all_transitions(self) -> list:
        """
        返回所有72条转换路径

        返回:
            list of dict: {from, to, action, logic}
        """
        if self._transitions is None:
            self._transitions = self._build_transition_table()

        result = []
        for (from_state, to_state), (action, logic) in self._transitions.items():
            result.append({
                "from": from_state,
                "to": to_state,
                "action": action,
                "logic": logic,
            })

        logger.info(f"返回全部 {len(result)} 条转换路径")
        return result

    # ============================================================
    # 按目标状态查询
    # ============================================================
    def get_transitions_to(self, to_state: str) -> list:
        """
        查询所有指向某个状态的转换路径

        参数:
            to_state: 目标状态

        返回:
            list of dict
        """
        if self._transitions is None:
            self._transitions = self._build_transition_table()

        result = []
        for (from_state, to), (action, logic) in self._transitions.items():
            if to == to_state:
                result.append({
                    "from": from_state,
                    "to": to,
                    "action": action,
                    "logic": logic,
                })

        return result

    # ============================================================
    # 辅助：获取所有买入信号状态
    # ============================================================
    def get_buy_signals(self) -> list:
        """
        获取所有买入相关的状态转换

        买入信号：进入⑨底背离、进入⑥弱转强

        返回:
            list of dict
        """
        if self._transitions is None:
            self._transitions = self._build_transition_table()

        buy_actions = {self.ACTION_ADD, self.ACTION_ADD_BATCH2, self.ACTION_BUILD}
        result = []
        for (from_state, to_state), (action, logic) in self._transitions.items():
            if action in buy_actions:
                result.append({
                    "from": from_state,
                    "to": to_state,
                    "action": action,
                    "logic": logic,
                })

        return result

    # ============================================================
    # 辅助：获取所有卖出信号状态
    # ============================================================
    def get_sell_signals(self) -> list:
        """
        获取所有卖出相关的状态转换

        卖出信号：进入①领涨减速、进入④强转弱、进入⑦持续杀跌

        返回:
            list of dict
        """
        if self._transitions is None:
            self._transitions = self._build_transition_table()

        sell_actions = {self.ACTION_REDUCE, self.ACTION_CLEAR, self.ACTION_STOP_LOSS, self.ACTION_HOLD_STOP}
        result = []
        for (from_state, to_state), (action, logic) in self._transitions.items():
            if action in sell_actions:
                result.append({
                    "from": from_state,
                    "to": to_state,
                    "action": action,
                    "logic": logic,
                })

        return result

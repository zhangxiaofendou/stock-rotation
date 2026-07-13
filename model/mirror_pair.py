"""
板块对镜像识别
==============
只在关联板块组内匹配，不做全市场随机配对。

镜像对规则：
  - ④↔⑥: 同一关联组内，一个板块强转弱(④)、一个板块弱转强(⑥)
  - ③↔⑦: 同一关联组内，一个板块加速冲顶(③)、一个板块持续杀跌(⑦)
  - 交叉验证：如果板块A显示⑥，检查是否有关联板块B显示④
"""

from typing import Optional, List, Dict, Tuple
import numpy as np

from config.logger import get_logger
from config.sector_map import SECTOR_GROUPS, get_sector_name

logger = get_logger(__name__)


class MirrorPair:
    """板块对镜像识别"""

    # 镜像对配对规则：{ (状态A, 状态B): 描述 }
    MIRROR_PAIRS = {
        ("④强转弱", "⑥弱转强"): "强弱转换镜像对",
        ("⑥弱转强", "④强转弱"): "强弱转换镜像对",
        ("③加速冲顶", "⑦持续杀跌"): "极端状态镜像对",
        ("⑦持续杀跌", "③加速冲顶"): "极端状态镜像对",
    }

    def __init__(self, sqlite_store, state_machine):
        """
        初始化镜像对识别器

        参数:
            sqlite_store: SQLiteStore 实例（读取板块分组）
            state_machine: StateMachine 实例（获取板块状态）
        """
        self.sqlite_store = sqlite_store
        self.state_machine = state_machine

    # ============================================================
    # 获取板块所属关联组
    # ============================================================
    def _get_sector_group(self, sector_code: str) -> Optional[str]:
        """
        获取板块所属的关联组

        参数:
            sector_code: 板块代码

        返回:
            关联组名，如 "大金融"，不在任何关联组返回 None
        """
        for group_name, group_info in SECTOR_GROUPS.items():
            if sector_code in group_info["level2_codes"]:
                return group_name
        return None

    def _get_group_sectors(self, group_name: str) -> List[str]:
        """
        获取关联组内所有板块代码

        参数:
            group_name: 关联组名

        返回:
            板块代码列表
        """
        if group_name in SECTOR_GROUPS:
            return SECTOR_GROUPS[group_name]["level2_codes"]
        return []

    # ============================================================
    # 查找镜像对
    # ============================================================
    def find_mirror_pairs(self, date: str = None) -> List[Dict]:
        """
        识别当前镜像对
        在每个关联组内，找④↔⑥、③↔⑦的配对

        参数:
            date: 目标日期，None表示最新

        返回:
            list of dict: {
                'strong_sector': 强势板块代码,
                'strong_state': 强势板块状态,
                'weak_sector': 弱势板块代码,
                'weak_state': 弱势板块状态,
                'group': 关联组名,
                'pair_type': 镜像对类型,
                'confidence': 置信度
            }
        """
        logger.info(f"开始识别镜像对, date={date or '最新'}")

        # 获取所有板块状态
        state_df = self.state_machine.calc_all_sectors_state(date=date)
        if state_df is None or state_df.empty:
            logger.warning("无法获取板块状态，镜像对识别失败")
            return []

        # 构建板块状态映射: {code: state}
        sector_states = {}
        for _, row in state_df.iterrows():
            sector_states[row["sector_code"]] = row["state"]

        # 按关联组分组
        group_sectors = {}
        for code, state in sector_states.items():
            group = self._get_sector_group(code)
            if group is not None:
                if group not in group_sectors:
                    group_sectors[group] = []
                group_sectors[group].append((code, state))

        # 在每个组内查找镜像对
        mirror_pairs = []

        for group_name, sectors in group_sectors.items():
            if len(sectors) < 2:
                continue

            # 收集组内各状态的板块
            state_groups = {}
            for code, state in sectors:
                if state not in state_groups:
                    state_groups[state] = []
                state_groups[state].append(code)

            # 查找 ④↔⑥ 配对
            state_4_codes = state_groups.get("④强转弱", [])
            state_6_codes = state_groups.get("⑥弱转强", [])
            for strong_code in state_6_codes:
                for weak_code in state_4_codes:
                    if strong_code != weak_code:
                        confidence = self._calc_confidence(strong_code, weak_code, date)
                        mirror_pairs.append({
                            "strong_sector": strong_code,
                            "strong_name": get_sector_name(strong_code),
                            "strong_state": "⑥弱转强",
                            "weak_sector": weak_code,
                            "weak_name": get_sector_name(weak_code),
                            "weak_state": "④强转弱",
                            "group": group_name,
                            "pair_type": "强弱转换镜像对",
                            "confidence": confidence,
                        })

            # 查找 ③↔⑦ 配对
            state_3_codes = state_groups.get("③加速冲顶", [])
            state_7_codes = state_groups.get("⑦持续杀跌", [])
            for strong_code in state_3_codes:
                for weak_code in state_7_codes:
                    if strong_code != weak_code:
                        confidence = self._calc_confidence(strong_code, weak_code, date)
                        mirror_pairs.append({
                            "strong_sector": strong_code,
                            "strong_name": get_sector_name(strong_code),
                            "strong_state": "③加速冲顶",
                            "weak_sector": weak_code,
                            "weak_name": get_sector_name(weak_code),
                            "weak_state": "⑦持续杀跌",
                            "group": group_name,
                            "pair_type": "极端状态镜像对",
                            "confidence": confidence,
                        })

        # 按置信度排序
        mirror_pairs.sort(key=lambda x: x["confidence"], reverse=True)

        logger.info(f"镜像对识别完成, 共找到 {len(mirror_pairs)} 对")
        return mirror_pairs

    # ============================================================
    # 计算置信度
    # ============================================================
    def _calc_confidence(self, code_a: str, code_b: str, date: str = None) -> float:
        """
        计算镜像对的置信度

        参数:
            code_a: 板块A代码
            code_b: 板块B代码
            date: 目标日期

        返回:
            置信度 (0.0-1.0)
        """
        # 基础置信度
        confidence = 0.5

        try:
            # 检查两个板块是否都在同一关联组
            group_a = self._get_sector_group(code_a)
            group_b = self._get_sector_group(code_b)
            if group_a == group_b and group_a is not None:
                confidence += 0.2  # 同组加分

            # 检查是否有足够的历史数据
            if self.state_machine is not None:
                state_a = self.state_machine.calc_state_series(code_a)
                state_b = self.state_machine.calc_state_series(code_b)
                if state_a is not None and state_b is not None:
                    if len(state_a) >= 60 and len(state_b) >= 60:
                        confidence += 0.1  # 充足数据加分

                    # 检查状态持续时间（极端状态持续越久越可信）
                    if len(state_a) > 0 and len(state_b) > 0:
                        last_state_a = state_a["state"].iloc[-1]
                        last_state_b = state_b["state"].iloc[-1]

                        # 检查最近5天状态是否稳定
                        recent_a = state_a["state"].tail(5)
                        recent_b = state_b["state"].tail(5)
                        if (recent_a == last_state_a).sum() >= 4:
                            confidence += 0.1
                        if (recent_b == last_state_b).sum() >= 4:
                            confidence += 0.1

        except Exception as e:
            logger.debug(f"计算置信度异常: {e}")

        return min(confidence, 1.0)

    # ============================================================
    # 交叉验证信号
    # ============================================================
    def validate_signal(self, sector_code: str, sector_state: str, date: str = None) -> Tuple[bool, Optional[str], float]:
        """
        交叉验证信号
        如果板块A显示⑥，检查是否有关联板块B显示④

        参数:
            sector_code: 板块代码
            sector_state: 板块当前状态
            date: 目标日期

        返回:
            (is_valid, mirror_sector, confidence)
        """
        logger.info(f"交叉验证信号: {sector_code}, state={sector_state}")

        # 获取板块所属关联组
        group = self._get_sector_group(sector_code)
        if group is None:
            logger.debug(f"板块 {sector_code} 不在任何关联组中")
            return (False, None, 0.0)

        # 确定需要查找的对立状态
        opposite_state = None
        if sector_state == "⑥弱转强":
            opposite_state = "④强转弱"
        elif sector_state == "④强转弱":
            opposite_state = "⑥弱转强"
        elif sector_state == "③加速冲顶":
            opposite_state = "⑦持续杀跌"
        elif sector_state == "⑦持续杀跌":
            opposite_state = "③加速冲顶"

        if opposite_state is None:
            logger.debug(f"板块 {sector_code} 状态 {sector_state} 不参与镜像验证")
            return (True, None, 1.0)  # 非极端状态不需要交叉验证

        # 获取关联组内所有板块状态
        group_codes = self._get_group_sectors(group)
        state_df = self.state_machine.calc_all_sectors_state(date=date)
        if state_df is None or state_df.empty:
            return (False, None, 0.0)

        # 查找对立状态板块
        opposite_codes = []
        for _, row in state_df.iterrows():
            if row["sector_code"] in group_codes and row["state"] == opposite_state:
                if row["sector_code"] != sector_code:
                    opposite_codes.append(row["sector_code"])

        if opposite_codes:
            # 找到镜像板块，信号有效
            confidence = self._calc_confidence(sector_code, opposite_codes[0], date)
            logger.info(
                f"交叉验证通过: {sector_code}({sector_state}) ↔ {opposite_codes[0]}({opposite_state}), "
                f"置信度={confidence:.2f}"
            )
            return (True, opposite_codes[0], confidence)
        else:
            # 未找到镜像板块
            logger.info(f"交叉验证未通过: {sector_code}({sector_state}), 关联组{group}内无{opposite_state}板块")
            return (False, None, 0.0)

"""
市场环境熔断机制
================
当市场出现极端情况时，板块轮动逻辑失效，系统进入防御模式。

触发条件：
  1. 全市场>90%板块下跌且持续≥3天 → 防御模式
  2. 全市场>90%板块处于⑦持续杀跌 → 系统性风险
  3. 沪深300连续5日跌幅>3% → 强制空仓

恢复条件：
  - 涨跌比恢复至50%以上且持续2天 → 退出防御

返回: {
    'mode': 'normal'/'defense',
    'reason': 触发原因,
    'down_ratio': 下跌板块占比,
    'consecutive_days': 连续下跌天数
}
"""

from typing import Optional, Dict, List
import numpy as np
import pandas as pd

from config.logger import get_logger
from config.settings import PARQUET_DIR

logger = get_logger(__name__)


class CircuitBreaker:
    """市场环境熔断机制"""

    # 熔断阈值
    DOWN_RATIO_THRESHOLD = 0.90       # >90%板块下跌
    CONSECUTIVE_DAYS_THRESHOLD = 3    # 持续≥3天
    KILL_STATE_RATIO_THRESHOLD = 0.90  # >90%板块处于⑦持续杀跌
    HS300_DROP_DAYS = 5               # 沪深300连续5日
    HS300_DROP_THRESHOLD = -3.0       # 跌幅>3%
    RECOVERY_RATIO = 0.50             # 恢复阈值：50%上涨
    RECOVERY_DAYS = 2                 # 持续2天

    MODE_NORMAL = "normal"
    MODE_DEFENSE = "defense"

    def __init__(self, parquet_store=None, state_machine=None):
        """
        初始化市场熔断器

        参数:
            parquet_store: ParquetStore 实例
            state_machine: StateMachine 实例
        """
        self.parquet_store = parquet_store
        self.state_machine = state_machine

    # ============================================================
    # 计算下跌板块占比
    # ============================================================
    def _calc_down_ratio(self, date: str = None) -> float:
        """
        计算某日下跌板块的占比

        参数:
            date: 目标日期

        返回:
            下跌板块占比 (0.0-1.0)
        """
        if self.state_machine is None:
            logger.warning("未配置StateMachine，无法计算下跌板块占比")
            return 0.0

        state_df = self.state_machine.calc_all_sectors_state(date=date)
        if state_df is None or state_df.empty:
            return 0.0

        total = len(state_df)
        if total == 0:
            return 0.0

        down_count = (state_df["trend"] == "下跌").sum()
        return down_count / total

    # ============================================================
    # 检查条件1：全市场>90%板块下跌且持续≥3天
    # ============================================================
    def _check_market_down(self, date: str = None) -> Dict:
        """
        检查市场整体下跌情况

        返回:
            dict: {triggered, down_ratio, consecutive_days}
        """
        if self.state_machine is None:
            return {"triggered": False, "down_ratio": 0.0, "consecutive_days": 0}

        # 获取所有板块的历史状态
        import os
        rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "trend")
        if not os.path.exists(rs_dir):
            return {"triggered": False, "down_ratio": 0.0, "consecutive_days": 0}

        # 获取所有板块代码
        sector_codes = []
        for f in os.listdir(rs_dir):
            if f.endswith(".parquet"):
                code = f.replace(".parquet", "").replace("_", ".", 1)
                if not code.endswith(".SI"):
                    code = code.replace("_SI", ".SI")
                sector_codes.append(code)

        if not sector_codes:
            return {"triggered": False, "down_ratio": 0.0, "consecutive_days": 0}

        # 加载所有板块的趋势数据，合并
        all_trends = []
        for code in sector_codes:
            trend_path = os.path.join(rs_dir, f"{code.replace('.', '_')}.parquet")
            if os.path.exists(trend_path):
                try:
                    df = pd.read_parquet(trend_path)
                    if "date" in df.columns and "trend" in df.columns:
                        df["date"] = pd.to_datetime(df["date"])
                        # 取最近30天
                        df = df[df["trend"] != "数据不足"]
                        df = df.tail(30)
                        for _, row in df.iterrows():
                            all_trends.append({
                                "date": row["date"],
                                "code": code,
                                "trend": row["trend"],
                            })
                except Exception as e:
                    logger.debug(f"加载板块 {code} 趋势失败: {e}")

        if not all_trends:
            return {"triggered": False, "down_ratio": 0.0, "consecutive_days": 0}

        trends_df = pd.DataFrame(all_trends)

        # 按日期统计下跌比例
        daily_ratio = trends_df.groupby("date").apply(
            lambda g: (g["trend"] == "下跌").sum() / len(g)
        ).sort_index()

        if daily_ratio.empty:
            return {"triggered": False, "down_ratio": 0.0, "consecutive_days": 0}

        # 取最新日期的数据
        latest_date = daily_ratio.index[-1]
        current_ratio = daily_ratio.iloc[-1]

        # 检查连续天数
        consecutive = 0
        for ratio in reversed(daily_ratio.values):
            if ratio >= self.DOWN_RATIO_THRESHOLD:
                consecutive += 1
            else:
                break

        triggered = (
            current_ratio >= self.DOWN_RATIO_THRESHOLD
            and consecutive >= self.CONSECUTIVE_DAYS_THRESHOLD
        )

        return {
            "triggered": triggered,
            "down_ratio": current_ratio,
            "consecutive_days": consecutive,
            "latest_date": str(latest_date.date()),
        }

    # ============================================================
    # 检查条件2：>90%板块处于⑦持续杀跌
    # ============================================================
    def _check_systematic_risk(self, date: str = None) -> Dict:
        """
        检查系统性风险

        返回:
            dict: {triggered, kill_ratio}
        """
        if self.state_machine is None:
            return {"triggered": False, "kill_ratio": 0.0}

        distribution = self.state_machine.get_state_distribution(date=date)
        if not distribution:
            return {"triggered": False, "kill_ratio": 0.0}

        total = sum(len(codes) for codes in distribution.values())
        if total == 0:
            return {"triggered": False, "kill_ratio": 0.0}

        kill_count = len(distribution.get("⑦持续杀跌", []))
        kill_ratio = kill_count / total

        triggered = kill_ratio >= self.KILL_STATE_RATIO_THRESHOLD

        return {
            "triggered": triggered,
            "kill_ratio": kill_ratio,
        }

    # ============================================================
    # 检查条件3：沪深300连续5日跌幅>3%
    # ============================================================
    def _check_hs300_drop(self, date: str = None) -> Dict:
        """
        检查沪深300连续下跌

        返回:
            dict: {triggered, hs300_drop, consecutive_days}
        """
        if self.parquet_store is None:
            return {"triggered": False, "hs300_drop": 0.0, "consecutive_days": 0}

        # 加载沪深300数据
        hs300_df = self.parquet_store.load_benchmark_hist("000300.SH")
        if hs300_df is None:
            # 尝试 sh000300
            hs300_df = self.parquet_store.load_benchmark_hist("sh000300")

        if hs300_df is None or hs300_df.empty:
            logger.debug("沪深300数据不存在")
            return {"triggered": False, "hs300_drop": 0.0, "consecutive_days": 0}

        # 确保有date和close列
        if "date" not in hs300_df.columns:
            return {"triggered": False, "hs300_drop": 0.0, "consecutive_days": 0}

        # 找close列
        close_col = None
        for col in ["close", "收盘", "close_sector"]:
            if col in hs300_df.columns:
                close_col = col
                break
        if close_col is None:
            close_col = hs300_df.columns[1] if len(hs300_df.columns) > 1 else None
        if close_col is None:
            return {"triggered": False, "hs300_drop": 0.0, "consecutive_days": 0}

        hs300_df["date"] = pd.to_datetime(hs300_df["date"])
        hs300_df = hs300_df.sort_values("date")

        # 取最近数据
        recent = hs300_df.tail(10)
        if len(recent) < 2:
            return {"triggered": False, "hs300_drop": 0.0, "consecutive_days": 0}

        # 计算每日涨跌幅
        recent = recent.copy()
        recent["change"] = recent[close_col].pct_change() * 100

        # 检查最近5天
        last_5 = recent.tail(self.HS300_DROP_DAYS + 1)  # +1 for pct_change offset
        if len(last_5) < self.HS300_DROP_DAYS:
            return {"triggered": False, "hs300_drop": 0.0, "consecutive_days": 0}

        changes = last_5["change"].dropna().tail(self.HS300_DROP_DAYS)
        if len(changes) < self.HS300_DROP_DAYS:
            return {"triggered": False, "hs300_drop": 0.0, "consecutive_days": 0}

        # 检查是否连续5日跌幅>3%
        # 这里解读为：近5个交易日的累计跌幅>3%
        cumulative_return = 1.0
        for c in changes:
            cumulative_return *= (1 + c / 100)
        hs300_drop = (cumulative_return - 1) * 100

        triggered = hs300_drop <= self.HS300_DROP_THRESHOLD

        return {
            "triggered": triggered,
            "hs300_drop": round(hs300_drop, 2),
            "consecutive_days": len(changes),
        }

    # ============================================================
    # 检查恢复条件
    # ============================================================
    def _check_recovery(self) -> bool:
        """
        检查是否满足恢复条件：涨跌比恢复至50%以上且持续2天

        返回:
            True=可以退出防御模式
        """
        if self.state_machine is None:
            return False

        import os
        rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "trend")
        if not os.path.exists(rs_dir):
            return False

        sector_codes = []
        for f in os.listdir(rs_dir):
            if f.endswith(".parquet"):
                code = f.replace(".parquet", "").replace("_", ".", 1)
                if not code.endswith(".SI"):
                    code = code.replace("_SI", ".SI")
                sector_codes.append(code)

        if not sector_codes:
            return False

        all_trends = []
        for code in sector_codes:
            trend_path = os.path.join(rs_dir, f"{code.replace('.', '_')}.parquet")
            if os.path.exists(trend_path):
                try:
                    df = pd.read_parquet(trend_path)
                    if "date" in df.columns and "trend" in df.columns:
                        df["date"] = pd.to_datetime(df["date"])
                        df = df[df["trend"] != "数据不足"]
                        df = df.tail(10)  # 最近10天
                        for _, row in df.iterrows():
                            all_trends.append({
                                "date": row["date"],
                                "trend": row["trend"],
                            })
                except Exception:
                    pass

        if not all_trends:
            return False

        trends_df = pd.DataFrame(all_trends)

        # 按日期统计上涨比例
        daily_ratio = trends_df.groupby("date").apply(
            lambda g: (g["trend"] == "上涨").sum() / len(g)
        ).sort_index()

        if len(daily_ratio) < self.RECOVERY_DAYS:
            return False

        # 检查最近2天是否都>50%
        last_2 = daily_ratio.tail(self.RECOVERY_DAYS)
        return (last_2 >= self.RECOVERY_RATIO).all()

    # ============================================================
    # 主检查入口
    # ============================================================
    def check_market_status(self, date: str = None) -> Dict:
        """
        检查市场环境

        返回:
            dict: {
                'mode': 'normal'/'defense',
                'reason': 触发原因,
                'down_ratio': 下跌板块占比,
                'consecutive_days': 连续下跌天数,
                'kill_ratio': ⑦状态板块占比,
                'hs300_drop': 沪深300跌幅,
                'triggers': 各触发条件详情,
            }
        """
        logger.info(f"检查市场环境, date={date or '最新'}")

        # 检查三个触发条件
        market_down = self._check_market_down(date)
        systematic = self._check_systematic_risk(date)
        hs300 = self._check_hs300_drop(date)

        # 检查恢复条件
        is_recovery = self._check_recovery()

        # 判断模式
        reasons = []
        triggers = {
            "market_down": market_down,
            "systematic_risk": systematic,
            "hs300_drop": hs300,
        }

        if market_down["triggered"]:
            reasons.append(
                f"全市场下跌板块占比{market_down['down_ratio']:.1%}，"
                f"连续{market_down['consecutive_days']}天"
            )

        if systematic["triggered"]:
            reasons.append(
                f"系统性风险：{systematic['kill_ratio']:.1%}板块处于⑦持续杀跌"
            )

        if hs300["triggered"]:
            reasons.append(
                f"沪深300近5日跌幅{hs300['hs300_drop']:.2f}%"
            )

        if reasons and not is_recovery:
            mode = self.MODE_DEFENSE
            reason = "；".join(reasons)
        else:
            mode = self.MODE_NORMAL
            reason = "市场环境正常" if not reasons else f"已恢复：{'; '.join(reasons)}"

        result = {
            "mode": mode,
            "reason": reason,
            "down_ratio": market_down.get("down_ratio", 0.0),
            "consecutive_days": market_down.get("consecutive_days", 0),
            "kill_ratio": systematic.get("kill_ratio", 0.0),
            "hs300_drop": hs300.get("hs300_drop", 0.0),
            "triggers": triggers,
        }

        logger.info(
            f"市场状态: {mode}, 原因: {reason}, "
            f"下跌占比={market_down.get('down_ratio', 0):.1%}, "
            f"连续={market_down.get('consecutive_days', 0)}天, "
            f"杀跌占比={systematic.get('kill_ratio', 0):.1%}"
        )

        return result

    # ============================================================
    # 判断是否可操作
    # ============================================================
    def can_operate(self, date: str = None) -> bool:
        """
        判断当前是否可以正常操作（非防御模式）

        返回:
            True=可正常操作, False=防御模式
        """
        status = self.check_market_status(date=date)
        return status["mode"] == self.MODE_NORMAL

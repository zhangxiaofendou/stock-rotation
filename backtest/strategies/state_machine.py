"""
策略2：九宫格状态轮动（进阶版 / 左侧交易）
========================================
PRD §6.4：
  - 标的池：申万二级行业指数
  - 买入：⑨底背离持续≥persist_days天 → 第一批 1/3 仓位；⑥弱转强 → 第二批 1/3
  - 减仓：①领涨减速 → 减回 1/3
  - 清仓：④强转弱 → 清仓；⑨→⑦/⑧ 持续恶化 → 止损清仓
  - 不追：③加速冲顶 → 不加仓（维持）
  - 上限：单板块最多计划仓位 base_weight 的 2/3（左侧分批）
  - 止损：由引擎按 stop_loss_pct 统一处理

状态序列由 StateMachine 预计算（一次性），回测期逐日读取，无未来函数。
"""

from __future__ import annotations

import pandas as pd
from typing import Dict, List, Optional

from config.logger import get_logger

logger = get_logger(__name__)


class NineGridStrategy:
    def __init__(
        self,
        persist_days: int = 3,
        base_weight: float = 0.10,
        use_state_machine: bool = True,
    ):
        self.persist_days = persist_days
        self.base_weight = base_weight  # 单板块计划仓位（满仓时）
        self.use_state_machine = use_state_machine

    # ------- 状态面板预计算 -------
    def _build_state_panels(self, engine) -> Dict[str, pd.Series]:
        from model.state_machine import StateMachine
        from data.storage.parquet_store import ParquetStore
        from data.storage.sqlite_store import SQLiteStore

        dates = engine.dates
        sm = StateMachine(ParquetStore(), SQLiteStore())
        panels: Dict[str, pd.Series] = {}
        for c in engine.codes:
            try:
                s = sm.calc_state_series(c)
            except Exception as e:
                logger.warning("板块 %s 状态序列计算失败: %s", c, e)
                continue
            if s is None or s.empty:
                continue
            s = s.copy()
            s["date"] = pd.to_datetime(s["date"])
            s = s.sort_values("date").set_index("date")["state"]
            panels[c] = s.reindex(dates).ffill()
        logger.info("九宫格状态面板构建完成：%d 个板块", len(panels))
        return panels

    @staticmethod
    def _persist(state_series: pd.Series, i: int, state: str) -> int:
        cnt = 0
        for j in range(i, -1, -1):
            if j < len(state_series) and state_series.iloc[j] == state:
                cnt += 1
            else:
                break
        return cnt

    def build(self, engine) -> Dict[pd.Timestamp, Dict[str, float]]:
        panels = self._build_state_panels(engine)
        if not panels:
            return {}

        dates = engine.dates
        stage: Dict[str, int] = {c: 0 for c in panels}  # 0空/1初仓/2满仓
        decisions: Dict[pd.Timestamp, Dict[str, float]] = {}

        for i, d in enumerate(dates):
            changed = False
            for c, ser in panels.items():
                if i >= len(ser):
                    continue
                st = ser.iloc[i]
                new_stage = stage[c]
                if st == "⑨底背离" and self._persist(ser, i, "⑨底背离") >= self.persist_days:
                    if stage[c] < 1:
                        new_stage = 1
                        changed = True
                elif st == "⑥弱转强":
                    new_stage = 2
                    changed = True
                elif st == "①领涨减速":
                    new_stage = 1
                    changed = True
                elif st in ("④强转弱", "⑦持续杀跌", "⑧下跌中继"):
                    if stage[c] != 0:
                        new_stage = 0
                        changed = True
                elif st in ("②稳健上行", "③加速冲顶", "⑤中性震荡"):
                    # 持有/不追/观望：维持当前 stage
                    pass
                if new_stage != stage[c]:
                    stage[c] = new_stage
                    changed = True

            if not changed:
                continue

            # 由 stage 推导目标权重，并做总仓位归一化（避免杠杆）
            target: Dict[str, float] = {}
            total = 0.0
            for c, stg in stage.items():
                if stg == 0:
                    w = 0.0
                elif stg == 1:
                    w = self.base_weight / 3.0
                else:
                    w = 2.0 * self.base_weight / 3.0
                target[c] = w
                total += w
            if total > 1.0:
                target = {c: w / total for c, w in target.items()}
            decisions[d] = target

        logger.info("九宫格策略生成 %d 个调仓决策", len(decisions))
        return decisions

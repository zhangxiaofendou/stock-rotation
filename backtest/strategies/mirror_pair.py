"""
策略3：镜像对轮动（高阶版）
==========================
PRD §6.5：
  - 标的池：申万二级行业指数
  - 信号源：识别镜像对（④↔⑥），仅在关联板块组内匹配
  - 信号过滤：无镜像对的⑥信号降低权重（此处：不建仓）
  - 买入：⑥端板块，配对确认后建仓
  - 卖出：⑥端板块转为①/④时卖出

实现：预计算各板块状态面板 + 关联组映射；某板块处⑥且同组存在④即为"配对确认"，
建仓；状态转入①④⑦⑧清仓。与策略2差异在于"必须经过镜像确认"这一过滤门槛。
"""

from __future__ import annotations

import pandas as pd
from typing import Dict, List, Optional

from config.logger import get_logger
from config.sector_map import SECTOR_GROUPS

logger = get_logger(__name__)


class MirrorPairStrategy:
    def __init__(self, base_weight: float = 0.10, persist_days: int = 1):
        self.base_weight = base_weight
        self.persist_days = persist_days

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
        return panels

    @staticmethod
    def _group_of(code: str) -> Optional[str]:
        for g, info in SECTOR_GROUPS.items():
            if code in info["level2_codes"]:
                return g
        return None

    @staticmethod
    def _persist(ser: pd.Series, i: int, state: str) -> int:
        cnt = 0
        for j in range(i, -1, -1):
            if j < len(ser) and ser.iloc[j] == state:
                cnt += 1
            else:
                break
        return cnt

    def build(self, engine) -> Dict[pd.Timestamp, Dict[str, float]]:
        panels = self._build_state_panels(engine)
        if not panels:
            return {}
        # 预计算每组的板块集合
        group_members: Dict[str, List[str]] = {}
        for c in panels:
            g = self._group_of(c)
            if g:
                group_members.setdefault(g, []).append(c)

        dates = engine.dates
        held: Dict[str, bool] = {c: False for c in panels}
        decisions: Dict[pd.Timestamp, Dict[str, float]] = {}

        for i, d in enumerate(dates):
            # 当天每组的状态快照
            group_state: Dict[str, Dict[str, str]] = {}
            for g, members in group_members.items():
                snap = {}
                for c in members:
                    if i < len(panels[c]):
                        snap[c] = panels[c].iloc[i]
                group_state[g] = snap

            changed = False
            for c, ser in panels.items():
                if i >= len(ser):
                    continue
                st = ser.iloc[i]
                g = self._group_of(c)
                snap = group_state.get(g, {})
                confirmed_4 = any(s == "④强转弱" for s in snap.values())

                want = False
                if st == "⑥弱转强" and confirmed_4:
                    want = True
                elif st == "⑨底背离" and self._persist(ser, i, "⑨底背离") >= self.persist_days:
                    # ⑨ 端镜像：同组存在③或⑦（极端镜像对）
                    if any(s in ("③加速冲顶", "⑦持续杀跌") for s in snap.values()):
                        want = True
                elif st in ("①领涨减速", "④强转弱", "⑦持续杀跌", "⑧下跌中继"):
                    want = False  # 卖出
                # ②/③/⑤ 维持

                if want != held[c]:
                    held[c] = want
                    changed = True

            if not changed:
                continue

            target = {c: (self.base_weight if held[c] else 0.0) for c in panels}
            total = sum(target.values())
            if total > 1.0:
                target = {c: w / total for c, w in target.items()}
            decisions[d] = target

        logger.info("镜像对策略生成 %d 个调仓决策", len(decisions))
        return decisions

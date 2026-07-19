"""信号事件账本。

将板块历史状态序列中的“状态发生变化”固化为可审计事件。账本只记录事实：
事件日期、前后状态/信号、72 条路径动作和当日状态指标；不计算绩效，
后续信号绩效、历史回放和盘后报告共同读取此表，避免口径分叉。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from config.logger import get_logger
from config.sector_map import SW_LEVEL2_MAP, get_sector_name
from model.state_machine import StateMachine
from model.transition import TransitionRules

logger = get_logger(__name__)

# 当状态机规则演进时递增该版本；同步会覆写相同日期/板块事件，保留可审计口径。
SOURCE_VERSION = "state_machine_v2_cross_section_abs_momentum"


class SignalEventLedger:
    """从状态机历史序列构建并同步板块状态切换事件。"""

    def __init__(self, sqlite_store, state_machine: StateMachine):
        self.sqlite_store = sqlite_store
        self.state_machine = state_machine
        self.transition_rules = TransitionRules()

    @staticmethod
    def _as_optional_float(value):
        """将 pandas/numpy 数字安全转为 SQLite 可写值。"""
        if value is None or pd.isna(value):
            return None
        return float(value)

    def build_events_for_sector(self, sector_code: str) -> list[tuple]:
        """从一个板块的完整状态序列提取所有状态切换事件。"""
        series = self.state_machine.calc_state_series(sector_code)
        if series is None or len(series) < 2:
            return []

        series = series.sort_values("date").reset_index(drop=True)
        changed = series[series["state"].ne(series["state"].shift())].copy()
        # 首个有效状态没有“前一状态”，不属于转换事件。
        changed = changed.iloc[1:]
        if changed.empty:
            return []

        sector_name = get_sector_name(sector_code)
        events = []
        for index in changed.index:
            row = series.loc[index]
            previous = series.loc[index - 1]
            from_state, to_state = previous["state"], row["state"]
            action, action_logic = self.transition_rules.get_transition_action(from_state, to_state)
            events.append((
                pd.Timestamp(row["date"]).strftime("%Y-%m-%d"),
                sector_code,
                sector_name,
                from_state,
                to_state,
                self.state_machine.get_signal(from_state),
                self.state_machine.get_signal(to_state),
                action,
                action_logic,
                row.get("trend"),
                self._as_optional_float(row.get("rs_percentile")),
                self._as_optional_float(row.get("rs_momentum_percentile")),
                self._as_optional_float(row.get("rs_momentum_cross_pct")),
                SOURCE_VERSION,
            ))
        return events

    def sync(self, sector_codes: Optional[list[str]] = None) -> dict:
        """同步指定板块（默认所有二级行业）的历史状态切换事件。

        该操作幂等，可安全重复执行。返回成功板块数、事件写入数和失败项。
        """
        codes = sector_codes or list(SW_LEVEL2_MAP.keys())
        event_rows, failed, processed = [], [], 0
        for sector_code in codes:
            try:
                event_rows.extend(self.build_events_for_sector(sector_code))
                processed += 1
            except Exception as exc:
                logger.error(f"构建信号事件失败 {sector_code}: {exc}")
                failed.append(sector_code)

        written = self.sqlite_store.upsert_signal_events(event_rows)
        summary = {
            "processed_sectors": processed,
            "written_events": written,
            "failed_sectors": failed,
        }
        logger.info(
            "信号事件账本同步完成：板块 %s，事件 %s，失败 %s",
            processed,
            written,
            len(failed),
        )
        return summary

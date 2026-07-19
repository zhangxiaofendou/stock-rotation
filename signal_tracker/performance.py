"""信号绩效聚合统计。

读取 signal_performance 账本，按时间窗、路径、信号方向与板块聚合样本量、
胜率、收益分布与超额收益，并生成失效预警。所有函数均接受已加载的
perf_df 以复用，避免重复读库；模块本身不重算状态机。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from config.logger import get_logger
from data.storage.sqlite_store import SQLiteStore

logger = get_logger(__name__)

# 重点观察路径（PRD 3.4 / 2.2 核心交易规则中的关键演化）
KEY_PATHS = [
    "⑨底背离→⑥弱转强",
    "⑥弱转强→③加速冲顶",
    "③加速冲顶→①领涨减速",
    "①领涨减速→④强转弱",
    "④强转弱→⑦持续杀跌",
    "④强转弱→⑧下跌中继",
    "②稳健上行→③加速冲顶",
    "②稳健上行→①领涨减速",
    "③加速冲顶→④强转弱",
    "⑥弱转强→④强转弱",
]

# 失效预警默认阈值
DEFAULT_MIN_SAMPLES = 30
DEFAULT_FAIL_THRESHOLD = 0.40


@dataclass
class PerformanceConfig:
    min_samples: int = DEFAULT_MIN_SAMPLES
    fail_threshold: float = DEFAULT_FAIL_THRESHOLD


def _group_stats(df: pd.DataFrame, by) -> pd.DataFrame:
    """对 perf DataFrame 按指定列聚合成败与收益统计。"""
    if df.empty:
        return pd.DataFrame()
    g = df.groupby(by, dropna=False)
    out = g.agg(
        samples=("outcome", "size"),
        success=("outcome", lambda s: int((s == "success").sum())),
        failure=("outcome", lambda s: int((s == "failure").sum())),
        neutral=("outcome", lambda s: int((s == "neutral").sum())),
        avg_return_t5=("return_t5", "mean"),
        avg_return_t20=("return_t20", "mean"),
        avg_excess_t20=("excess_t20", "mean"),
    ).reset_index()
    denom = out["success"] + out["failure"]
    out["win_rate"] = np.where(denom > 0, out["success"] / denom, np.nan)
    return out


def _load_perf(sqlite: Optional[SQLiteStore], perf_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if perf_df is not None:
        return perf_df
    sqlite = sqlite or SQLiteStore()
    return sqlite.get_signal_performance()


def aggregate_overview(window_days: int = 90, perf_df: Optional[pd.DataFrame] = None,
                       sqlite: Optional[SQLiteStore] = None) -> Optional[dict]:
    """按时间窗聚合信号绩效总览。

    返回含 to_state / from_state / path / direction 四张聚合表与窗口元信息；
    无数据返回 None。
    """
    df = _load_perf(sqlite, perf_df)
    if df is None or df.empty:
        return None
    df = df.copy()
    df["event_date"] = pd.to_datetime(df["event_date"])
    max_date = df["event_date"].max()
    anchor = max_date - pd.Timedelta(days=window_days)
    window = df[df["event_date"] >= anchor].copy()
    if window.empty:
        return None
    window["path"] = window["from_state"].astype(str) + "→" + window["to_state"].astype(str)

    return {
        "to_state": _group_stats(window, "to_state"),
        "from_state": _group_stats(window, "from_state"),
        "path": _group_stats(window, "path"),
        "direction": _group_stats(window, "signal_direction"),
        "anchor": anchor,
        "max_date": max_date,
        "n": len(window),
    }


def path_analysis(paths: Optional[list[str]] = None, perf_df: Optional[pd.DataFrame] = None,
                  sqlite: Optional[SQLiteStore] = None) -> pd.DataFrame:
    """比较关键路径（或指定路径）的后续表现。"""
    df = _load_perf(sqlite, perf_df)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["path"] = df["from_state"].astype(str) + "→" + df["to_state"].astype(str)
    target = paths or KEY_PATHS
    sub = df[df["path"].isin(target)]
    if sub.empty:
        return pd.DataFrame(columns=["path", "samples", "success", "failure", "neutral",
                                     "win_rate", "avg_return_t5", "avg_return_t20", "avg_excess_t20"])
    stats = _group_stats(sub, "path")
    order = {p: i for i, p in enumerate(target)}
    stats["__o"] = stats["path"].map(order).fillna(len(target))
    stats = stats.sort_values("__o").drop(columns="__o").reset_index(drop=True)
    return stats


def failure_alerts(config: Optional[PerformanceConfig] = None,
                   perf_df: Optional[pd.DataFrame] = None,
                   sqlite: Optional[SQLiteStore] = None,
                   window_days: int = 90) -> dict:
    """按进入的目标状态（信号类型）聚合，样本充足且胜率低于阈值时预警。

    PRD 3.4 要求“按准确率预警失效”，故默认只看**近 window_days 日**的表现：
    一个历史上 50% 但对近期信号已显著失效的类型，应被及时标出。window_days<=0
    时回退为全样本（长期结构性失效视角）。

    返回 {"alerts": 预警表, "all": 全量 to_state 聚合表, "window": 实际窗口}。
    """
    cfg = config or PerformanceConfig()
    df = _load_perf(sqlite, perf_df)
    if df is None or df.empty:
        return {"alerts": pd.DataFrame(), "all": pd.DataFrame(), "window": None}
    df = df.copy()
    df["event_date"] = pd.to_datetime(df["event_date"])
    actual_window = None
    if window_days and window_days > 0:
        max_date = df["event_date"].max()
        anchor = max_date - pd.Timedelta(days=window_days)
        df = df[df["event_date"] >= anchor].copy()
        actual_window = anchor
        if df.empty:
            return {"alerts": pd.DataFrame(), "all": pd.DataFrame(), "window": actual_window}
    stats = _group_stats(df, "to_state").sort_values("win_rate", na_position="last")
    alerts = stats[
        (stats["samples"] >= cfg.min_samples)
        & (stats["win_rate"] < cfg.fail_threshold)
    ].copy()
    return {"alerts": alerts.reset_index(drop=True), "all": stats.reset_index(drop=True)}


def get_sector_signal_summary(sector_code: str, perf_df: Optional[pd.DataFrame] = None,
                              sqlite: Optional[SQLiteStore] = None) -> dict:
    """返回某板块作为信号源的历史表现摘要，供板块详情增量展示。

    包含：总样本量、整体胜率、平均20日收益，以及按进入目标状态(to_state)的细分。
    """
    df = _load_perf(sqlite, perf_df)
    if df is None or df.empty:
        return {"has_data": False, "samples": 0}
    sub = df[df["sector_code"] == sector_code]
    if sub.empty:
        return {"has_data": False, "samples": 0}
    total = _group_stats(sub, "outcome")
    total_samples = int(len(sub))
    succ = int((sub["outcome"] == "success").sum())
    fail = int((sub["outcome"] == "failure").sum())
    win_rate = succ / (succ + fail) if (succ + fail) > 0 else None
    by_to = _group_stats(sub, "to_state")
    return {
        "has_data": True,
        "samples": total_samples,
        "success": succ,
        "failure": fail,
        "neutral": int((sub["outcome"] == "neutral").sum()),
        "win_rate": win_rate,
        "avg_return_t20": float(sub["return_t20"].mean()) if sub["return_t20"].notna().any() else None,
        "by_to_state": by_to,
    }

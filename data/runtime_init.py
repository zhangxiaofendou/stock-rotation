"""
运行时数据自初始化（云端部署用）
==========================

云端（Streamlit Cloud）部署只有代码 + 已提交的 parquet（板块行情 / RS / 趋势），
没有 SQLite 账本（signal_events / signal_performance）和基准 parquet。
本模块用**已提交的 parquet** 在云端首次访问时重建账本，使信号绩效页无需手动 CLI 即可工作。

设计原则：
  - 幂等：signal_performance 已有数据则直接跳过，不重复劳动。
  - 尽量不依赖网络：signal_events / signal_performance 都由已提交 parquet 算出；
    基准（benchmark）best-effort 下载，失败仅导致超额收益列为空，不影响回报类指标。
  - 本地开发已有数据时不会触发重建，也不改变既有 Git 不提交运行期数据的约定
    （db 是"算出来的"，不是"提交进去的"）。

用法（也可在页面按钮里调用）：
    from data.runtime_init import ensure_signal_performance
    ensure_signal_performance()            # 空则重建，已有则跳过
    ensure_signal_performance(force=True)  # 强制重建
"""
from __future__ import annotations

import logging
from typing import Optional

from data.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def ensure_signal_performance(force: bool = False) -> dict:
    """确保 signal_performance 账本已填充；为空则用已提交 parquet 重建。

    返回 {'built': bool, 'events': int, 'perf': int, 'benchmark_ok': bool}。
    """
    sqlite = SQLiteStore()
    n = sqlite.count_signal_performance()
    if n > 0 and not force:
        logger.info("signal_performance 已有 %s 条，跳过重建。", n)
        return {"built": False, "events": 0, "perf": 0, "benchmark_ok": True}

    # 1) 基准指数（best-effort，失败仅影响超额收益列，不阻断回报类指标）
    benchmark_ok = True
    try:
        from data.daily_pipeline import update_benchmarks
        update_benchmarks()
    except Exception as e:
        logger.warning("基准更新失败（超额收益将留空，可待每日管线补全）: %s", e)
        benchmark_ok = False

    # 2) 板块元数据（云端首次部署 sectors 表为空，signal_events 外键会失败）
    sqlite.ensure_sectors()

    # 3) 信号事件账本（状态机从已提交 parquet 算出，无网络依赖）
    from data.daily_pipeline import sync_signal_events
    sync_signal_events()

    # 3) 信号后续表现（全量重建，依赖已提交板块行情 parquet）
    from signal_tracker.tracker import SignalTracker
    summary = SignalTracker().enrich_events()

    return {
        "built": True,
        "events": int(summary.get("evaluated", 0)),
        "perf": int(summary.get("written", 0)),
        "benchmark_ok": benchmark_ok,
    }

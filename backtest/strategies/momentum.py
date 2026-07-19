"""
策略1：板块动量轮动（基础版）
==========================
PRD §6.3：
  - 标的池：行业指数（数据现实：仅二级行业指数可用，故用全二级板块池替代一级）
  - 选板块：过去 lookback 日相对沪深300超额收益排名前 top_n
  - 调仓频率：每周五盘后
  - 权重：等权
  - 仓位控制：沪深300低于 market_ma 日均线时空仓 cash_when_bear（默认半仓）
  - 止损：单板块亏损 stop_loss_pct 强制移出（由引擎统一处理）

决策在周五收盘后给出，引擎 T+1 开盘执行 → 无未来函数。
"""

from __future__ import annotations

import pandas as pd
from typing import Dict, List

from config.logger import get_logger

logger = get_logger(__name__)


class MomentumStrategy:
    def __init__(
        self,
        lookback: int = 20,
        top_n: int = 5,
        market_ma: int = 20,
        cash_when_bear: float = 0.5,
        rebalance: str = "W-FRI",
    ):
        self.lookback = lookback
        self.top_n = top_n
        self.market_ma = market_ma
        self.cash_when_bear = cash_when_bear
        self.rebalance = rebalance  # 当前仅支持 W-FRI

    def build(self, engine) -> Dict[pd.Timestamp, Dict[str, float]]:
        dates = engine.dates
        # 对齐所有收盘价与基准到主日历（前向填充，处理停牌缺口）
        closes = pd.DataFrame({c: engine.prices[c]["close"].reindex(dates).ffill() for c in engine.codes})
        bench = engine.benchmark["close"].reindex(dates).ffill()

        ret = closes.pct_change(self.lookback)
        bench_ret = bench.pct_change(self.lookback)
        excess = ret.sub(bench_ret, axis=0)

        bench_ma = bench.rolling(self.market_ma).mean()
        bear = bench < bench_ma  # 熊市过滤器

        decisions: Dict[pd.Timestamp, Dict[str, float]] = {}
        for i, d in enumerate(dates):
            if d.weekday() != 4:  # 仅周五
                continue
            row = excess.iloc[i]
            valid = row.dropna()
            if valid.empty:
                continue
            top = valid.nlargest(self.top_n)
            if top.empty:
                continue
            scale = self.cash_when_bear if (i < len(bear) and bool(bear.iloc[i])) else 1.0
            w = (1.0 / len(top)) * scale
            decisions[d] = {c: float(w) for c in top.index}
        logger.info("动量策略生成 %d 个调仓决策", len(decisions))
        return decisions

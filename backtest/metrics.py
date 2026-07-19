"""
回测评估指标
============
输入 BacktestResult，输出与 PRD §6.6 对齐的指标：
  - 年化收益率（CAGR）
  - 最大回撤
  - 夏普比率
  - 胜率 / 盈亏比（按卖出回合统计）
  - 换手率
  - 分年度收益
  - 板块贡献分析
  - 相对沪深300的超额收益

口径约定：T+1 开盘基准，与 signal_performance 一致；无风险利率取 0。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Dict, List, Optional

TRADING_DAYS = 252


def compute_metrics(result) -> Dict:
    """从 BacktestResult 计算评估指标。"""
    eq = result.equity_curve
    if eq.empty:
        return {}

    equity = eq["equity"]
    rets = eq["ret"]

    n = len(eq)
    years = max(n / TRADING_DAYS, 1e-9)
    start_eq = float(equity.iloc[0])
    end_eq = float(equity.iloc[-1])
    total_ret = end_eq / start_eq - 1 if start_eq > 0 else 0.0
    cagr = (end_eq / start_eq) ** (1 / years) - 1 if start_eq > 0 and end_eq > 0 else 0.0

    # 最大回撤
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_dd = float(drawdown.min())

    # 夏普（无风险利率 0）
    vol = float(rets.std(ddof=1)) if n > 1 else 0.0
    daily_rf = 0.0
    sharpe = (rets.mean() - daily_rf) / vol * np.sqrt(TRADING_DAYS) if vol > 0 else 0.0

    # 胜率 / 盈亏比（按卖出回合）
    win_rate, profit_loss_ratio, n_rounds = _win_stats(result.trades)

    # 换手率（年化）
    turnover = _turnover(result.trades, equity.mean(), years)

    # 分年度收益
    annual = _annual_returns(equity)

    # 基准对比
    bench_excess = None
    if "benchmark" in eq.columns:
        bench_total = float(eq["benchmark"].iloc[-1] / eq["benchmark"].iloc[0] - 1) if eq["benchmark"].iloc[0] > 0 else 0.0
        bench_cagr = (eq["benchmark"].iloc[-1] / eq["benchmark"].iloc[0]) ** (1 / years) - 1 if eq["benchmark"].iloc[0] > 0 and eq["benchmark"].iloc[-1] > 0 else 0.0
        bench_excess = {
            "bench_total_ret": bench_total,
            "bench_cagr": bench_cagr,
            "excess_total_ret": total_ret - bench_total,
            "excess_cagr": cagr - bench_cagr,
        }

    return {
        "start_equity": start_eq,
        "end_equity": end_eq,
        "total_ret": total_ret,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "sharpe": float(sharpe),
        "volatility": vol * np.sqrt(TRADING_DAYS),
        "win_rate": win_rate,
        "profit_loss_ratio": profit_loss_ratio,
        "n_rounds": n_rounds,
        "turnover_annual": turnover,
        "annual_returns": annual,
        "benchmark": bench_excess,
    }


def _win_stats(trades) -> (float, float, int):
    """按卖出回合统计胜率与盈亏比。"""
    pnls = []
    for t in trades:
        if t.side == "SELL":
            pnl = (t.price - t.entry_price) * abs(t.shares) - t.cost
            pnls.append(pnl)
    if not pnls:
        return 0.0, 0.0, 0
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls)
    avg_win = np.mean(wins) if wins else 0.0
    avg_loss = abs(np.mean(losses)) if losses else 0.0
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else (float("inf") if avg_win > 0 else 0.0)
    return win_rate, pl_ratio, len(pnls)


def _turnover(trades, avg_equity: float, years: float) -> float:
    """年化换手率 = 累计买入成交额 / (平均权益 × 年数)。"""
    if avg_equity <= 0 or years <= 0:
        return 0.0
    bought = sum(t.notional for t in trades if t.side == "BUY")
    return (bought / (avg_equity * years)) if avg_equity * years > 0 else 0.0


def _annual_returns(equity: pd.Series) -> Dict[str, float]:
    """分年度收益（按年末相对年初）。"""
    s = equity.copy()
    s.index = pd.to_datetime(s.index)
    out: Dict[str, float] = {}
    years = sorted(set(s.index.year))
    for y in years:
        sub = s[s.index.year == y]
        if len(sub) < 2:
            continue
        yr_ret = float(sub.iloc[-1] / sub.iloc[0] - 1)
        out[str(y)] = yr_ret
    return out

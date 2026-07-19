"""
事件驱动回测引擎（核心）
======================
设计要点：
  - 决策/执行分离，杜绝未来函数：第 T 日收盘后策略给出目标权重，
    实际在第 T+1 日开盘价撮合。
  - 交易成本在每次换手时按成交金额双边计收。
  - 支持单标的止损（跌破入场均价 stop_loss_pct 后，下一开盘强平）。
  - 与真实持仓账本、信号绩效完全隔离，只读冻结的历史行情。

对外接口：
  BacktestEngine.from_parquet_store(codes, bench_code, ...) 构造
  engine.run(decisions, stop_loss_pct=...) 运行
  返回 BacktestResult（equity_curve / trades / contributions / params）
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Trade:
    date: str                # 执行日（T+1 开盘）
    code: str
    side: str                # BUY / SELL
    shares: float
    price: float             # 成交价（开盘价）
    notional: float          # 成交金额（正值）
    cost: float              # 交易成本
    weight_before: float
    weight_after: float
    entry_price: float = 0.0 # 成交前该标的成本基准（SELL 时为原始入场价，用于算盈亏）


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame       # date, equity, cash, benchmark, ret, bench_ret
    trades: List[Trade]
    contributions: pd.DataFrame       # 各标的收益贡献
    params: dict
    summary: dict = field(default_factory=dict)


class BacktestEngine:
    """轻量事件驱动回测框架。"""

    def __init__(
        self,
        prices: Dict[str, pd.DataFrame],
        benchmark: pd.DataFrame,
        cost: float = 0.0015,
        init_cash: float = 1_000_000.0,
        benchmark_code: str = "000300.SH",
    ):
        """
        参数:
            prices: {code: DataFrame}，需含 date 列(datetime)、open、close，已按日期升序。
            benchmark: DataFrame，含 date 列、close 列（基准指数）。
            cost: 单边交易成本（PRD 默认约 0.075%，双边合计 ~0.15%；这里传合计值）。
            init_cash: 初始资金。
        """
        self.cost = float(cost)
        self.init_cash = float(init_cash)
        self.benchmark_code = benchmark_code

        # 统一索引为 Timestamp，按日期升序
        self.prices: Dict[str, pd.DataFrame] = {}
        for code, df in prices.items():
            d = df.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.sort_values("date").set_index("date")
            self.prices[code] = d

        bench = benchmark.copy()
        bench["date"] = pd.to_datetime(bench["date"])
        bench = bench.sort_values("date").set_index("date")
        self.benchmark = bench

        self.codes = list(self.prices.keys())

        # 主交易日历 = 所有行情与基准日期的并集，升序
        all_dates: set = set()
        for d in self.prices.values():
            all_dates |= set(d.index)
        all_dates |= set(self.benchmark.index)
        self.dates: List[pd.Timestamp] = sorted(all_dates)
        self._date_pos = {d: i for i, d in enumerate(self.dates)}

    # ============================================================
    # 行情查询辅助
    # ============================================================
    def _price_at(self, code: str, date: pd.Timestamp, col: str) -> Optional[float]:
        s = self.prices[code][col]
        # 取该日期及之前最近的一个值（前视填充，避免停牌缺口）
        sub = s[s.index <= date]
        if sub.empty:
            return None
        return float(sub.iloc[-1])

    def _bench_at(self, date: pd.Timestamp, col: str = "close") -> Optional[float]:
        s = self.benchmark[col]
        sub = s[s.index <= date]
        if sub.empty:
            return None
        return float(sub.iloc[-1])

    def _next_date(self, date: pd.Timestamp) -> Optional[pd.Timestamp]:
        pos = self._date_pos.get(date)
        if pos is None or pos + 1 >= len(self.dates):
            return None
        return self.dates[pos + 1]

    # ============================================================
    # 运行
    # ============================================================
    def run(
        self,
        decisions: Dict[pd.Timestamp, Dict[str, float]],
        stop_loss_pct: Optional[float] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> BacktestResult:
        """运行回测。

        参数:
            decisions: {决策日(Timestamp): {code: 目标权重}}，目标权重之和 ≤ 1，
                      其余为现金。未出现的 code 视为"维持当前权重"。
            stop_loss_pct: 单标的止损比例（如 0.10 = 亏 10% 强平）。None 表示不止损。
            start/end: 回测区间（含）。

        返回:
            BacktestResult
        """
        start_ts = pd.to_datetime(start) if start else self.dates[0]
        end_ts = pd.to_datetime(end) if end else self.dates[-1]
        timeline = [d for d in self.dates if start_ts <= d <= end_ts]
        if not timeline:
            raise ValueError("回测区间无可用交易日")

        cash = self.init_cash
        shares: Dict[str, float] = {c: 0.0 for c in self.codes}
        avg_cost: Dict[str, float] = {c: 0.0 for c in self.codes}
        pending: Optional[Dict[str, float]] = None  # 待下一开盘执行的权重

        equity_rows: List[dict] = []
        trade_rows: List[Trade] = []
        # 各标的累计已实现盈亏（用于板块贡献）
        realized_pnl: Dict[str, float] = {c: 0.0 for c in self.codes}
        # 各标的期间加权权重（用于贡献分析）
        weight_sum: Dict[str, float] = {c: 0.0 for c in self.codes}
        weight_count: Dict[str, float] = {c: 0.0 for c in self.codes}

        for d in timeline:
            # ---- 1) 执行上一日决策（T+1 开盘） ----
            if pending is not None:
                cash, shares, avg_cost, day_trades = self._execute(
                    d, pending, cash, shares, avg_cost
                )
                trade_rows.extend(day_trades)
                pending = None

            # 记录当前持仓权重（用于贡献分析）
            port_val = cash + sum(
                shares[c] * (self._price_at(c, d, "close") or 0) for c in self.codes
            )
            for c in self.codes:
                px = self._price_at(c, d, "close")
                if px and port_val > 0:
                    w = shares[c] * px / port_val
                    weight_sum[c] += w
                    weight_count[c] += 1

            # ---- 2) 当日决策（用截至 d 的数据，下一开盘执行） ----
            new_pending = decisions.get(d)
            if new_pending is not None:
                # 止损：将已跌破止损线的持仓强制清零（下一开盘执行）
                if stop_loss_pct:
                    for c in self.codes:
                        if shares[c] > 0 and avg_cost[c] > 0:
                            px = self._price_at(c, d, "close")
                            if px is not None and px / avg_cost[c] - 1 <= -stop_loss_pct:
                                new_pending = dict(new_pending)
                                new_pending[c] = 0.0
                pending = new_pending

            # ---- 3) 收盘估值 ----
            eq = cash + sum(
                shares[c] * (self._price_at(c, d, "close") or 0) for c in self.codes
            )
            bench = self._bench_at(d)
            equity_rows.append({
                "date": d,
                "equity": eq,
                "cash": cash,
            })
            # 记录基准（归一化到 init_cash）
            if bench is not None:
                b0 = self._bench_at(timeline[0])
                equity_rows[-1]["benchmark"] = (bench / b0) * self.init_cash if b0 else bench

        equity_curve = pd.DataFrame(equity_rows).set_index("date").sort_index()
        equity_curve["ret"] = equity_curve["equity"].pct_change().fillna(0)
        if "benchmark" in equity_curve:
            equity_curve["bench_ret"] = equity_curve["benchmark"].pct_change().fillna(0)

        contributions = self._contributions(equity_curve, weight_sum, weight_count, start_ts, end_ts)

        params = {
            "cost": self.cost,
            "init_cash": self.init_cash,
            "benchmark_code": self.benchmark_code,
            "stop_loss_pct": stop_loss_pct,
            "start": str(start_ts.date()),
            "end": str(end_ts.date()),
            "n_decisions": len(decisions),
            "universe_size": len(self.codes),
        }
        result = BacktestResult(
            equity_curve=equity_curve,
            trades=trade_rows,
            contributions=contributions,
            params=params,
        )
        result.summary = {
            "final_equity": float(equity_curve["equity"].iloc[-1]),
            "n_trades": len(trade_rows),
        }
        return result

    def _execute(self, date, target: Dict[str, float], cash, shares, avg_cost):
        """在 date 开盘价执行目标权重。返回 (cash, shares, avg_cost, trades)。"""
        trades: List[Trade] = []
        # 执行前组合总值（用开盘价估值）
        port_val = cash + sum(shares[c] * (self._price_at(c, date, "open") or 0) for c in self.codes)
        if port_val <= 0:
            return cash, shares, avg_cost, trades

        for c in self.codes:
            w = target.get(c, 0.0)  # 未给出 = 目标权重 0（清仓），不维持旧仓
            px = self._price_at(c, date, "open")
            if px is None or px <= 0:
                continue
            target_val = w * port_val
            target_shares = target_val / px
            delta = target_shares - shares[c]
            if abs(delta) * px < 1.0:  # 忽略极小头寸变动
                continue
            notional = abs(delta) * px
            cost_amt = notional * self.cost
            entry = avg_cost[c]  # 成交前成本基准（每股）
            if delta > 0:  # 买入
                cash -= notional + cost_amt
                # 更新每股持仓成本（加权）：新均价 = (原市值 + 本次投入) / 新万股数
                old_shares = shares[c]
                old_avg = avg_cost[c]
                old_value = old_shares * old_avg
                add_value = delta * px
                new_shares = target_shares
                avg_cost[c] = (old_value + add_value) / new_shares if new_shares > 0 else 0.0
                side = "BUY"
            else:  # 卖出
                sell_shares = -delta
                realized = (px - entry) * sell_shares - cost_amt
                # 记到 contributions 用（在 run 里汇总）
                self._realized_buffer = getattr(self, "_realized_buffer", {})
                self._realized_buffer[c] = self._realized_buffer.get(c, 0.0) + realized
                cash += notional - cost_amt
                if target_shares <= 1e-9:
                    avg_cost[c] = 0.0
                side = "SELL"
            w_before = shares[c] * px / port_val if port_val else 0
            w_after = target_shares * px / port_val if port_val else 0
            trades.append(Trade(
                date=str(date.date()), code=c, side=side,
                shares=delta, price=px, notional=notional, cost=cost_amt,
                weight_before=w_before, weight_after=w_after, entry_price=entry,
            ))
            shares[c] = target_shares
        return cash, shares, avg_cost, trades

    def _contributions(self, equity_curve, weight_sum, weight_count, start_ts, end_ts):
        """各标的收益贡献：加权权重 × 期间累计涨幅。"""
        start_px = {}
        end_px = {}
        for c in self.codes:
            s = self.prices[c]["close"]
            sub = s[s.index <= start_ts]
            start_px[c] = float(sub.iloc[-1]) if not sub.empty else None
            sub2 = s[s.index <= end_ts]
            end_px[c] = float(sub2.iloc[-1]) if not sub2.empty else None
        rows = []
        realized = getattr(self, "_realized_buffer", {})
        for c in self.codes:
            avg_w = weight_sum[c] / weight_count[c] if weight_count[c] > 0 else 0.0
            if start_px[c] and end_px[c] and start_px[c] > 0:
                total_ret = end_px[c] / start_px[c] - 1
            else:
                total_ret = 0.0
            rows.append({
                "code": c,
                "avg_weight": avg_w,
                "total_return": total_ret,
                "realized_pnl": realized.get(c, 0.0),
                "contribution": avg_w * total_ret,
            })
        df = pd.DataFrame(rows).sort_values("contribution", ascending=False)
        return df

    # ============================================================
    # 构造器
    # ============================================================
    @classmethod
    def from_parquet_store(
        cls,
        codes: List[str],
        bench_code: str = "000300.SH",
        cost: float = 0.0015,
        init_cash: float = 1_000_000.0,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> "BacktestEngine":
        """从 ParquetStore 加载板块行情与基准构造引擎。"""
        from data.storage.parquet_store import ParquetStore
        ps = ParquetStore()

        prices: Dict[str, pd.DataFrame] = {}
        # 中文列名 → 英文（板块行情为 日期/开盘/收盘，基准为 date/open/close）
        rename_map = {"日期": "date", "开盘": "open", "收盘": "close",
                      "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}
        for code in codes:
            df = ps.load_index_hist(code, start=start, end=end)
            if df is None or df.empty:
                logger.warning("板块 %s 无行情数据，跳过", code)
                continue
            df = df.rename(columns=rename_map)
            if "open" not in df.columns or "close" not in df.columns:
                logger.warning("板块 %s 行情缺少 open/close 列，跳过", code)
                continue
            prices[code] = df[["date", "open", "close"]]

        bench = ps.load_benchmark_hist(bench_code)
        if bench is None or bench.empty:
            # 退化：用任何一个板块当基准占位，避免崩溃（指标会退化为对比自身）
            logger.warning("未找到基准 %s，基准对比将失效", bench_code)
            first = next(iter(prices.values()))
            bench = first.rename(columns={"收盘": "close"})[["date", "close"]]

        if not prices:
            raise ValueError("没有可用的板块行情数据")

        return cls(prices, bench, cost=cost, init_cash=init_cash, benchmark_code=bench_code)

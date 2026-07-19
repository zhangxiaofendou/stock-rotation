"""信号后续表现追踪。

读取 signal_events 账本，对每个状态切换事件冻结其后续实际表现：

- ``base_price``：T+1 开盘价（模拟次日进场基准，与回测 T+1 规则一致）
- ``close_t5`` / ``close_t20``：T+5 / T+20 交易日收盘价
- ``state_t5`` / ``state_t20``：对应交易日的九宫格状态
- ``return_t5`` / ``return_t20``：相对 base_price 的涨跌幅
- ``excess_t20``：return_t20 减去同期沪深300收益（超额收益）
- ``outcome``：按信号方向（BUY/SELL/HOLD/AVOID）的 T+20 实际收益判定
  success / failure / neutral——形成 PRD 3.4 要求的反馈闭环

结果幂等写入 ``signal_performance`` 表，重算口径会覆盖同一事件记录，
不影响 ``signal_events`` 原始事实源。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional

from config.logger import get_logger
from config.sector_map import SW_LEVEL2_MAP, get_sector_name
from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from model.state_machine import StateMachine

logger = get_logger(__name__)

# 超额收益基准：统一用沪深300（覆盖期最长、跨板块可比）。
BENCHMARK_FILE = "benchmark_sh000300.parquet"

# 信号方向：基于进入的目标状态（to_state）的通用买卖语义。
_BUY_STATES = {"⑥弱转强", "⑨底背离"}
_SELL_STATES = {"①领涨减速", "④强转弱"}
_HOLD_STATES = {"②稳健上行", "③加速冲顶"}
_AVOID_STATES = {"⑦持续杀跌", "⑧下跌中继"}


class SignalTracker:
    """冻结信号事件的后续实际表现。"""

    def __init__(self, sqlite_store: Optional[SQLiteStore] = None,
                 parquet_store: Optional[ParquetStore] = None,
                 state_machine: Optional[StateMachine] = None):
        self.sqlite = sqlite_store or SQLiteStore()
        self.parquet = parquet_store or ParquetStore()
        self.sm = state_machine or StateMachine(self.parquet, self.sqlite)
        self._benchmark: Optional[pd.DataFrame] = None

    # ============================================================
    # 静态判定工具
    # ============================================================
    @staticmethod
    def signal_direction(to_state: str) -> str:
        """基于目标状态的通用买卖方向。"""
        if to_state in _BUY_STATES:
            return "BUY"
        if to_state in _SELL_STATES:
            return "SELL"
        if to_state in _HOLD_STATES:
            return "HOLD"
        if to_state in _AVOID_STATES:
            return "AVOID"
        return "NEUTRAL"

    @staticmethod
    def evaluate_outcome(from_state: str, to_state: str,
                         excess_t20: Optional[float] = None,
                         return_t20: Optional[float] = None) -> str:
        """按信号方向的收益表现判定成败——信号绩效反馈闭环的核心口径。

        PRD 3.4 要求“形成反馈闭环、按准确率预警失效”，因此成败必须反映
        信号发出后的实际收益，而非静态路径是否“看起来对”：

        - BUY（⑥弱转强/⑨底背离）、HOLD（②稳健上行/③加速冲顶）：
          T+20 收益（优先用相对沪深300的超额收益）为正 → 成功，否则失败；
        - SELL（①领涨减速/④强转弱）、AVOID（⑦持续杀跌/⑧下跌中继）：
          T+20 收益为负（回避了下跌/风险兑现）→ 成功，否则失败；
        - NEUTRAL（⑤中性震荡，观望信号）：不参与成败，标 neutral。

        无收益数据（近期事件尚未走完 20 日、或基准缺失）时退化为 neutral，
        避免对未验证信号误判。
        """
        direction = SignalTracker.signal_direction(to_state)
        if direction == "NEUTRAL":
            return "neutral"
        r = excess_t20
        if r is None or (isinstance(r, float) and np.isnan(r)):
            r = return_t20
        if r is None or (isinstance(r, float) and np.isnan(r)):
            return "neutral"
        if direction in ("BUY", "HOLD"):
            return "success" if r > 0 else "failure"
        return "success" if r < 0 else "failure"

    # ============================================================
    # 数据加载与定位
    # ============================================================
    def _load_benchmark(self) -> Optional[pd.DataFrame]:
        if self._benchmark is not None:
            return self._benchmark
        path = self.parquet.index_hist_dir / BENCHMARK_FILE
        if not path.exists():
            logger.warning("基准文件缺失，超额收益将留空: %s", path)
            self._benchmark = None
            return None
        df = pd.read_parquet(path).rename(columns={"date": "date", "open": "open", "close": "close"})
        df["date"] = pd.to_datetime(df["date"])
        df = df.dropna(subset=["date", "close", "open"]).sort_values("date").reset_index(drop=True)
        self._benchmark = df
        return df

    @staticmethod
    def _pos_after(dates_np: np.ndarray, target: str, n: int) -> Optional[int]:
        """返回 dates_np（升序 datetime64）中 target 之后第 n 个交易日的整数下标。

        target 当日缺失或偏移超出序列则返回 None。
        """
        ts = np.datetime64(pd.Timestamp(target))
        j = int(np.searchsorted(dates_np, ts, side="left"))
        if j >= len(dates_np) or dates_np[j] != ts:
            return None
        pos = j + n
        return pos if pos < len(dates_np) else None

    # ============================================================
    # 主流程
    # ============================================================
    def enrich_events(self, sector_codes: Optional[list[str]] = None,
                      since_date: Optional[str] = None) -> dict:
        """补全全部（或指定）板块信号事件的后续表现并写库。幂等可重跑。

        since_date 用于增量模式：只处理该日期之后发生的事件（其 T+20 表现
        尚未固定），避免每日全量重算。不传则为全量回填（首次或口径演进时）。

        返回 processed_sectors / evaluated / skipped / written / failed_sectors。
        """
        codes = sector_codes or list(SW_LEVEL2_MAP.keys())
        code_set = set(codes)

        events = self.sqlite.get_signal_events(start=since_date)
        if events is None or events.empty:
            logger.info("信号事件账本为空，跳过后续表现补全")
            return {"processed_sectors": 0, "evaluated": 0, "skipped": 0, "written": 0, "failed_sectors": []}
        events = events[events["sector_code"].isin(code_set)].copy()

        benchmark = self._load_benchmark()
        bench_dates = benchmark["date"].values if benchmark is not None else None
        bench_close = benchmark["close"].values if benchmark is not None else None
        bench_open = benchmark["open"].values if benchmark is not None else None

        rows = []
        evaluated = 0
        skipped = 0
        failed = []

        for code, grp in events.groupby("sector_code", sort=False):
            try:
                price = self.parquet.load_index_hist(code)
                if price is None or price.empty:
                    skipped += len(grp)
                    continue
                price = price.rename(columns={"日期": "date", "收盘": "close", "开盘": "open"})
                price["date"] = pd.to_datetime(price["date"])
                price = price.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
                p_dates = price["date"].values
                p_close = price["close"].values
                p_open = price["open"].values if "open" in price.columns else p_close

                state_df = self.sm.calc_state_series(code)
                state_map = {}
                if state_df is not None and not state_df.empty:
                    sd = state_df.copy()
                    sd["date"] = pd.to_datetime(sd["date"])
                    sd = sd.dropna(subset=["date", "state"]).sort_values("date")
                    state_map = dict(zip(sd["date"].astype(str).str[:10], sd["state"]))

                for rec in grp.to_dict("records"):
                    d = str(rec["event_date"])[:10]
                    pos0 = self._pos_after(p_dates, d, 0)
                    pos1 = self._pos_after(p_dates, d, 1)
                    pos5 = self._pos_after(p_dates, d, 5)
                    pos20 = self._pos_after(p_dates, d, 20)
                    if pos0 is None or pos1 is None or pos5 is None:
                        skipped += 1
                        continue

                    base_price = float(p_open[pos1]) if not np.isnan(p_open[pos1]) else float(p_close[pos1])
                    price_t0 = float(p_close[pos0])
                    close_t5 = float(p_close[pos5])
                    close_t20 = float(p_close[pos20]) if pos20 is not None else None
                    state_t5 = state_map.get(str(p_dates[pos5])[:10])
                    state_t20 = state_map.get(str(p_dates[pos20])[:10]) if pos20 is not None else None

                    ret5 = (close_t5 - base_price) / base_price if base_price else None
                    ret20 = (close_t20 - base_price) / base_price if (close_t20 is not None and base_price) else None

                    excess20 = None
                    if ret20 is not None and bench_dates is not None:
                        b1 = self._pos_after(bench_dates, d, 1)
                        b20 = self._pos_after(bench_dates, str(p_dates[pos20])[:10], 0) if pos20 is not None else None
                        if b1 is not None and b20 is not None:
                            b_base = float(bench_open[b1]) if not np.isnan(bench_open[b1]) else float(bench_close[b1])
                            b_end = float(bench_close[b20])
                            if b_base:
                                excess20 = ret20 - (b_end - b_base) / b_base

                    from_state = rec["from_state"]
                    to_state = rec["to_state"]
                    direction = self.signal_direction(to_state)
                    outcome = self.evaluate_outcome(
                        from_state, to_state, excess_t20=excess20, return_t20=ret20
                    )

                    rows.append((
                        d, code, rec.get("sector_name"), from_state, to_state,
                        direction, price_t0, base_price, close_t5, close_t20,
                        state_t5, state_t20, ret5, ret20, excess20, outcome,
                    ))
                    evaluated += 1
            except Exception as exc:
                logger.error("补全信号后续表现失败 %s: %s", code, exc)
                failed.append(code)

        written = self.sqlite.upsert_signal_performance(rows)
        summary = {
            "processed_sectors": events["sector_code"].nunique() if not events.empty else 0,
            "evaluated": evaluated,
            "skipped": skipped,
            "written": written,
            "failed_sectors": failed,
        }
        logger.info(
            "信号后续表现补全完成：板块 %s，评估 %s，跳过 %s，写入 %s，失败 %s",
            summary["processed_sectors"], evaluated, skipped, written, len(failed),
        )
        return summary


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )
    tracker = SignalTracker()
    result = tracker.enrich_events()
    print("信号后续表现补全：", result)

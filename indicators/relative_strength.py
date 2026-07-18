"""
相对强弱指标（RS）计算模块
==========================
九宫格模型的核心模块，计算板块相对于基准的各项强弱指标。

指标说明：
  - RS: 板块收盘价 / 基准收盘价（相对强弱）
  - RS分位数: RS在当前历史窗口中的百分位（位置判断）
  - RS动量: RS序列的线性回归斜率（方向判断）
  - RS动量分位数: 动量在历史窗口中的百分位（增强/减弱/走平）
  - RS加速度: 动量的变化量（拐点判断）
"""

from typing import Optional, Dict
import numpy as np
import pandas as pd
from scipy import stats

from config.logger import get_logger

logger = get_logger(__name__)


class RelativeStrength:
    """相对强弱指标计算"""

    def __init__(self, parquet_store, sqlite_store):
        """
        初始化相对强弱计算器

        参数:
            parquet_store: ParquetStore 实例
            sqlite_store: SQLiteStore 实例
        """
        self.parquet_store = parquet_store
        self.sqlite_store = sqlite_store

    # ============================================================
    # 内部辅助方法
    # ============================================================
    def _load_sector_data(self, sector_code: str) -> Optional[pd.DataFrame]:
        """
        加载板块指数数据并标准化列名

        参数:
            sector_code: 板块代码，如 "801012.SI"

        返回:
            DataFrame，列名为 date/close/open/high/low/volume/amount
            如果数据不存在返回 None
        """
        df = self.parquet_store.load_index_hist(sector_code)
        if df is None or df.empty:
            logger.warning(f"板块 {sector_code} 数据不存在或为空")
            return None

        # 标准化列名：中文列名 -> 英文列名
        col_map = {
            "日期": "date",
            "收盘": "close",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "代码": "code",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        # 确保日期列为 datetime
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df

    def _load_benchmark_data(self, benchmark_code: str) -> Optional[pd.DataFrame]:
        """
        加载基准指数数据并标准化列名

        参数:
            benchmark_code: 基准代码，如 "000300.SH"

        返回:
            DataFrame，列名为 date/close
        """
        # 尝试多种格式加载基准数据
        # parquet文件命名：benchmark_sh000300.parquet
        # benchmark_code: "000300.SH"
        df = self.parquet_store.load_benchmark_hist(benchmark_code)
        if df is None:
            # 提取纯数字部分，如 "000300.SH" -> "000300"
            numeric_part = benchmark_code.split(".")[0]
            # 判断市场后缀：.SH -> sh, .SZ -> sz
            if benchmark_code.endswith(".SH"):
                sh_code = f"sh{numeric_part}"
            elif benchmark_code.endswith(".SZ"):
                sh_code = f"sz{numeric_part}"
            else:
                sh_code = f"sh{numeric_part}"
            df = self.parquet_store.load_benchmark_hist(sh_code)

        if df is None or df.empty:
            logger.warning(f"基准指数 {benchmark_code} 数据不存在或为空")
            return None

        # 确保日期列为 datetime
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
        return df

    def _align_dates(self, sector_df: pd.DataFrame, benchmark_df: pd.DataFrame) -> pd.DataFrame:
        """
        对齐板块和基准的日期，取交集

        参数:
            sector_df: 板块数据 DataFrame，含 date/close 列
            benchmark_df: 基准数据 DataFrame，含 date/close 列

        返回:
            对齐后的 DataFrame，含 date/sector_close/bench_close 列
        """
        # 取日期交集
        common_dates = pd.Index(sector_df["date"]).intersection(pd.Index(benchmark_df["date"]))
        if len(common_dates) == 0:
            logger.error("板块和基准无共同交易日")
            return pd.DataFrame()

        sector_aligned = sector_df[sector_df["date"].isin(common_dates)].copy()
        benchmark_aligned = benchmark_df[benchmark_df["date"].isin(common_dates)].copy()

        # 合并
        merged = pd.merge(
            sector_aligned[["date", "close"]],
            benchmark_aligned[["date", "close"]],
            on="date",
            suffixes=("_sector", "_bench"),
            how="inner",
        )
        merged = merged.sort_values("date").reset_index(drop=True)
        return merged

    def _calc_linear_slope(self, y: np.ndarray) -> float:
        """
        计算序列的线性回归斜率

        参数:
            y: 待计算斜率的序列

        返回:
            斜率值，如果计算失败返回 0.0
        """
        if len(y) < 2:
            return 0.0
        try:
            x = np.arange(len(y))
            slope, _ = np.polyfit(x, y, 1)
            return float(slope)
        except Exception:
            return 0.0

    def _calc_percentile(self, series: pd.Series, window: int) -> pd.Series:
        """
        计算滚动百分位

        参数:
            series: 数值序列
            window: 滚动窗口大小

        返回:
            百分位序列 (0-100)
        """
        result = pd.Series(np.nan, index=series.index, dtype=float)
        for i in range(len(series)):
            if i < window - 1:
                continue
            window_values = series.iloc[i - window + 1 : i + 1]
            current = series.iloc[i]
            # 计算当前值在窗口中的百分位排名
            rank = (window_values < current).sum() + (window_values == current).sum() * 0.5
            result.iloc[i] = (rank / window) * 100
        return result

    # ============================================================
    # 核心指标计算
    # ============================================================
    def calc_rs(self, sector_code: str, benchmark_code: str) -> Optional[pd.DataFrame]:
        """
        计算相对强弱序列

        RS = 板块指数收盘价 / 基准指数收盘价

        参数:
            sector_code: 板块代码
            benchmark_code: 基准指数代码

        返回:
            DataFrame，含 date/rs 列
        """
        logger.info(f"计算相对强弱: {sector_code} vs {benchmark_code}")

        sector_df = self._load_sector_data(sector_code)
        benchmark_df = self._load_benchmark_data(benchmark_code)

        if sector_df is None or benchmark_df is None:
            return None

        merged = self._align_dates(sector_df, benchmark_df)
        if merged.empty:
            return None

        merged["rs"] = merged["close_sector"] / merged["close_bench"]
        result = merged[["date", "rs"]].copy()

        logger.info(f"RS计算完成: {sector_code}, 共 {len(result)} 条")
        return result

    def calc_rs_percentile(
        self, sector_code: str, benchmark_code: str, window: int = 250
    ) -> Optional[pd.DataFrame]:
        """
        计算RS分位数（位置指标）

        RS_分位 = 当前RS在过去window日的百分位
        > 80% = 高位, < 20% = 低位

        参数:
            sector_code: 板块代码
            benchmark_code: 基准指数代码
            window: 百分位窗口（默认250个交易日 ≈ 1年）

        返回:
            DataFrame，含 date/rs/rs_percentile 列
        """
        logger.info(f"计算RS分位数: {sector_code}, 窗口={window}")

        rs_df = self.calc_rs(sector_code, benchmark_code)
        if rs_df is None or rs_df.empty:
            return None

        if len(rs_df) < window:
            logger.warning(f"板块 {sector_code} 数据不足{window}条，无法计算分位数，共{len(rs_df)}条")
            # 使用较小窗口
            window = max(10, len(rs_df) // 2)

        rs_df["rs_percentile"] = self._calc_percentile(rs_df["rs"], window)
        logger.info(f"RS分位数计算完成: {sector_code}")
        return rs_df

    def calc_rs_momentum(
        self, sector_code: str, benchmark_code: str, lookback: int = 5
    ) -> Optional[pd.DataFrame]:
        """
        计算RS动量（方向指标）

        RS_动量 = 近lookback日RS的线性回归斜率
        用numpy.polyfit计算斜率

        参数:
            sector_code: 板块代码
            benchmark_code: 基准指数代码
            lookback: 回看天数（默认5天）

        返回:
            DataFrame，含 date/rs/rs_momentum 列
        """
        logger.info(f"计算RS动量: {sector_code}, lookback={lookback}")

        rs_df = self.calc_rs(sector_code, benchmark_code)
        if rs_df is None or rs_df.empty:
            return None

        if len(rs_df) < lookback:
            logger.warning(f"板块 {sector_code} 数据不足{lookback}条，无法计算动量")
            rs_df["rs_momentum"] = np.nan
            return rs_df

        # 滚动计算斜率
        rs_values = rs_df["rs"].values
        momentum = np.full(len(rs_values), np.nan)
        for i in range(lookback - 1, len(rs_values)):
            momentum[i] = self._calc_linear_slope(rs_values[i - lookback + 1 : i + 1])

        rs_df["rs_momentum"] = momentum
        logger.info(f"RS动量计算完成: {sector_code}")
        return rs_df

    def calc_rs_momentum_percentile(
        self, sector_code: str, benchmark_code: str, lookback: int = 5, window: int = 250
    ) -> Optional[pd.DataFrame]:
        """
        计算RS动量分位数（用于判断增强/减弱/走平）

        RS_动量分位 = 当前RS_动量在过去window日的百分位
        > 70% = 增强
        < 30% = 减弱
        30%~70% = 走平

        参数:
            sector_code: 板块代码
            benchmark_code: 基准指数代码
            lookback: 动量回看天数
            window: 分位数窗口

        返回:
            DataFrame，含 date/rs/rs_momentum/rs_momentum_percentile 列
        """
        logger.info(f"计算RS动量分位数: {sector_code}, lookback={lookback}, window={window}")

        rs_mom_df = self.calc_rs_momentum(sector_code, benchmark_code, lookback)
        if rs_mom_df is None or rs_mom_df.empty:
            return None

        # 计算动量的分位数
        valid_momentum = rs_mom_df["rs_momentum"].dropna()
        if len(valid_momentum) < max(window, 10):
            logger.warning(f"板块 {sector_code} 有效动量数据不足，无法计算动量分位数")
            rs_mom_df["rs_momentum_percentile"] = np.nan
            return rs_mom_df

        # 使用有效动量数据的最小长度作为实际窗口
        actual_window = min(window, len(valid_momentum))
        rs_mom_df["rs_momentum_percentile"] = self._calc_percentile(
            rs_mom_df["rs_momentum"], actual_window
        )

        logger.info(f"RS动量分位数计算完成: {sector_code}")
        return rs_mom_df

    def calc_rs_acceleration(
        self, sector_code: str, benchmark_code: str, lookback: int = 5
    ) -> Optional[pd.DataFrame]:
        """
        计算RS加速度（拐点指标）

        RS_加速度 = 近lookback日RS动量 - 前lookback日RS动量
        > 0 = 加速走强
        < 0 = 减速/见顶

        参数:
            sector_code: 板块代码
            benchmark_code: 基准指数代码
            lookback: 回看天数

        返回:
            DataFrame，含 date/rs/rs_momentum/rs_acceleration 列
        """
        logger.info(f"计算RS加速度: {sector_code}, lookback={lookback}")

        rs_mom_df = self.calc_rs_momentum(sector_code, benchmark_code, lookback)
        if rs_mom_df is None or rs_mom_df.empty:
            return None

        # 加速度 = 当前动量 - lookback日前动量
        rs_mom_df["rs_acceleration"] = rs_mom_df["rs_momentum"].diff(lookback)

        logger.info(f"RS加速度计算完成: {sector_code}")
        return rs_mom_df

    def calc_all_rs_indicators(
        self, sector_code: str, benchmark_code: str, window: int = 250, lookback: int = 5
    ) -> Optional[pd.DataFrame]:
        """
        一次性计算某板块的所有RS指标

        参数:
            sector_code: 板块代码
            benchmark_code: 基准指数代码
            window: 分位数窗口
            lookback: 动量回看天数

        返回:
            DataFrame: date, rs, rs_percentile, rs_momentum, rs_momentum_percentile, rs_acceleration
        """
        logger.info(f"批量计算所有RS指标: {sector_code}")

        # 从RS加速度开始（它内部会级联计算RS动量和RS）
        result = self.calc_rs_acceleration(sector_code, benchmark_code, lookback)
        if result is None or result.empty:
            return None

        # 补充RS分位数
        if len(result) >= window:
            result["rs_percentile"] = self._calc_percentile(result["rs"], window)
        else:
            result["rs_percentile"] = np.nan

        # 补充RS动量分位数
        valid_momentum = result["rs_momentum"].dropna()
        if len(valid_momentum) >= max(window, 10):
            actual_window = min(window, len(valid_momentum))
            result["rs_momentum_percentile"] = self._calc_percentile(
                result["rs_momentum"], actual_window
            )
        else:
            result["rs_momentum_percentile"] = np.nan

        # 选择最终输出列
        cols = ["date", "rs", "rs_percentile", "rs_momentum", "rs_momentum_percentile", "rs_acceleration"]
        result = result[[c for c in cols if c in result.columns]]

        logger.info(f"全部RS指标计算完成: {sector_code}, 共 {len(result)} 条")
        return result

    def calc_all_sectors_rs(
        self, window: int = 250, lookback: int = 5
    ) -> Dict[str, pd.DataFrame]:
        """
        批量计算所有板块的RS指标

        从SQLite读取板块-基准映射，逐个计算

        参数:
            window: 分位数窗口
            lookback: 动量回看天数

        返回:
            dict: {sector_code: DataFrame}
        """
        logger.info("开始批量计算所有板块RS指标...")

        # 获取板块-基准映射
        benchmark_map = self.sqlite_store.get_benchmark_map()
        if benchmark_map.empty:
            logger.error("未找到板块-基准映射数据")
            return {}

        results = {}
        success_count = 0
        fail_count = 0
        skip_count = 0

        for _, row in benchmark_map.iterrows():
            sector_code = row["sector_code"]
            benchmark_code = row["benchmark_code"]

            try:
                # 检查板块数据是否存在
                if not self.parquet_store.index_hist_exists(sector_code):
                    logger.debug(f"板块 {sector_code} 数据文件不存在，跳过")
                    skip_count += 1
                    continue

                # 检查基准数据是否存在（使用与 _load_benchmark_data 相同的格式转换）
                benchmark_df = self._load_benchmark_data(benchmark_code)
                if benchmark_df is None:
                    logger.debug(f"基准指数 {benchmark_code} 数据不存在，跳过板块 {sector_code}")
                    skip_count += 1
                    continue

                df = self.calc_all_rs_indicators(sector_code, benchmark_code, window, lookback)
                if df is not None and not df.empty:
                    results[sector_code] = df
                    success_count += 1
                else:
                    fail_count += 1

            except Exception as e:
                logger.error(f"计算板块 {sector_code} RS指标异常: {e}")
                fail_count += 1

        logger.info(
            f"批量RS计算完成: 成功={success_count}, 失败={fail_count}, 跳过={skip_count}"
        )
        return results

    def calc_cross_sectional_ranks(
        self, results: Dict[str, pd.DataFrame]
    ) -> Dict[str, pd.DataFrame]:
        """
        横截面排名（全市场）：把各板块「同一天」的 RS 动量 / RS 分位在全市场范围内排名。

        解决的问题：原 rs_momentum_percentile 是板块「自身过去 250 日」的时间序列百分位，
        板块跌了 250 天后小幅反弹，5 日 RS 斜率也会排到 90+ 分位，被误判为 ③加速冲顶。
        横截面排名改为「今天这个板块的 RS 动量在全市场里排第几」，
        反弹板块在弱市里只是全市场最弱的那个，排名低位 → 不会误判为领跑。

        参数:
            results: {sector_code: DataFrame}，需含 date / rs_momentum / rs_percentile 列

        返回:
            {sector_code: DataFrame[date, rs_momentum_cross_pct, rs_percentile_cross_pct]}
            rs_momentum_cross_pct: 当日 RS 动量在全市场的横截面百分位（0-100，越高越领先）
        """
        logger.info("计算 RS 横截面排名（全市场）...")

        def _build_panel(col: str) -> Optional[pd.DataFrame]:
            frames = []
            for code, df in results.items():
                if df is None or df.empty or col not in df.columns:
                    continue
                sub = df[["date", col]].dropna().rename(columns={col: code})
                frames.append(sub)
            if not frames:
                return None
            panel = frames[0]
            for f in frames[1:]:
                panel = panel.merge(f, on="date", how="outer")
            return panel.sort_values("date").reset_index(drop=True)

        panel_mom = _build_panel("rs_momentum")
        panel_pos = _build_panel("rs_percentile")

        # 横截面排名：仅对板块列排名，排除 date 列（否则会与 Timestamp 比较报错）
        ranked_mom = (
            panel_mom[[c for c in panel_mom.columns if c != "date"]].rank(axis=1, pct=True) * 100
            if panel_mom is not None else None
        )
        ranked_pos = (
            panel_pos[[c for c in panel_pos.columns if c != "date"]].rank(axis=1, pct=True) * 100
            if panel_pos is not None else None
        )

        out: Dict[str, pd.DataFrame] = {}
        for code, df in results.items():
            if df is None or df.empty:
                continue
            if panel_mom is not None and code in panel_mom.columns:
                dates = panel_mom["date"]
            elif panel_pos is not None and code in panel_pos.columns:
                dates = panel_pos["date"]
            else:
                continue
            row = {"date": dates}
            if ranked_mom is not None and code in ranked_mom.columns:
                row["rs_momentum_cross_pct"] = ranked_mom[code]
            if ranked_pos is not None and code in ranked_pos.columns:
                row["rs_percentile_cross_pct"] = ranked_pos[code]
            out[code] = pd.DataFrame(row)

        logger.info(f"RS 横截面排名计算完成: {len(out)} 个板块")
        return out

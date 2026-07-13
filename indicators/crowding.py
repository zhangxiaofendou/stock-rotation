"""
板块拥挤度计算模块
==================
衡量板块资金参与热度和交易拥挤程度。

核心指标：
  - 换手率分位：成交额的历史分位数
  - 成交量分位：成交量的历史分位数
  - 综合拥挤度评分：结合换手率和成交量的综合评分

判断标准：
  - 换手率分位 > 90% → 极度拥挤
  - 换手率分位 < 10% → 无人问津

注意：板块指数数据中有成交额(amount)和成交量(volume)，但没有市值数据。
当前使用成交额的历史分位数作为拥挤度核心指标。
"""

from typing import Optional, Dict
import numpy as np
import pandas as pd

from config.logger import get_logger

logger = get_logger(__name__)


class CrowdingIndicator:
    """板块拥挤度计算"""

    def __init__(self, parquet_store, sqlite_store):
        """
        初始化拥挤度计算器

        参数:
            parquet_store: ParquetStore 实例
            sqlite_store: SQLiteStore 实例
        """
        self.parquet_store = parquet_store
        self.sqlite_store = sqlite_store

    # ============================================================
    # 内部辅助方法
    # ============================================================
    def _load_and_standardize(self, sector_code: str) -> Optional[pd.DataFrame]:
        """
        加载板块指数数据并标准化列名
        """
        df = self.parquet_store.load_index_hist(sector_code)
        if df is None or df.empty:
            logger.warning(f"板块 {sector_code} 数据不存在或为空")
            return None

        col_map = {
            "日期": "date",
            "收盘": "close",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df

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
            if np.isnan(current):
                continue
            rank = (window_values < current).sum() + (window_values == current).sum() * 0.5
            result.iloc[i] = (rank / window) * 100
        return result

    # ============================================================
    # 核心指标计算
    # ============================================================
    def calc_turnover(self, sector_code: str) -> Optional[pd.DataFrame]:
        """
        计算板块成交额指标

        板块换手率 = 成交额 / 总自由流通市值
        由于缺少市值数据，此处返回成交额的滚动标准化值作为近似指标

        参数:
            sector_code: 板块代码

        返回:
            DataFrame，含 date/amount/turnover_proxy 列
        """
        logger.info(f"计算板块成交指标: {sector_code}")

        df = self._load_and_standardize(sector_code)
        if df is None:
            return None

        if "amount" not in df.columns:
            logger.warning(f"板块 {sector_code} 缺少成交额数据")
            return None

        result = df[["date", "amount"]].copy()

        # 使用成交额的20日滚动Z-score作为换手率近似
        # Z-score = (当前值 - 均值) / 标准差
        rolling_mean = result["amount"].rolling(window=20, min_periods=5).mean()
        rolling_std = result["amount"].rolling(window=20, min_periods=5).std()
        rolling_std = rolling_std.replace(0, np.nan)
        result["turnover_proxy"] = (result["amount"] - rolling_mean) / rolling_std

        logger.info(f"板块 {sector_code} 成交指标计算完成, 共 {len(result)} 条")
        return result

    def calc_turnover_percentile(
        self, sector_code: str, window: int = 250
    ) -> Optional[pd.DataFrame]:
        """
        计算换手率（成交额）分位数

        > 90% → 极度拥挤
        < 10% → 无人问津

        参数:
            sector_code: 板块代码
            window: 分位数窗口（默认250个交易日 ≈ 1年）

        返回:
            DataFrame，含 date/amount/amount_percentile 列
        """
        logger.info(f"计算换手率分位数: {sector_code}, 窗口={window}")

        df = self._load_and_standardize(sector_code)
        if df is None:
            return None

        if "amount" not in df.columns:
            logger.warning(f"板块 {sector_code} 缺少成交额数据")
            return None

        result = df[["date", "amount"]].copy()

        if len(result) < window:
            actual_window = max(10, len(result) // 2)
            logger.warning(f"板块 {sector_code} 数据不足{window}条，使用窗口={actual_window}")
        else:
            actual_window = window

        result["amount_percentile"] = self._calc_percentile(result["amount"], actual_window)

        logger.info(f"板块 {sector_code} 换手率分位数计算完成")
        return result

    def calc_crowding_score(self, sector_code: str) -> Optional[pd.DataFrame]:
        """
        综合拥挤度评分（0-100）

        结合成交额分位 + 成交量分位，取加权平均

        参数:
            sector_code: 板块代码

        返回:
            DataFrame，含 date/amount_percentile/volume_percentile/crowding_score 列
        """
        logger.info(f"计算综合拥挤度评分: {sector_code}")

        df = self._load_and_standardize(sector_code)
        if df is None:
            return None

        # 计算成交额分位数
        if "amount" in df.columns and len(df) >= 20:
            window = min(250, len(df))
            amount_pct = self._calc_percentile(df["amount"], window)
        else:
            amount_pct = pd.Series(np.nan, index=df.index)

        # 计算成交量分位数
        if "volume" in df.columns and len(df) >= 20:
            window = min(250, len(df))
            volume_pct = self._calc_percentile(df["volume"], window)
        else:
            volume_pct = pd.Series(np.nan, index=df.index)

        # 综合评分：成交额权重0.6，成交量权重0.4
        crowding_score = np.full(len(df), np.nan)
        for i in range(len(df)):
            vals = []
            weights = []
            if not np.isnan(amount_pct.iloc[i]):
                vals.append(amount_pct.iloc[i])
                weights.append(0.6)
            if not np.isnan(volume_pct.iloc[i]):
                vals.append(volume_pct.iloc[i])
                weights.append(0.4)
            if vals:
                # 加权平均
                total_weight = sum(weights)
                crowding_score[i] = sum(v * w / total_weight for v, w in zip(vals, weights))

        result = pd.DataFrame({
            "date": df["date"],
            "amount_percentile": amount_pct,
            "volume_percentile": volume_pct,
            "crowding_score": crowding_score,
        })

        logger.info(f"板块 {sector_code} 拥挤度评分计算完成, 共 {len(result)} 条")
        return result

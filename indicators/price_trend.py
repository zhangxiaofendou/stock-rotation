"""
绝对价格趋势判断模块
====================
基于均线系统判断板块的绝对价格趋势方向（上涨/横盘/下跌）。

判断逻辑：
  - 上涨：5日均线 > 20日均线 > 60日均线 且 20日均线斜率 > 0.2%/日
  - 下跌：5日均线 < 20日均线 < 60日均线 且 20日均线斜率 < -0.2%/日
  - 横盘：其他情况
"""

from typing import Optional, List
import numpy as np
import pandas as pd

from config.logger import get_logger

logger = get_logger(__name__)


class PriceTrend:
    """绝对价格趋势判断（均线法）"""

    def __init__(self, parquet_store, sqlite_store):
        """
        初始化价格趋势计算器

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

        参数:
            sector_code: 板块代码

        返回:
            DataFrame，列名为 date/close，或 None
        """
        df = self.parquet_store.load_index_hist(sector_code)
        if df is None or df.empty:
            logger.warning(f"板块 {sector_code} 数据不存在或为空")
            return None

        # 标准化列名
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

    # ============================================================
    # 均线计算
    # ============================================================
    def calc_ma(self, prices: pd.Series, periods: List[int] = None) -> pd.DataFrame:
        """
        计算多条移动平均线

        参数:
            prices: 价格序列
            periods: 均线周期列表，默认 [5, 20, 60]

        返回:
            DataFrame，每列为一个周期的均线
        """
        if periods is None:
            periods = [5, 20, 60]

        result = pd.DataFrame()
        for p in periods:
            col_name = f"ma{p}"
            result[col_name] = prices.rolling(window=p, min_periods=1).mean()

        return result

    def calc_ma_slope(self, ma_series: pd.Series, window: int = 5) -> pd.Series:
        """
        计算均线的滚动斜率（%/日）

        参数:
            ma_series: 均线序列
            window: 斜率计算窗口

        返回:
            斜率序列（%/日）
        """
        # 斜率 = (当前值 - window日前值) / window / 当前值 * 100
        slope = (ma_series - ma_series.shift(window)) / window / ma_series * 100
        return slope

    # ============================================================
    # 趋势判断
    # ============================================================
    def judge_trend(self, sector_code: str) -> Optional[str]:
        """
        判断板块当前绝对价格趋势

        返回:
            "上涨" / "横盘" / "下跌"，数据不足返回 None
        """
        logger.info(f"判断板块趋势: {sector_code}")

        df = self._load_and_standardize(sector_code)
        if df is None or len(df) < 60:
            logger.warning(f"板块 {sector_code} 数据不足60条，无法判断趋势")
            return None

        close = df["close"]
        mas = self.calc_ma(close, [5, 20, 60])

        # 取最新值
        ma5 = mas["ma5"].iloc[-1]
        ma20 = mas["ma20"].iloc[-1]
        ma60 = mas["ma60"].iloc[-1]

        # 计算20日均线斜率
        ma20_slope = self.calc_ma_slope(mas["ma20"], window=5)
        slope_val = ma20_slope.iloc[-1]

        # 判断逻辑
        is_bullish = ma5 > ma20 > ma60
        is_bearish = ma5 < ma20 < ma60
        slope_positive = slope_val is not None and not np.isnan(slope_val) and slope_val > 0.2
        slope_negative = slope_val is not None and not np.isnan(slope_val) and slope_val < -0.2

        if is_bullish and slope_positive:
            trend = "上涨"
        elif is_bearish and slope_negative:
            trend = "下跌"
        else:
            trend = "横盘"

        logger.info(
            f"板块 {sector_code} 趋势判断: {trend} "
            f"(MA5={ma5:.2f}, MA20={ma20:.2f}, MA60={ma60:.2f}, 斜率={slope_val:.4f}%/日)"
        )
        return trend

    def calc_trend_series(self, sector_code: str) -> Optional[pd.DataFrame]:
        """
        计算板块历史趋势序列（每天的趋势状态）

        返回:
            DataFrame，含 date/close/ma5/ma20/ma60/ma20_slope/trend 列
        """
        logger.info(f"计算板块历史趋势序列: {sector_code}")

        df = self._load_and_standardize(sector_code)
        if df is None:
            return None

        close = df["close"]
        dates = df["date"]

        # 计算均线
        mas = self.calc_ma(close, [5, 20, 60])
        ma20_slope = self.calc_ma_slope(mas["ma20"], window=5)

        # 逐日判断趋势
        trends = []
        for i in range(len(df)):
            if i < 60:
                trends.append("数据不足")
                continue

            ma5_val = mas["ma5"].iloc[i]
            ma20_val = mas["ma20"].iloc[i]
            ma60_val = mas["ma60"].iloc[i]
            slope_val = ma20_slope.iloc[i]

            if np.isnan(slope_val):
                trends.append("数据不足")
                continue

            if ma5_val > ma20_val > ma60_val and slope_val > 0.2:
                trends.append("上涨")
            elif ma5_val < ma20_val < ma60_val and slope_val < -0.2:
                trends.append("下跌")
            else:
                trends.append("横盘")

        result = pd.DataFrame({
            "date": dates,
            "close": close,
            "ma5": mas["ma5"],
            "ma20": mas["ma20"],
            "ma60": mas["ma60"],
            "ma20_slope": ma20_slope,
            "trend": trends,
        })

        logger.info(f"板块 {sector_code} 趋势序列计算完成, 共 {len(result)} 条")
        return result

"""
板块内分化度计算模块
====================
计算板块内成分股表现的离散程度，反映板块上涨的一致性。

分化度 = 板块内成分股涨幅的标准差
分化度越大 → 成分股走势越分化 → 板块一致性越差 → 可能接近退潮

当成分股数据不可用时，使用板块指数的日内波动率作为替代指标：
  替代分化度 = (最高 - 最低) / 收盘
"""

from typing import Optional, List
import numpy as np
import pandas as pd

from config.logger import get_logger

logger = get_logger(__name__)


class SectorDivergence:
    """板块内分化度计算"""

    def __init__(self, parquet_store, sqlite_store, data_source=None):
        """
        初始化分化度计算器

        参数:
            parquet_store: ParquetStore 实例
            sqlite_store: SQLiteStore 实例
            data_source: AkShareSource 实例（可选，用于实时获取成分股数据）
        """
        self.parquet_store = parquet_store
        self.sqlite_store = sqlite_store
        self.data_source = data_source

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

    def _get_component_stocks(self, sector_code: str) -> Optional[List[str]]:
        """
        获取板块成分股列表

        参数:
            sector_code: 板块代码

        返回:
            成分股代码列表，获取失败返回 None
        """
        # 先从 SQLite 获取
        stocks_df = self.sqlite_store.get_sector_stocks(sector_code)
        if not stocks_df.empty:
            return stocks_df["stock_code"].tolist()

        # 如果 SQLite 中没有，尝试从数据源实时获取
        if self.data_source is not None:
            logger.info(f"SQLite中无{sector_code}成分股数据，尝试从数据源获取...")
            try:
                comp_df = self.data_source.get_index_component(sector_code)
                if comp_df is not None and not comp_df.empty:
                    stock_col = "stock_code" if "stock_code" in comp_df.columns else comp_df.columns[0]
                    stocks = comp_df[stock_col].tolist()
                    logger.info(f"从数据源获取到 {sector_code} 成分股 {len(stocks)} 只")
                    return stocks
            except Exception as e:
                logger.warning(f"从数据源获取成分股失败 {sector_code}: {e}")

        return None

    def _get_stock_return(self, stock_code: str, date: pd.Timestamp) -> Optional[float]:
        """
        获取某只股票在某日的收益率

        参数:
            stock_code: 股票代码
            date: 日期

        返回:
            当日收益率（%），获取失败返回 None
        """
        stock_df = self.parquet_store.load_stock_hist(stock_code)
        if stock_df is None or stock_df.empty:
            return None

        # 标准化列名
        col_map = {
            "日期": "date",
            "收盘": "close",
            "开盘": "open",
            "涨跌幅": "pct_chg",
        }
        stock_df = stock_df.rename(columns={k: v for k, v in col_map.items() if k in stock_df.columns})
        stock_df["date"] = pd.to_datetime(stock_df["date"])

        # 查找当日数据
        target_date = pd.Timestamp(date)
        row = stock_df[stock_df["date"] == target_date]
        if row.empty:
            return None

        # 优先使用涨跌幅列
        if "pct_chg" in row.columns:
            val = row["pct_chg"].values[0]
            if not np.isnan(val):
                return float(val)

        # 用收盘价计算
        if "close" in row.columns:
            # 需要前一日收盘价
            prev_row = stock_df[stock_df["date"] < target_date].tail(1)
            if not prev_row.empty and "close" in prev_row.columns:
                today_close = row["close"].values[0]
                prev_close = prev_row["close"].values[0]
                if prev_close != 0:
                    return float((today_close / prev_close - 1) * 100)

        return None

    def _calc_intraday_volatility(self, sector_code: str, date: pd.Timestamp) -> Optional[float]:
        """
        计算板块指数的日内波动率（替代分化度指标）

        日内波动率 = (最高 - 最低) / 收盘

        参数:
            sector_code: 板块代码
            date: 日期

        返回:
            日内波动率值
        """
        df = self._load_and_standardize(sector_code)
        if df is None:
            return None

        target_date = pd.Timestamp(date)
        row = df[df["date"] == target_date]
        if row.empty:
            return None

        high = row["high"].values[0]
        low = row["low"].values[0]
        close = row["close"].values[0]

        if close == 0:
            return None

        return float((high - low) / close)

    # ============================================================
    # 核心指标计算
    # ============================================================
    def calc_divergence(self, sector_code: str, date: str) -> Optional[float]:
        """
        计算某日板块内个股涨幅的标准差

        参数:
            sector_code: 板块代码
            date: 日期，格式 "YYYY-MM-DD"

        返回:
            涨幅标准差值，数据不足返回 None
        """
        logger.info(f"计算分化度: {sector_code}, 日期={date}")

        target_date = pd.Timestamp(date)

        # 尝试获取成分股并计算涨幅标准差
        stocks = self._get_component_stocks(sector_code)
        if stocks is not None and len(stocks) > 0:
            returns = []
            for stock_code in stocks:
                ret = self._get_stock_return(stock_code, target_date)
                if ret is not None:
                    returns.append(ret)

            if len(returns) >= 3:
                divergence = float(np.std(returns))
                logger.info(f"板块 {sector_code} {date} 分化度(成分股法)={divergence:.4f}, 有效样本={len(returns)}")
                return divergence

        # 成分股数据不足，使用日内波动率作为替代
        logger.info(f"成分股数据不足，使用日内波动率替代: {sector_code}, {date}")
        volatility = self._calc_intraday_volatility(sector_code, target_date)
        if volatility is not None:
            logger.info(f"板块 {sector_code} {date} 分化度(波动率替代)={volatility:.4f}")
        return volatility

    def calc_divergence_series(
        self, sector_code: str, start_date: str = None, end_date: str = None
    ) -> Optional[pd.DataFrame]:
        """
        计算板块分化度时间序列

        参数:
            sector_code: 板块代码
            start_date: 起始日期，格式 "YYYY-MM-DD"
            end_date: 结束日期，格式 "YYYY-MM-DD"

        返回:
            DataFrame，含 date/divergence 列
        """
        logger.info(f"计算分化度时间序列: {sector_code}, {start_date} ~ {end_date}")

        df = self._load_and_standardize(sector_code)
        if df is None:
            return None

        # 过滤日期范围
        if start_date:
            df = df[df["date"] >= pd.Timestamp(start_date)]
        if end_date:
            df = df[df["date"] <= pd.Timestamp(end_date)]

        if df.empty:
            logger.warning(f"板块 {sector_code} 在指定日期范围内无数据")
            return None

        # 先尝试获取成分股
        stocks = self._get_component_stocks(sector_code)

        if stocks is not None and len(stocks) > 0:
            # 使用成分股法逐日计算
            divergence_values = []
            for _, row in df.iterrows():
                date = row["date"]
                returns = []
                for stock_code in stocks:
                    ret = self._get_stock_return(stock_code, date)
                    if ret is not None:
                        returns.append(ret)
                if len(returns) >= 3:
                    divergence_values.append(float(np.std(returns)))
                else:
                    divergence_values.append(np.nan)
        else:
            # 成分股数据不可用，使用日内波动率替代
            divergence_values = []
            for _, row in df.iterrows():
                high = row.get("high", np.nan)
                low = row.get("low", np.nan)
                close = row.get("close", np.nan)
                if not np.isnan(high) and not np.isnan(low) and not np.isnan(close) and close != 0:
                    divergence_values.append(float((high - low) / close))
                else:
                    divergence_values.append(np.nan)

        result = pd.DataFrame({
            "date": df["date"].values,
            "divergence": divergence_values,
        })

        # 对缺失值做前向填充
        result["divergence"] = result["divergence"].fillna(method="ffill")

        valid_count = result["divergence"].notna().sum()
        logger.info(f"板块 {sector_code} 分化度序��计算完成, 共 {len(result)} 条, 有效 {valid_count} 条")
        return result

    def detect_divergence_spike(
        self, sector_code: str, window: int = 20
    ) -> Optional[List[str]]:
        """
        检测分化度突变日期

        分化度突然放大 → 分歧出现 → 可能接近退潮
        检测条件：当前分化度 > 过去window日平均分化度 + 2倍标准差

        参数:
            sector_code: 板块代码
            window: 检测窗口

        返回:
            突变日期列表
        """
        logger.info(f"检测分化度突变: {sector_code}, 窗口={window}")

        div_df = self.calc_divergence_series(sector_code)
        if div_df is None or div_df.empty:
            return []

        # 计算滚动均值和标准差
        div_df["rolling_mean"] = div_df["divergence"].rolling(window=window, min_periods=window // 2).mean()
        div_df["rolling_std"] = div_df["divergence"].rolling(window=window, min_periods=window // 2).std()

        # 检测突变：当前值 > 均值 + 2*标准差
        div_df["is_spike"] = div_df["divergence"] > (div_df["rolling_mean"] + 2 * div_df["rolling_std"])

        spike_dates = div_df[div_df["is_spike"] == True]["date"].dt.strftime("%Y-%m-%d").tolist()

        logger.info(f"板块 {sector_code} 检测到 {len(spike_dates)} 个分化度突变日")
        return spike_dates

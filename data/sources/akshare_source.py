"""
AkShare 数据源封装模块
======================
封装 AkShare 各项数据接口，统一返回 pd.DataFrame。
包含重试机制和错误处理，所有接口失败返回 None。
"""

import time
import functools
from typing import Optional, Callable
import pandas as pd

from config.logger import get_logger
from config.settings import AKSHARE_RETRY_CONFIG
from .base import BaseDataSource

logger = get_logger(__name__)


# ============================================================
# 重试装饰器
# ============================================================
def retry_on_failure(
    max_retries: int = None,
    retry_interval: float = None,
    backoff_factor: float = None,
):
    """
    重试装饰器，支持指数退避

    参数:
        max_retries: 最大重试次数
        retry_interval: 初始重试间隔（秒）
        backoff_factor: 退避因子
    """
    max_retries = max_retries or AKSHARE_RETRY_CONFIG["max_retries"]
    retry_interval = retry_interval or AKSHARE_RETRY_CONFIG["retry_interval"]
    backoff_factor = backoff_factor or AKSHARE_RETRY_CONFIG["backoff_factor"]

    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            current_interval = retry_interval
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"[{func.__name__}] 第{attempt + 1}次尝试失败: {e}, "
                            f"{current_interval}秒后重试..."
                        )
                        time.sleep(current_interval)
                        current_interval *= backoff_factor
                    else:
                        logger.error(
                            f"[{func.__name__}] 重试{max_retries}次后仍然失败: {e}"
                        )
            return None
        return wrapper
    return decorator


# ============================================================
# AkShare 数据源实现
# ============================================================
class AkShareSource(BaseDataSource):
    """AkShare 数据源实现类"""

    def __init__(self):
        self._ensure_akshare()

    def _ensure_akshare(self):
        """确保 akshare 可用"""
        try:
            import akshare as ak
            self.ak = ak
            logger.info("AkShare 加载成功，版本: %s", ak.__version__)
        except ImportError:
            raise ImportError("请安装 akshare: pip install akshare")
        except Exception as e:
            logger.warning(f"AkShare 加载异常: {e}")

    # ============================================================
    # 申万行业分类
    # ============================================================
    @retry_on_failure()
    def get_sw_level1_info(self) -> Optional[pd.DataFrame]:
        """获取申万一级行业分类"""
        logger.info("获取申万一级行业分类...")
        df = self.ak.index_classification_sw()
        if df is not None and not df.empty:
            logger.info(f"获取到 {len(df)} 条申万一级行业数据")
        return df

    @retry_on_failure()
    def get_sw_level2_info(self) -> Optional[pd.DataFrame]:
        """获取申万二级行业分类"""
        logger.info("获取申万二级行业分类...")
        df = self.ak.index_classification_sw(level="二级")
        if df is not None and not df.empty:
            logger.info(f"获取到 {len(df)} 条申万二级行业数据")
        return df

    # ============================================================
    # 申万指数历史数据
    # ============================================================
    @retry_on_failure()
    def get_sw_index_hist(self, symbol: str, period: str = "day") -> Optional[pd.DataFrame]:
        """
        获取申万指数历史数据

        参数:
            symbol: 指数代码，如 "801010" 或 "801010.SI"（自动去掉.SI后缀）
            period: 周期，"day"/"week"/"month"（申万接口用day而非daily）

        返回:
            DataFrame，包含日期/开盘/收盘/最高/最低/成交量等
        """
        # 去掉.SI后缀，申万接口只接受纯数字代码
        if ".SI" in symbol:
            symbol = symbol.replace(".SI", "")
        logger.info(f"获取申万指数历史: {symbol}, 周期: {period}")
        df = self.ak.index_hist_sw(symbol=symbol, period=period)
        if df is not None and not df.empty:
            # 标准化列名
            df.columns = [col.lower() for col in df.columns]
            logger.info(f"获取到 {symbol} 历史数据 {len(df)} 条")
        return df

    # ============================================================
    # 申万成分股
    # ============================================================
    @retry_on_failure()
    def get_index_component(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        获取申万成分股

        参数:
            symbol: 指数代码，如 "801010.SI"

        返回:
            DataFrame，包含成分股代码/名称等。失败返回 None
        """
        logger.info(f"获取申万成分股: {symbol}")
        try:
            df = self.ak.index_component_sw(symbol=symbol)
            if df is not None and not df.empty:
                # 处理列名异常（可能出现中文或英文列名）
                col_map = {}
                for col in df.columns:
                    col_lower = str(col).lower().strip()
                    if col_lower in ["stock_code", "stock code", "代码", "股票代码"]:
                        col_map[col] = "stock_code"
                    elif col_lower in ["stock_name", "stock name", "名称", "股票名称"]:
                        col_map[col] = "stock_name"
                if col_map:
                    df = df.rename(columns=col_map)
                logger.info(f"获取到 {symbol} 成分股 {len(df)} 只")
            return df
        except Exception as e:
            logger.error(f"获取成分股失败 {symbol}: {e}")
            return None

    # ============================================================
    # 东方财富行业板块
    # ============================================================
    @retry_on_failure()
    def get_em_industry_list(self) -> Optional[pd.DataFrame]:
        """获取东方财富行业板块列表"""
        logger.info("获取东方财富行业板块列表...")
        df = self.ak.stock_board_industry_name_em()
        if df is not None and not df.empty:
            logger.info(f"获取到 {len(df)} 个行业板块")
        return df

    @retry_on_failure()
    def get_em_industry_hist(self, symbol: str, start: str = "20180101", end: str = "20500101") -> Optional[pd.DataFrame]:
        """
        获取东方财富行业历史行情

        参数:
            symbol: 板块代码，如 "BK0477"
            start: 起始日期，格式 "YYYYMMDD"
            end: 结束日期，格式 "YYYYMMDD"

        返回:
            DataFrame
        """
        logger.info(f"获取东方财富行业历史: {symbol}, {start} ~ {end}")
        df = self.ak.stock_board_industry_hist_em(
            symbol=symbol,
            start_date=start,
            end_date=end,
            adjust="",
        )
        if df is not None and not df.empty:
            logger.info(f"获取到 {symbol} 历史数据 {len(df)} 条")
        return df

    @retry_on_failure()
    def get_em_industry_cons(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        获取东方财富行业成分股

        参数:
            symbol: 板块代码，如 "BK0477"

        返回:
            DataFrame
        """
        logger.info(f"获取东方财富行业成分股: {symbol}")
        df = self.ak.stock_board_industry_cons_em(symbol=symbol)
        if df is not None and not df.empty:
            logger.info(f"获取到 {symbol} 成分股 {len(df)} 只")
        return df

    # ============================================================
    # 个股行情
    # ============================================================
    @retry_on_failure()
    def get_stock_hist(self, symbol: str, start: str, end: str, adjust: str = "qfq") -> Optional[pd.DataFrame]:
        """
        获取个股日线行情

        参数:
            symbol: 股票代码，如 "000001"
            start: 起始日期，格式 "YYYYMMDD"
            end: 结束日期，格式 "YYYYMMDD"
            adjust: 复权方式，qfq=前复权，hfq=后复权，""=不复权

        返回:
            DataFrame
        """
        logger.info(f"获取个股行情: {symbol}, {start} ~ {end}, 复权: {adjust}")
        df = self.ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start,
            end_date=end,
            adjust=adjust,
        )
        if df is not None and not df.empty:
            logger.info(f"获取到 {symbol} 行情 {len(df)} 条")
        return df

    # ============================================================
    # 资金流
    # ============================================================
    @retry_on_failure()
    def get_market_fund_flow(self) -> Optional[pd.DataFrame]:
        """获取市场资金流"""
        logger.info("获取市场资金流...")
        df = self.ak.stock_market_fund_flow()
        if df is not None and not df.empty:
            logger.info(f"获取到市场资金流 {len(df)} 条")
        return df

    @retry_on_failure()
    def get_concept_fund_flow(self) -> Optional[pd.DataFrame]:
        """获取概念资金流"""
        logger.info("获取概念资金流...")
        df = self.ak.stock_concept_fund_flow()
        if df is not None and not df.empty:
            logger.info(f"获取到概念资金流 {len(df)} 条")
        return df

    @retry_on_failure(max_retries=5, retry_interval=3)
    def get_sector_fund_flow_rank(self, indicator: str = "今日") -> Optional[pd.DataFrame]:
        """
        获取行业资金流排名（加强重试：5次）

        参数:
            indicator: 指标，如 "今日", "5日", "10日"

        返回:
            DataFrame
        """
        logger.info(f"获取行业资金流排名, 指标: {indicator}")
        df = self.ak.stock_sector_fund_flow_rank(indicator=indicator)
        if df is not None and not df.empty:
            logger.info(f"获取到行业资金流排名 {len(df)} 条")
        return df

    @retry_on_failure()
    def get_stock_individual_fund_flow(self, stock: str = "600000", market: str = "sh") -> Optional[pd.DataFrame]:
        """
        获取个股资金流

        参数:
            stock: 股票代码
            market: 市场，sh=上海，sz=深圳

        返回:
            DataFrame
        """
        logger.info(f"获取个股资金流: {stock}, 市场: {market}")
        df = self.ak.stock_individual_fund_flow(stock=stock, market=market)
        if df is not None and not df.empty:
            logger.info(f"获取到 {stock} 资金流 {len(df)} 条")
        return df

    # ============================================================
    # 北向资金
    # ============================================================
    @retry_on_failure()
    def get_north_fund_summary(self) -> Optional[pd.DataFrame]:
        """获取北向资金汇总"""
        logger.info("获取北向资金汇总...")
        df = self.ak.stock_hsgt_north_net_flow_in_em(symbol="北上")
        if df is not None and not df.empty:
            logger.info(f"获取到北向资金汇总 {len(df)} 条")
        return df

    @retry_on_failure()
    def get_north_hist(self, symbol: str = "沪股通") -> Optional[pd.DataFrame]:
        """
        获取北向历史数据

        参数:
            symbol: "沪股通" 或 "深股通" 或 "北上"

        返回:
            DataFrame
        """
        logger.info(f"获取北向历史数据: {symbol}")
        try:
            df = self.ak.stock_hsgt_hist_em(symbol=symbol)
            if df is not None and not df.empty:
                logger.info(f"获取到北向历史 {symbol} {len(df)} 条")
            return df
        except Exception as e:
            # 某些 symbol 参数可能不支持，尝试用 "北上"
            if symbol != "北上":
                logger.warning(f"获取北向历史 {symbol} 失败: {e}，尝试使用 '北上'")
                try:
                    df = self.ak.stock_hsgt_hist_em(symbol="北上")
                    if df is not None and not df.empty:
                        logger.info(f"获取到北向历史 北上 {len(df)} 条")
                    return df
                except Exception as e2:
                    logger.error(f"获取北向历史也失败: {e2}")
                    return None
            logger.error(f"获取北向历史失败: {e}")
            return None

    # ============================================================
    # 融资融券
    # ============================================================
    @retry_on_failure()
    def get_margin_detail_sse(self, date: str = "20240101") -> Optional[pd.DataFrame]:
        """
        获取上交所融资融券明细

        参数:
            date: 日期，格式 "YYYYMMDD"

        返回:
            DataFrame
        """
        logger.info(f"获取上交所融资融券: {date}")
        df = self.ak.stock_margin_detail_sse(date=date)
        if df is not None and not df.empty:
            logger.info(f"获取到上交所融资融券 {len(df)} 条")
        return df

    @retry_on_failure()
    def get_margin_detail_szse(self, date: str = "20240101") -> Optional[pd.DataFrame]:
        """
        获取深交所融资融券明细（需处理bytes返回）

        参数:
            date: 日期，格式 "YYYYMMDD"

        返回:
            DataFrame
        """
        logger.info(f"获取深交所融资融券: {date}")
        try:
            result = self.ak.stock_margin_detail_szse(date=date)
            # 处理可能的 bytes 返回
            if isinstance(result, bytes):
                from io import BytesIO
                df = pd.read_excel(BytesIO(result))
                logger.info(f"获取到深交所融资融券(从bytes解析) {len(df)} 条")
                return df
            elif isinstance(result, pd.DataFrame):
                logger.info(f"获取到深交所融资融券 {len(result)} 条")
                return result
            else:
                logger.warning(f"深交所融资融券返回未知类型: {type(result)}")
                return None
        except Exception as e:
            logger.error(f"获取深交所融资融券失败: {e}")
            return None

    # ============================================================
    # 基准指数
    # ============================================================
    @retry_on_failure()
    def get_benchmark_hist(self, symbol: str = "sh000300") -> Optional[pd.DataFrame]:
        """
        获取基准指数历史数据

        参数:
            symbol: 指数代码，需带交易所前缀，如 "sh000300"（沪深300）, "sh000905"（中证500）, "sh000852"（中证1000）

        返回:
            DataFrame
        """
        logger.info(f"获取基准指数历史: {symbol}")
        # stock_zh_index_daily 需要带 sh/sz 前缀
        df = self.ak.stock_zh_index_daily(symbol=symbol)
        if df is not None and not df.empty:
            logger.info(f"获取到基准指数 {symbol} 历史 {len(df)} 条")
        return df

    # ============================================================
    # 交易日历
    # ============================================================
    @retry_on_failure()
    def get_trade_calendar(self) -> Optional[pd.DataFrame]:
        """获取交易日历"""
        logger.info("获取交易日历...")
        df = self.ak.tool_trade_date_hist_sina()
        if df is not None and not df.empty:
            logger.info(f"获取到交易日历 {len(df)} 条")
        return df

    # ============================================================
    # 涨停板
    # ============================================================
    @retry_on_failure()
    def get_zt_pool(self, date: str = "20240101") -> Optional[pd.DataFrame]:
        """
        获取涨停板数据

        参数:
            date: 日期，格式 "YYYYMMDD"

        返回:
            DataFrame
        """
        logger.info(f"获取涨停板数据: {date}")
        df = self.ak.stock_zt_pool_em(date=date)
        if df is not None and not df.empty:
            logger.info(f"获取到涨停板数据 {len(df)} 条")
        return df

    # ============================================================
    # ETF
    # ============================================================
    @retry_on_failure()
    def get_etf_list(self) -> Optional[pd.DataFrame]:
        """获取ETF列表"""
        logger.info("获取ETF列表...")
        df = self.ak.fund_etf_category_sina(symbol="ETF基金")
        if df is not None and not df.empty:
            logger.info(f"获取到ETF列表 {len(df)} 条")
        return df

"""
数据源抽象基类
==============
定义统一的数据源接口，便于后续接入 Tushare 等其他数据源。
"""

from abc import ABC, abstractmethod
from typing import Optional
import pandas as pd


class BaseDataSource(ABC):
    """数据源抽象基类"""

    @abstractmethod
    def get_sw_level1_info(self) -> Optional[pd.DataFrame]:
        """获取申万一级行业分类"""
        pass

    @abstractmethod
    def get_sw_level2_info(self) -> Optional[pd.DataFrame]:
        """获取申万二级行业分类"""
        pass

    @abstractmethod
    def get_sw_index_hist(self, symbol: str, period: str = "daily") -> Optional[pd.DataFrame]:
        """获取申万指数历史数据"""
        pass

    @abstractmethod
    def get_index_component(self, symbol: str) -> Optional[pd.DataFrame]:
        """获取申万成分股"""
        pass

    @abstractmethod
    def get_em_industry_list(self) -> Optional[pd.DataFrame]:
        """获取东方财富行业板块列表"""
        pass

    @abstractmethod
    def get_em_industry_hist(self, symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
        """获取东方财富行业历史行情"""
        pass

    @abstractmethod
    def get_em_industry_cons(self, symbol: str) -> Optional[pd.DataFrame]:
        """获取东方财富行业成分股"""
        pass

    @abstractmethod
    def get_stock_hist(self, symbol: str, start: str, end: str, adjust: str = "qfq") -> Optional[pd.DataFrame]:
        """获取个股日线行情"""
        pass

    @abstractmethod
    def get_market_fund_flow(self) -> Optional[pd.DataFrame]:
        """获取市场资金流"""
        pass

    @abstractmethod
    def get_concept_fund_flow(self) -> Optional[pd.DataFrame]:
        """获取概念资金流"""
        pass

    @abstractmethod
    def get_sector_fund_flow_rank(self, indicator: str = "今日") -> Optional[pd.DataFrame]:
        """获取行业资金流排名"""
        pass

    @abstractmethod
    def get_stock_individual_fund_flow(self, stock: str, market: str = "sh") -> Optional[pd.DataFrame]:
        """获取个股资金流"""
        pass

    @abstractmethod
    def get_north_fund_summary(self) -> Optional[pd.DataFrame]:
        """获取北向资金汇总"""
        pass

    @abstractmethod
    def get_north_hist(self, symbol: str) -> Optional[pd.DataFrame]:
        """获取北向历史数据"""
        pass

    @abstractmethod
    def get_margin_detail_sse(self, date: str) -> Optional[pd.DataFrame]:
        """获取上交所融资融券明细"""
        pass

    @abstractmethod
    def get_margin_detail_szse(self, date: str) -> Optional[pd.DataFrame]:
        """获取深交所融资融券明细"""
        pass

    @abstractmethod
    def get_benchmark_hist(self, symbol: str) -> Optional[pd.DataFrame]:
        """获取基准指数历史数据"""
        pass

    @abstractmethod
    def get_trade_calendar(self) -> Optional[pd.DataFrame]:
        """获取交易日历"""
        pass

    @abstractmethod
    def get_zt_pool(self, date: str) -> Optional[pd.DataFrame]:
        """获取涨停板数据"""
        pass

    @abstractmethod
    def get_etf_list(self) -> Optional[pd.DataFrame]:
        """获取ETF列表"""
        pass

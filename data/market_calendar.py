"""
交易日历模块
============
从 AkShare 获取交易日历，提供交易日判断、前N个交易日获取等功能。
"""

from datetime import datetime, timedelta
from typing import Optional, List
import pandas as pd

from .sources.akshare_source import AkShareSource
from .storage.sqlite_store import SQLiteStore
from config.logger import get_logger

logger = get_logger(__name__)


class TradeCalendar:
    """交易日历管理"""

    def __init__(self, source: AkShareSource = None, store: SQLiteStore = None):
        self.source = source or AkShareSource()
        self.store = store or SQLiteStore()

    def fetch_and_store(self, start_year: int = 2000, end_year: int = 2030) -> bool:
        """
        从 AkShare 获取交易日历并存入 SQLite

        参数:
            start_year: 起始年份
            end_year: 结束年份

        返回:
            bool: 是否成功
        """
        try:
            logger.info(f"获取交易日历: {start_year} ~ {end_year}")
            df = self.source.get_trade_calendar()
            if df is None or df.empty:
                logger.error("获取交易日历失败，返回空数据")
                return False

            # 标准化列名和日期格式
            # 交易日历通常有 trade_date 列
            dates = []
            for _, row in df.iterrows():
                date_val = None
                for col in ["trade_date", "cal_date", "date", "日期"]:
                    if col in df.columns:
                        date_val = row[col]
                        break

                if date_val is not None:
                    try:
                        if isinstance(date_val, str):
                            # 处理各种日期格式
                            date_val = date_val.replace("-", "").replace("/", "")
                            dt = datetime.strptime(date_val[:8], "%Y%m%d")
                        elif hasattr(date_val, "strftime"):
                            dt = date_val
                        else:
                            dt = pd.Timestamp(date_val).to_pydatetime()

                        date_str = dt.strftime("%Y-%m-%d")
                        year = dt.year
                        if start_year <= year <= end_year:
                            weekday = dt.weekday()  # 0=周一
                            dates.append((date_str, 1, weekday))
                    except (ValueError, TypeError):
                        continue

            if dates:
                self.store.insert_trade_calendar_batch(dates)
                logger.info(f"交易日历入库完成，共 {len(dates)} 个交易日")
                return True
            else:
                logger.error("未能解析出有效交易日")
                return False

        except Exception as e:
            logger.error(f"获取交易日历失败: {e}")
            return False

    def is_trading_day(self, date: str = None) -> bool:
        """
        判断是否为交易日

        参数:
            date: 日期字符串，格式 "YYYY-MM-DD"，默认为今天

        返回:
            bool
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        return self.store.is_trading_day(date)

    def get_last_n_trading_days(self, n: int, end_date: str = None) -> List[str]:
        """
        获取前N个交易日

        参数:
            n: 数量
            end_date: 截止日期（含），默认今天

        返回:
            日期字符串列表
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        all_dates = self.store.get_trade_dates(end=end_date)
        if not all_dates:
            logger.warning("交易日历为空，请先执行 fetch_and_store()")
            return []

        # 取最后 N 个
        return all_dates[-n:] if len(all_dates) >= n else all_dates

    def get_trade_dates_between(self, start: str, end: str) -> List[str]:
        """
        获取日期区间内的所有交易日

        参数:
            start: 起始日期
            end: 结束日期

        返回:
            日期列表
        """
        return self.store.get_trade_dates(start=start, end=end)

    def next_trading_day(self, date: str = None, offset: int = 1) -> Optional[str]:
        """
        获取下一个交易日

        参数:
            date: 基准日期，默认今天
            offset: 偏移量，正数向后，负数向前

        返回:
            日期字符串
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        all_dates = self.store.get_trade_dates()
        if not all_dates:
            return None

        try:
            idx = all_dates.index(date)
            target_idx = idx + offset
            if 0 <= target_idx < len(all_dates):
                return all_dates[target_idx]
        except ValueError:
            # 日期不在日历中，查找最近的交易日
            for i, d in enumerate(all_dates):
                if d > date:
                    return all_dates[i + offset - 1] if offset > 0 else all_dates[max(0, i + offset)]
        return None

    def skip_non_trading_days(self, date: str) -> str:
        """
        跳过非交易日，返回最近的交易日

        参数:
            date: 目标日期

        返回:
            最近的交易日
        """
        if self.is_trading_day(date):
            return date

        # 向前查找最近的交易日
        dt = datetime.strptime(date, "%Y-%m-%d")
        for _ in range(30):  # 最多回溯30天
            dt -= timedelta(days=1)
            check_date = dt.strftime("%Y-%m-%d")
            if self.is_trading_day(check_date):
                return check_date
        return date  # 找不到则返回原日期

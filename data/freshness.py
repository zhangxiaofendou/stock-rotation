"""
数据新鲜度监控模块
==================
记录每次数据更新的时间戳，检查数据是否过期，生成数据新鲜度报告。
"""

from datetime import datetime, timedelta
from typing import Optional
import pandas as pd

from .storage.sqlite_store import SQLiteStore
from config.logger import get_logger
from config.settings import DATA_STALE_HOURS

logger = get_logger(__name__)


class DataFreshness:
    """数据新鲜度监控器"""

    def __init__(self, store: SQLiteStore = None):
        self.store = store or SQLiteStore()
        self.stale_hours = DATA_STALE_HOURS

    def record_update(self, data_type: str, data_key: str = None,
                      data_start: str = None, data_end: str = None,
                      record_count: int = None, status: str = "ok"):
        """
        记录数据更新时间

        参数:
            data_type: 数据类型，如 "sector_hist", "stock_hist", "calendar"
            data_key: 数据标识，如板块代码 "801010.SI"
            data_start: 数据起始日期
            data_end: 数据结束日期
            record_count: 记录数量
            status: 状态：ok, stale, error
        """
        self.store.update_freshness(
            data_type=data_type,
            data_key=data_key,
            data_start=data_start,
            data_end=data_end,
            record_count=record_count,
            status=status,
        )
        logger.debug(f"记录数据更新: {data_type}/{data_key}, 状态: {status}")

    def check_stale(self) -> pd.DataFrame:
        """
        检查过期数据

        返回:
            DataFrame，包含所有超过阈值的过期数据记录
        """
        stale = self.store.get_stale_data(stale_hours=self.stale_hours)
        if not stale.empty:
            logger.warning(f"发现 {len(stale)} 条过期数据（超过{self.stale_hours}小时）")
        return stale

    def is_stale(self, data_type: str, data_key: str = None) -> bool:
        """
        检查指定数据是否过期

        参数:
            data_type: 数据类型
            data_key: 数据标识

        返回:
            bool: True=已过期
        """
        df = self.store.get_freshness_report()
        if df.empty:
            return True  # 没有记录，认为过期

        mask = df["data_type"] == data_type
        if data_key is not None:
            mask &= df["data_key"] == data_key

        filtered = df[mask]
        if filtered.empty:
            return True

        # 检查最后更新时间
        last_update_str = filtered.iloc[0]["last_update"]
        if last_update_str is None:
            return True

        try:
            last_update = datetime.strptime(last_update_str, "%Y-%m-%d %H:%M:%S")
            threshold = datetime.now() - timedelta(hours=self.stale_hours)
            return last_update < threshold
        except (ValueError, TypeError):
            return True

    def generate_report(self) -> str:
        """
        生成数据新鲜度报告

        返回:
            报告文本
        """
        df = self.store.get_freshness_report()
        if df.empty:
            return "暂无数据新鲜度记录"

        now = datetime.now()
        lines = ["=" * 60, "数据新鲜度报告", f"生成时间: {now.strftime('%Y-%m-%d %H:%M:%S')}", "=" * 60]

        # 按数据类型分组
        for data_type in df["data_type"].unique():
            type_data = df[df["data_type"] == data_type]
            lines.append(f"\n## {data_type} ({len(type_data)} 条)")

            for _, row in type_data.iterrows():
                key = row.get("data_key", "") or "-"
                last_update = row.get("last_update", "未知")
                status = row.get("status", "未知")
                record_count = row.get("record_count", 0)
                data_end = row.get("data_end_date", "") or "-"

                # 计算距���时间
                try:
                    update_time = datetime.strptime(last_update, "%Y-%m-%d %H:%M:%S")
                    hours_ago = (now - update_time).total_seconds() / 3600
                    ago_str = f"{hours_ago:.1f}小时前"
                except (ValueError, TypeError):
                    ago_str = "未知"

                status_icon = "✓" if status == "ok" else "✗"
                lines.append(
                    f"  {status_icon} {key}: {record_count}条, "
                    f"截止{data_end}, 更新于{last_update} ({ago_str})"
                )

        # 过期数据汇总
        stale = self.check_stale()
        if not stale.empty:
            lines.append(f"\n⚠ 警告: 有 {len(stale)} 条数据已过期（超过{self.stale_hours}小时）")
            for _, row in stale.iterrows():
                lines.append(f"  - {row['data_type']}/{row.get('data_key', '-')}")

        lines.append("\n" + "=" * 60)
        return "\n".join(lines)

    def mark_stale(self, data_type: str, data_key: str = None):
        """标记数据为过期状态"""
        self.record_update(data_type, data_key, status="stale")

    def mark_error(self, data_type: str, data_key: str = None):
        """标记数据为错误状态"""
        self.record_update(data_type, data_key, status="error")

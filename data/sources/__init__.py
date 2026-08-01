# data.sources 包
#
# 数据源工厂：根据配置选择主数据源。默认 "eastmoney"（东方财富公开接口，
# 行情到最新交易日收盘、无需 token）；"akshare" 回退到原有 AkShare 实现。

from data.sources.base import BaseDataSource  # noqa: F401
from data.sources.akshare_source import AkShareSource  # noqa: F401
from data.sources.eastmoney_source import EastMoneyLiveSource  # noqa: F401

__all__ = ["BaseDataSource", "AkShareSource", "EastMoneyLiveSource", "get_data_source"]


def get_data_source(name: str = None) -> "BaseDataSource":
    """返回配置的主数据源实例。

    参数:
        name: 显式指定 "eastmoney" | "akshare"；为 None 时读 settings.PRIMARY_DATA_SOURCE。
    返回:
        BaseDataSource 实例（EastMoneyLiveSource 或 AkShareSource）。
    """
    from config.settings import PRIMARY_DATA_SOURCE
    name = (name or PRIMARY_DATA_SOURCE or "akshare").lower()
    if name == "eastmoney":
        return EastMoneyLiveSource()
    return AkShareSource()

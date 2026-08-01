"""AkShare 同花顺行业真实资金流。

同花顺实时接口（q.10jqka.com.cn）运行时被反爬（401），无法直接拿真实资金流；
腾讯 qt.gtimg.cn 不含个股资金流字段，无法聚合行业资金流。
故改用 AkShare 的 ``stock_board_industry_summary_ths()``（东财数据源，运行时可达）
获取同花顺行业的真实主力净流入，并通过行业名映射到 881xxx。

验证：返回 90 个同花顺行业，名称经 ``_THS_FALLBACK_MAP`` 反查 881xxx 覆盖率 90/90。
"""
import logging
from typing import Dict

import akshare as ak

logger = logging.getLogger(__name__)


def fetch_ths_industry_fund_flow() -> Dict[str, dict]:
    """返回 ``{881xxx代码: {"net_inflow", "pct", "up", "down"}}``，net_inflow 单位：亿元。"""
    from data.sources.ths_source import _THS_FALLBACK_MAP

    df = ak.stock_board_industry_summary_ths()
    name2code = {v: k for k, v in _THS_FALLBACK_MAP.items()}

    result: Dict[str, dict] = {}
    for _, row in df.iterrows():
        name = str(row.get("板块", "")).strip()
        code = name2code.get(name)
        if not code:
            continue
        try:
            result[code] = {
                "net_inflow": float(row.get("净流入", 0) or 0),
                "pct": float(row.get("涨跌幅", 0) or 0),
                "up": int(float(row.get("上涨家数", 0) or 0)),
                "down": int(float(row.get("下跌家数", 0) or 0)),
            }
        except (ValueError, TypeError):
            continue
    logger.info(
        f"AkShare 同花顺行业资金流获取 {len(result)}/{len(df)} 个板块（已映射到 881xxx）"
    )
    return result

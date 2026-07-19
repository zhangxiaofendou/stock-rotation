"""
AI 模块（P6）
===========
PRD §5.6.3 阶段 E / §5.6.5：AI 作为辅助确认，不替代主状态机。
- consensus：纯规则研报/新闻共识（板块详情「确认与风险因子」卡片）
- multi_factor / features：ML 多因子个股排序（个股下钻「排序辅助」）
- llm_client / report_extractor / report_dedup / sentiment：大模型增强（可选，缺配置降级）
- store / seed_data / ingest：结构化存储与示例/真实数据导入
"""

from ai.store import AIStore
from ai.consensus import compute_sector_consensus, compute_all_sector_consensus
from ai.multi_factor import rank_stocks

__all__ = [
    "AIStore",
    "compute_sector_consensus",
    "compute_all_sector_consensus",
    "rank_stocks",
]

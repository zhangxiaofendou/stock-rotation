"""
AI 模块数据导入
==============
- ensure_ai_seed()：数据库为空时写入示例研报/新闻（幂等，云端自初始化用）。
- import_research_csv()：从 CSV 导入真实研报数据（is_seed=0）。
- 真实爬虫接入后，应直接调用 ai.store.AIStore.upsert_research_reports。
"""

import os
import pandas as pd

from config.logger import get_logger
from config.sector_map import get_sector_name
from ai.store import AIStore
from ai.seed_data import get_seed_research_reports, get_seed_news

logger = get_logger(__name__)


def ensure_ai_seed() -> dict:
    """若研报表为空，则写入示例数据。返回加载情况。

    与 data/runtime_init.py 的 ensure_signal_performance 同一思路：云端
    db 为空时用已提交代码里的示例数据自初始化，保证「研报/新闻共识」卡片
    开箱即用；本地已有数据则跳过。
    """
    store = AIStore()
    existing = store.count_research_reports(include_seed=True)
    if existing > 0:
        logger.info(f"AI 研报数据已存在（{existing} 条），跳过示例初始化")
        return {"skipped": True, "research": 0, "news": 0}

    reports = get_seed_research_reports()
    news = get_seed_news()
    n_r = store.upsert_research_reports(reports)
    n_n = store.upsert_news(news)
    logger.info(f"AI 示例数据已写入：研报 {n_r} 条 / 新闻 {n_n} 条")
    return {"skipped": False, "research": n_r, "news": n_n}


def import_research_csv(csv_path: str) -> int:
    """从 CSV 导入真实研报（is_seed=0）。

    CSV 列：sector_code, broker, stock_code, stock_name, rating, prev_rating,
    target_price, prev_target_price, rating_change, coverage_date,
    core_view, risk_keywords, source_url
    target_change_pct 自动计算（若提供 prev_target_price）。
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)
    df = pd.read_csv(csv_path)
    required = {"sector_code", "broker", "rating", "coverage_date"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV 缺少必要列: {missing}")

    store = AIStore()
    rows = []
    for _, r in df.iterrows():
        tp = _num(r.get("target_price"))
        prev_tp = _num(r.get("prev_target_price"))
        tchg = None
        if tp is not None and prev_tp not in (None, 0):
            tchg = round((tp - prev_tp) / prev_tp * 100.0, 2)
        rows.append((
            str(r["sector_code"]).strip(),
            get_sector_name(str(r["sector_code"]).strip()),
            str(r["broker"]).strip(),
            _str(r.get("stock_code")),
            _str(r.get("stock_name")),
            _str(r.get("rating")),
            _str(r.get("prev_rating")),
            tp, prev_tp,
            _str(r.get("rating_change")),
            tchg,
            str(r["coverage_date"])[:10],
            _str(r.get("core_view")),
            _str(r.get("risk_keywords")),
            _str(r.get("source_url")),
            0,
        ))
    return store.upsert_research_reports(rows)


def _num(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() != "nan" else None


if __name__ == "__main__":
    print(ensure_ai_seed())

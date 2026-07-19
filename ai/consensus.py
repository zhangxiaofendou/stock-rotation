"""
研报 / 新闻共识（纯规则，无需大模型）
====================================
依据 PRD §7.4「研报共识变化信号（纯规则，无需大模型）」计算板块级共识：

  1. 评级上调潮      —— 多家券商同时上调某板块评级，⑥弱转强的领先信号
  2. 目标价上调幅度  —— 目标价上调幅度的中位数，幅度越大信号越强
  3. 覆盖券商数量变化—— 近期覆盖券商数相对前期的变化，飙升提示关注度升温
  4. 评级分歧度      —— 买入/增持 占近期评级的比例，一致看多=晚期、分化=早期

每条结论都附带可追溯的研报证据（券商 / 评级 / 日期 / 原文链接），绝不凭空生成。
新闻情绪作为辅助反向指标一并汇总。

注意：本模块只做「共识信号」，不改变九宫格状态、综合评分或操作建议（PRD §5.6.5）。
"""

from datetime import datetime, timedelta
from typing import Optional, List, Dict

import pandas as pd

from config.logger import get_logger
from config.sector_map import get_sector_name
from ai.store import AIStore

logger = get_logger(__name__)

BULLISH = {"买入", "增持"}
BEARISH = {"减持", "卖出"}
NEUTRAL = {"中性"}

# 信号阈值（集中配置，便于调参）
UPGRADE_WAVE_MIN = 2          # 上调 >= 此数视为「上调潮」
DOWNGRADE_WAVE_MIN = 2        # 下调 >= 此数视为「下调潮」
TARGET_UP_STRONG_PCT = 5.0    # 目标价上调幅度 > 此值视为强信号
COVERAGE_SURGE_MIN = 3        # 覆盖券商数净增 >= 此值视为「关注度飙升」


def _parse_date(s) -> Optional[datetime]:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def compute_sector_consensus(
    sector_code: str,
    store: Optional[AIStore] = None,
    recent_days: int = 30,
    prior_days: int = 30,
) -> Dict:
    """计算单个板块的研报/新闻共识。

    返回 dict 含各信号数值、可追溯证据列表与整体方向/强度。
    sector_code 无数据或非法时返回 has_data=False 的安全结构。
    """
    name = get_sector_name(sector_code) if sector_code else sector_code
    empty = {
        "sector_code": sector_code,
        "sector_name": name,
        "has_data": False,
        "as_of": datetime.now().strftime("%Y-%m-%d"),
    }
    if not sector_code:
        return empty

    store = store or AIStore()
    today = datetime.now()
    recent_start = today - timedelta(days=recent_days)
    prior_start = recent_start - timedelta(days=prior_days)

    reports = store.get_research_reports(sector_code=sector_code, include_seed=True, limit=500)
    if not reports:
        return empty

    recent, prior = [], []
    for r in reports:
        d = _parse_date(r.get("coverage_date"))
        if d is None:
            continue
        if d >= recent_start:
            recent.append(r)
        elif prior_start <= d < recent_start:
            prior.append(r)

    # ---- 信号1：评级上调潮 / 下调潮 ----
    upgrades = [r for r in recent if (r.get("rating_change") or "") == "上调"]
    downgrades = [r for r in recent if (r.get("rating_change") or "") == "下调"]
    upgrade_wave = len(upgrades) >= UPGRADE_WAVE_MIN
    downgrade_wave = len(downgrades) >= DOWNGRADE_WAVE_MIN

    # ---- 信号2：目标价上调幅度（中位数） ----
    target_ups = [
        r["target_change_pct"] for r in recent
        if r.get("target_change_pct") is not None and r["target_change_pct"] > 0
    ]
    target_up_median = float(pd.Series(target_ups).median()) if target_ups else None
    target_down_median = None
    target_downs = [
        r["target_change_pct"] for r in recent
        if r.get("target_change_pct") is not None and r["target_change_pct"] < 0
    ]
    if target_downs:
        target_down_median = float(pd.Series(target_downs).median())

    # ---- 信号3：覆盖券商数量变化 ----
    cov_recent = {r["broker"] for r in recent}
    cov_prior = {r["broker"] for r in prior}
    coverage_change = len(cov_recent) - len(cov_prior)
    coverage_surge = coverage_change >= COVERAGE_SURGE_MIN

    # ---- 信号4：评级分歧度 ----
    bull = sum(1 for r in recent if (r.get("rating") or "") in BULLISH)
    bear = sum(1 for r in recent if (r.get("rating") or "") in BEARISH)
    neut = sum(1 for r in recent if (r.get("rating") or "") in NEUTRAL)
    total = len(recent)
    buy_ratio = (bull / total) if total else None
    if buy_ratio is None:
        divergence = "无评级"
    elif buy_ratio >= 0.67:
        divergence = "一致看多"
    elif buy_ratio <= 0.33:
        divergence = "谨慎偏空"
    else:
        divergence = "观点分化"

    # ---- 综合方向与强度 ----
    strength = 0.5
    if upgrade_wave:
        strength += 0.20
    if downgrade_wave:
        strength -= 0.20
    if target_up_median is not None and target_up_median > TARGET_UP_STRONG_PCT:
        strength += 0.10
    if target_down_median is not None and target_down_median < -TARGET_UP_STRONG_PCT:
        strength -= 0.10
    if coverage_surge:
        strength += 0.10
    strength = max(0.0, min(1.0, strength))
    if strength > 0.60:
        direction = "看多"
    elif strength < 0.40:
        direction = "看空"
    else:
        direction = "中性"

    # ---- 新闻情绪（辅助反向指标） ----
    news = store.get_news(sector_code=sector_code, include_seed=True, limit=200)
    pos = sum(1 for n in news if (n.get("sentiment") or "") == "positive")
    neg = sum(1 for n in news if (n.get("sentiment") or "") == "negative")
    news_net = pos - neg

    # ---- 可追溯证据（近期研报） ----
    evidence = []
    for r in sorted(recent, key=lambda x: str(x.get("coverage_date", "")), reverse=True):
        evidence.append({
            "broker": r.get("broker"),
            "rating": r.get("rating"),
            "rating_change": r.get("rating_change"),
            "target_change_pct": r.get("target_change_pct"),
            "coverage_date": r.get("coverage_date"),
            "core_view": r.get("core_view"),
            "source_url": r.get("source_url"),
        })

    return {
        "sector_code": sector_code,
        "sector_name": name,
        "has_data": True,
        "as_of": today.strftime("%Y-%m-%d"),
        # 信号1
        "upgrade_count": len(upgrades),
        "downgrade_count": len(downgrades),
        "upgrade_wave": upgrade_wave,
        "downgrade_wave": downgrade_wave,
        # 信号2
        "target_up_median_pct": target_up_median,
        "target_down_median_pct": target_down_median,
        # 信号3
        "coverage_recent": len(cov_recent),
        "coverage_prior": len(cov_prior),
        "coverage_change": coverage_change,
        "coverage_surge": coverage_surge,
        # 信号4
        "buy_ratio": buy_ratio,
        "divergence": divergence,
        "bull_count": bull,
        "bear_count": bear,
        "neutral_count": neut,
        "report_count": total,
        # 综合
        "direction": direction,
        "strength": round(strength, 3),
        # 新闻
        "news_positive": pos,
        "news_negative": neg,
        "news_net": news_net,
        # 证据
        "evidence": evidence,
    }


def compute_all_sector_consensus(
    store: Optional[AIStore] = None,
    recent_days: int = 30,
) -> pd.DataFrame:
    """计算所有有研报数据的板块共识，按强度倒序返回 DataFrame。"""
    store = store or AIStore()
    # 取有研报的所有板块
    all_reports = store.get_research_reports(include_seed=True, limit=10000)
    if not all_reports:
        return pd.DataFrame()
    codes = []
    seen = set()
    for r in all_reports:
        c = r.get("sector_code")
        if c and c not in seen:
            seen.add(c)
            codes.append(c)

    rows = []
    for code in codes:
        rows.append(compute_sector_consensus(code, store=store, recent_days=recent_days))
    df = pd.DataFrame(rows)
    if not df.empty and "strength" in df.columns:
        df = df.sort_values("strength", ascending=False).reset_index(drop=True)
    return df

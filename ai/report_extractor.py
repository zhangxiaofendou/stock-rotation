"""
研报结构化提取（三层漏斗第一层）
================================
从研报原文抽取：评级 / 目标价 / 核心观点 / 风险关键词。
优先用大模型（仅高变化研报触发），否则正则兜底。无论哪条路径，
输出都可追溯到原文片段（core_view 保留原文摘要）。
"""

import re
from typing import Dict, Optional

from config.logger import get_logger
from ai.llm_client import chat, is_configured, extract_json_block

logger = get_logger(__name__)

_RATING_KW = ["买入", "增持", "中性", "减持", "卖出", "强烈推荐", "推荐", "审慎推荐"]
_TARGET_RE = re.compile(r"(?:目标价|目标价为|目标价至)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*元?", re.IGNORECASE)
_RISK_KW = ["风险", "警惕", "注意", "承压", "下行", "不及预期", "放缓", "竞争加剧", "波动"]


def extract_report(text: str, use_llm: bool = True) -> Dict:
    """抽取结构化研报字段。

    返回 {rating, target_price, core_view, risk_keywords(list), method}。
    method = 'llm' | 'regex'。
    """
    if use_llm and is_configured() and text and len(text) > 30:
        out = _extract_by_llm(text)
        if out is not None:
            out["method"] = "llm"
            return out

    return _extract_by_regex(text or "")


def _extract_by_llm(text: str) -> Optional[Dict]:
    prompt = (
        "请从以下研报文本中抽取结构化字段，只返回 JSON："
        "{\"rating\": 评级(买入/增持/中性/减持/卖出), "
        "\"target_price\": 目标价数字或null, "
        "\"core_view\": 一句话核心观点, "
        "\"risk_keywords\": [风险关键词数组]}。"
        "不要任何解释。\n\n" + text[:2000]
    )
    res = chat(prompt, system="你是专业的金融研报结构化助手。")
    if not res.get("text"):
        return None
    data = extract_json_block(res["text"])
    if not data:
        return None
    return {
        "rating": data.get("rating"),
        "target_price": _to_float(data.get("target_price")),
        "core_view": data.get("core_view"),
        "risk_keywords": list(data.get("risk_keywords") or []),
    }


def _extract_by_regex(text: str) -> Dict:
    rating = None
    for kw in _RATING_KW:
        if kw in text:
            rating = kw
            break
    m = _TARGET_RE.search(text)
    target = float(m.group(1)) if m else None
    # 核心观点：取首个句号前的长句，或整段截断
    core_view = text.strip().split("\n")[0][:120] if text.strip() else None
    risks = [kw for kw in _RISK_KW if kw in text]
    return {
        "rating": rating,
        "target_price": target,
        "core_view": core_view,
        "risk_keywords": risks,
        "method": "regex",
    }


def _to_float(v):
    try:
        if v is None:
            return None
        return float(str(v).replace("元", "").strip())
    except (TypeError, ValueError):
        return None

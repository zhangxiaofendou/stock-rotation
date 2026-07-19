"""
新闻归类与情绪分析（规则 + 可选大模型）
======================================
PRD §3.1 将「研报/新闻热度」列为情绪反向指标。本模块对新闻做：
  - 分类：政策 / 公司 / 行业 / 宏观 / 其他
  - 情绪：positive / negative / neutral
默认走关键词词典（零依赖、可解释、可追溯），若配置大模型则对模糊样本增强。

注意：新闻情绪是「反向指标」——过热的正面舆情往往对应板块阶段性高点，
仅作风险提示，不生成交易指令。
"""

from typing import Dict, List

from config.logger import get_logger
from ai.llm_client import chat, is_configured

logger = get_logger(__name__)

# 分类关键词（命中即归类，优先级：政策 > 公司 > 行业 > 宏观）
_CAT_KEYWORDS = {
    "政策": ["政策", "国务院", "央行", "证监会", "发改委", "财政部", "发布会", "规划", "指引"],
    "公司": ["公司", "财报", "业绩", "订单", "中标", "回购", "增持", "减持", "高管", "并购", "定增"],
    "行业": ["行业", "装机", "产量", "价格", "需求", "供给", "库存", "渗透率", "出口", "进口"],
    "宏观": ["CPI", "PPI", "GDP", "社融", "货币", "利率", "汇率", "美联储", "非农", "经济"],
}

_POS = ["增长", "超预期", "回暖", "复苏", "突破", "提速", "利好", "放量", "上调", "新高", "加速", "落地", "获批"]
_NEG = ["下滑", "不及预期", "承压", "下跌", "下调", "回落", "萎缩", "风险", "亏损", "退坡", "放缓", "禁令", "制裁"]


def _classify_category(text: str) -> str:
    for cat, kws in _CAT_KEYWORDS.items():
        if any(kw in text for kw in kws):
            return cat
    return "其他"


def _classify_sentiment(text: str) -> str:
    pos = sum(1 for kw in _POS if kw in text)
    neg = sum(1 for kw in _NEG if kw in text)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def classify_news(headline: str, use_llm: bool = True) -> Dict:
    """对单条新闻分类 + 情绪。

    返回 {category, sentiment, score, method}。score 为情绪强度（正负计数差）。
    """
    text = headline or ""
    category = _classify_category(text)
    sentiment = _classify_sentiment(text)
    pos = sum(1 for kw in _POS if kw in text)
    neg = sum(1 for kw in _NEG if kw in text)
    method = "keyword"

    # 模糊样本（无明确情绪词）且已配置大模型 → 增强
    if use_llm and is_configured() and sentiment == "neutral" and len(text) > 10:
        enhanced = _classify_by_llm(text)
        if enhanced:
            category = enhanced.get("category", category)
            sentiment = enhanced.get("sentiment", sentiment)
            method = "llm"

    return {
        "category": category,
        "sentiment": sentiment,
        "score": pos - neg,
        "method": method,
    }


def _classify_by_llm(text: str) -> Dict:
    prompt = (
        "对以下新闻分类并判断情绪，只返回 JSON："
        "{\"category\": 政策/公司/行业/宏观/其他, "
        "\"sentiment\": positive/negative/neutral}。\n\n" + text[:500]
    )
    res = chat(prompt, system="你是金融新闻分类与情绪分析助手。")
    if not res.get("text"):
        return {}
    from ai.llm_client import extract_json_block
    data = extract_json_block(res["text"])
    return data or {}

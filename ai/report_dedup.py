"""
研报去重（三层漏斗第二层）
========================
依据 PRD §7.4 去重规则：
  - 同板块 + 同券商 + 7天内        → 视为重复，丢弃
  - 同板块 + 不同券商 + 相似度>0.85 → 合并（标注「N篇一致」）
  - 评级变化 / 目标价变化>10% / 相似度<0.85 → 保留（新信息）

文本相似度默认用 difflib（标准库，零依赖）；若安装了 sentence-transformers
则自动改用 embedding cosine 相似度（更准）。
"""

import difflib
from datetime import datetime
from typing import List, Dict, Tuple

from config.logger import get_logger

logger = get_logger(__name__)

_EMBEDDER = None


def _get_embedder():
    """惰性加载 sentence-transformers（可选依赖）。"""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER if _EMBEDDER else None
    try:  # pragma: no cover - optional
        from sentence_transformers import SentenceTransformer
        _EMBEDDER = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    except Exception:
        _EMBEDDER = False
    return _EMBEDDER if _EMBEDDER else None


def similarity(a: str, b: str) -> float:
    """两段文本的语义/字面相似度（0-1）。"""
    a, b = (a or ""), (b or "")
    if not a or not b:
        return 0.0
    emb = _get_embedder()
    if emb is not None:  # pragma: no cover - optional
        try:
            va, vb = emb.encode([a, b])
            cos = (va @ vb) / (np_norm(va) * np_norm(vb))
            return float(cos)
        except Exception:
            pass
    return difflib.SequenceMatcher(None, a, b).ratio()


def np_norm(v):
    import numpy as np
    return float(np.linalg.norm(v))


def _parse_date(s) -> datetime:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def is_duplicate(rep_a: Dict, rep_b: Dict, threshold: float = 0.85) -> Tuple[bool, str]:
    """判断 rep_a 与 rep_b 是否重复/应合并，返回 (应丢弃a, 原因)。

    以 rep_a 为「待判定」，rep_b 为「已有」。仅当同板块、且属于
    「同券商7天内」或「不同券商高相似且无重大变化」时，a 被视为冗余。
    """
    if rep_a.get("sector_code") != rep_b.get("sector_code"):
        return False, "不同板块"

    da, db = _parse_date(rep_a.get("coverage_date")), _parse_date(rep_b.get("coverage_date"))
    same_broker = rep_a.get("broker") == rep_b.get("broker")

    # 同券商 7 天内 → 重复
    if same_broker and da and db and abs((da - db).days) <= 7:
        return True, "同券商7天内重复"

    # 变化显著 → 保留（新信息）
    rating_changed = rep_a.get("rating") != rep_b.get("rating")
    tp_a, tp_b = rep_a.get("target_price"), rep_b.get("target_price")
    target_changed = False
    if tp_a and tp_b:
        target_changed = abs(tp_a - tp_b) / tp_b > 0.10
    if rating_changed or target_changed:
        return False, "评级/目标价显著变化"

    # 不同券商高相似 → 合并（丢弃 a，归并到 b）
    sim = similarity(rep_a.get("core_view") or "", rep_b.get("core_view") or "")
    if sim >= threshold:
        return True, f"高相似({sim:.2f})合并"

    return False, "低相似保留"


def dedup_reports(reports: List[Dict], threshold: float = 0.85) -> Dict:
    """对研报列表去重，返回 {kept: [...], merged: {保留id: [合并券商]}}。

    输入 reports 元素需含 sector_code/broker/coverage_date/rating/
    target_price/core_view（与 ai.store 字段一致）。
    """
    kept: List[Dict] = []
    merged: Dict[int, List[str]] = {}
    for i, rep in enumerate(reports):
        duplicate = False
        for j, existing in enumerate(kept):
            drop, reason = is_duplicate(rep, existing, threshold)
            if drop:
                merged.setdefault(j, []).append(rep.get("broker"))
                duplicate = True
                logger.debug(f"研报去重: {rep.get('broker')} 归并到 {existing.get('broker')}（{reason}）")
                break
        if not duplicate:
            kept.append(rep)
    return {"kept": kept, "merged": merged}

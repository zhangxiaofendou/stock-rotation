# -*- coding: utf-8 -*-
"""离线优先的个股行业解析器。

彻底摆脱云端对东财/腾讯网络的依赖：
  1) config/stock_industry_em.json —— 本机一次性抓全 A 股 代码->东财行业名（随仓库发布）；
  2) config/em_to_sw.json —— 东财行业名->同花顺 881xxx（二级行业，advisor 需要它命中板块状态机）；
  3) 在线反查 lookup_stock_info 仅作兜底（云端偶发不通时可能失败，但离线已覆盖绝大多数）。

resolve_stock_industry(code) -> (sector_name, sector_code)，任一缺失返回 None。
"""
import os
import json
import logging

logger = logging.getLogger("stock_rotation")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EM_JSON = os.path.join(_ROOT, "config", "stock_industry_em.json")
EM_TO_SW = os.path.join(_ROOT, "config", "em_to_sw.json")

_em_cache: dict = None
_sw_bridge: dict = None


def _load_em() -> dict:
    global _em_cache
    if _em_cache is None:
        try:
            with open(EM_JSON, "r", encoding="utf-8") as f:
                _em_cache = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning("加载离线行业名包失败: %s", e)
            _em_cache = {}
    return _em_cache


def _load_bridge() -> dict:
    global _sw_bridge
    if _sw_bridge is None:
        try:
            with open(EM_TO_SW, "r", encoding="utf-8") as f:
                _sw_bridge = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning("加载行业名->881 桥接失败: %s", e)
            _sw_bridge = {}
    return _sw_bridge


def _em_name_to_sw(em_name: str):
    """东财行业名 -> 881xxx（桥接优先，resolve_sector 模糊兜底）。"""
    if not em_name:
        return None
    bridge = _load_bridge()
    code = bridge.get(str(em_name).strip())
    if code:
        return code
    try:
        from portfolio.stock_lookup import resolve_sector
        code = resolve_sector(em_name)[0]
        if code:
            return code
    except Exception:  # noqa: BLE001
        pass
    return None


def resolve_stock_industry(code: str, online_fallback: bool = True):
    """离线优先解析 (sector_name, sector_code)。

    返回 (sector_name, sector_code)；任一缺失则为 None（不抛异常）。
    sector_name 取自离线包（东财行业名），sector_code 经桥接/模糊映射到 881xxx。
    online_fallback=True 时，离线缺失再尝试在线反查（云端可能不通）。
    """
    code = str(code or "").strip()
    if not code:
        return (None, None)
    em = _load_em().get(code)
    name = em
    code881 = _em_name_to_sw(name) if name else None

    if online_fallback and (not name or not code881):
        try:
            from portfolio.stock_lookup import lookup_stock_info
            info = lookup_stock_info(code)
            oname = (info or {}).get("sector_name") if info else None
            if oname and not name:
                name = oname
                if not code881:
                    code881 = _em_name_to_sw(name)
        except Exception:  # noqa: BLE001
            pass

    if name and not code881:
        code881 = _em_name_to_sw(name)
    return (name, code881)


def resolve_stock_industry_batch(codes: list, online_fallback: bool = False):
    """批量解析，返回 {code: (name, code881)}。默认不触发在线（批量场景离线即可）。"""
    out = {}
    for c in codes:
        try:
            out[str(c)] = resolve_stock_industry(str(c), online_fallback=online_fallback)
        except Exception:  # noqa: BLE001
            out[str(c)] = (None, None)
    return out

"""持仓录入「公共信息自动补全」：输入 6 位证券代码即返回名称/最新价/行业。

设计目标：让录入人只填最少字段。凡属于公共行情信息（证券名称、最新价、
所属行业）都由本模块从公开接口自动带出，录入人无需记忆或手抄。

数据源：
- 主源：东方财富 push2 实时快照（一次拿到 名称 + 最新价 + 行业名）
- 兜底：腾讯 qt.gtimg.cn（名称 + 最新价，无行业）

容错约定：
- 网络失败 / 解析失败 / 代码非法 → 返回 None（调用方决定降级为手动填写），
  绝不抛异常，绝不阻塞录入流程。
- 进程级缓存（TTL 3600s）：同一代码短时间内不重复请求。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_TTL = 3600.0  # 秒
_EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

# 进程级缓存：{code: (expire_ts, info_dict | None)}
_cache: dict[str, tuple[float, Optional[dict]]] = {}

# ETF主题到项目板块名称的明确映射。只配置已确认的产品，避免从名称猜行业。
_ETF_SECTOR_MAP = {
    "159766": "旅游",
}


def is_etf_code(code: str) -> bool:
    code = normalize_code(code)
    return bool(code and code.startswith(("15", "16", "50", "51", "56", "58")))


# ============================================================
# 代码规范化
# ============================================================
def normalize_code(code: str) -> Optional[str]:
    """把用户输入的代码规整为 6 位数字；非法返回 None。

    支持：沪市 60xxxx/688xxx、深市 00xxxx/30xxxx、北交所 8xxxxx/4xxxxx。
    """
    if not code:
        return None
    digits = "".join(ch for ch in str(code).strip() if ch.isdigit())
    if len(digits) != 6:
        return None
    return digits


def market_prefix(code: str) -> Optional[str]:
    """返回腾讯行情前缀 sh/sz/bj；无法识别返回 None。"""
    code = normalize_code(code)
    if not code:
        return None
    if code[0] in ("6", "9"):
        return "sh"
    if code[0] in ("0", "3"):
        return "sz"
    if code[0] in ("4", "8"):
        return "bj"
    if code[0] == "5":  # 沪市基金/ETF
        return "sh"
    if code[0] == "1":  # 深市基金/ETF
        return "sz"
    return None


def eastmoney_secid(code: str) -> Optional[str]:
    """转为东财 secid（1.600519 / 0.000001 / 0.8xxxxx）；无法识别返回 None。"""
    code = normalize_code(code)
    if not code:
        return None
    if code[0] in ("6", "9", "5"):
        return f"1.{code}"
    if code[0] in ("0", "3", "4", "8", "1"):
        return f"0.{code}"
    return None


# ============================================================
# 行情获取
# ============================================================
def _fetch_eastmoney(code: str) -> Optional[dict]:
    """东财实时快照 → {name, price, sector_name}；失败返回 None。"""
    secid = eastmoney_secid(code)
    if not secid:
        return None
    url = (
        f"{_EASTMONEY_QUOTE_URL}?secid={secid}"
        f"&fields=f43,f57,f58,f127,f128,f169,f170"
    )
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        d = json.loads(raw)
        data = d.get("data")
        if not data:
            return None
        name = str(data.get("f58") or "").strip()
        price_raw = data.get("f43")
        sector = str(data.get("f127") or "").strip()
        if not name or price_raw is None:
            return None
        # 东财 f43 为 ×100 整数（如 135060=1350.60）；个别场景返回已除 100 的浮点，按类型区分
        price = float(price_raw) / 100.0 if isinstance(price_raw, int) else float(price_raw)
        info = {
            "name": name,
            "price": round(price, 4),
            "sector_name": sector or _ETF_SECTOR_MAP.get(code),
            "asset_type": "etf" if is_etf_code(code) else "stock",
        }
        logger.info("东财补全 %s → %s", code, info)
        return info
    except Exception as e:  # noqa: BLE001
        logger.warning("东财实时快照失败(%s): %s", code, e)
        return None


def _fetch_tencent(code: str) -> Optional[dict]:
    """腾讯实时行情兜底 → {name, price, sector_name: None}；失败返回 None。"""
    prefix = market_prefix(code)
    if not prefix:
        return None
    url = _TENCENT_QUOTE_URL + f"{prefix}{code}"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("gbk", "ignore")
        for line in raw.strip().split(";"):
            if "=" not in line:
                continue
            head, _, body = line.partition("=")
            if not body.startswith('"'):
                continue
            f = body[1:-1].split("~")
            # [1]=名称 [3]=当前价
            if len(f) < 4 or not f[1] or not f[3]:
                continue
            info = {
                "name": f[1].strip(),
                "price": round(float(f[3]), 4),
                "sector_name": _ETF_SECTOR_MAP.get(code),
                "asset_type": "etf" if is_etf_code(code) else "stock",
            }
            logger.info("腾讯补全 %s → %s", code, info)
            return info
    except Exception as e:  # noqa: BLE001
        logger.warning("腾讯实时行情失败(%s): %s", code, e)
    return None


# ============================================================
# 对外入口
# ============================================================
def lookup_stock_info(code: str) -> Optional[dict]:
    """输入 6 位证券代码 → {name, price, sector_name}；任何失败返回 None。

    内部自动：代码规范化 → 东财主源 → 腾讯兜底 → 进程级缓存。
    不抛异常；网络不可用时由调用方降级为手动填写。
    """
    code = normalize_code(code)
    if not code:
        return None
    now = time.time()
    cached = _cache.get(code)
    if cached and cached[0] > now:
        return cached[1]

    info = _fetch_eastmoney(code) or _fetch_tencent(code)
    _cache[code] = (now + _CACHE_TTL, info)
    return info


def clear_cache() -> None:
    """清空进程级缓存（测试用）。"""
    _cache.clear()


def resolve_sector(sector_name: Optional[str]):
    """行业名（可能是简称/东财行业名，如「旅游」）→ (sector_code, norm_name, group)。

    把行情带出的行业名自动关联到同花顺 881xxx 行业代码，使持仓分析能按代码命中
    板块状态机，而不是只有一句简称（如「旅游」）却匹配不到任何板块。

    匹配优先级：
      1) 精确匹配同花顺行业全名（如「旅游及酒店」）；
      2) 双向包含匹配（「旅游」⊂「旅游及酒店」）；
      3) 至少用 classify_em_group 给出板块组。
    失败/无匹配返回 (None, 原名, "其他")。
    """
    if not sector_name:
        return (None, "", "其他")
    sn = str(sector_name).strip()
    try:
        from config.sector_map import SW_LEVEL2_MAP, classify_em_group
    except Exception:
        return (None, sn, "其他")
    # 归一：去掉常见连接/后缀词，提升简称命中率（「旅游酒店」→「旅游」）
    _stop = ["及", "酒店", "行业", "产业", "板块", "概念", "指数", "Ⅱ", "I",
             "（", "）", "(", ")", "etf", "ETF", " "]

    def _core(s: str) -> str:
        s = str(s).strip()
        for w in _stop:
            s = s.replace(w, "")
        return s

    csn = _core(sn)
    # 1) 精确匹配
    for code, (name, grp, _) in SW_LEVEL2_MAP.items():
        if name == sn:
            return (code, name, grp)
    # 2) 双向包含（含去后缀核心词匹配，简称/变体也能命中全名）
    for code, (name, grp, _) in SW_LEVEL2_MAP.items():
        cn = _core(name)
        if (csn and csn in cn) or (cn and cn in csn) or (sn in name) or (name in sn):
            return (code, name, grp)
    # 3) 至少给板块组
    return (None, sn, classify_em_group(sn))

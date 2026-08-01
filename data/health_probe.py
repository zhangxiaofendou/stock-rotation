"""
数据源连通性自检
================
解释「手动刷新后行业数据仍卡在 7.30」的根因：当前主源为同花顺，探测其
行业清单 / 行业 K线 / 实时行情接口的可达性，并抽样一个行业板块（半导体 881121）
返回其最新数据日期。

保留东方财富探针（probe_eastmoney / verdict）供对比/回退参考，但看板侧栏
「数据源诊断」默认调用同花顺探针（probe_ths / verdict_ths）。

所有探测均短超时，失败不抛异常。
"""

import json
import re
import urllib.request
import logging

from config.logger import get_logger

logger = get_logger(__name__)

_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://quote.eastmoney.com/",
}

_KLINE_FIELDS_Q = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def _probe(url: str, timeout: int = 6) -> dict:
    """返回 {'ok': bool, 'raw': str|None, 'err': str|None}。"""
    try:
        req = urllib.request.Request(url, headers=_EM_HEADERS)
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        return {"ok": True, "raw": raw, "err": None}
    except Exception as e:  # noqa: BLE001 - 探针必须吞掉所有网络异常
        return {"ok": False, "raw": None, "err": f"{type(e).__name__}: {e}"}


def _last_kline_date(raw: str) -> str:
    """从 K 线响应里取最后一根 K 线的日期。"""
    try:
        d = json.loads(raw)
        klines = d.get("data", {}).get("klines") or []
        if klines:
            return klines[-1].split(",")[0]
    except Exception:
        pass
    return ""


def probe_eastmoney() -> dict:
    """探测东财各接口可达性，返回结构化结果。"""
    out = {
        "clist_ok": False,
        "clist_sample": "",
        "kline_push2_ok": False,
        "kline_push2_date": "",
        "kline_push2his_ok": False,
        "kline_push2his_date": "",
        "quote_ok": False,
    }

    # 1) 行业清单 clist（push2 主站）
    r = _probe(
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=3&fs=m:90+t:2&fields=f12,f13,f14&fid=f3&np=1"
    )
    if r["ok"]:
        try:
            d = json.loads(r["raw"])
            diff = d.get("data", {}).get("diff") or []
            if diff:
                out["clist_ok"] = True
                out["clist_sample"] = diff[0].get("f14", "")
        except Exception:
            pass

    # 2) 行业 K 线 —— push2 主站（与 clist 同域名，云端通常可达）
    r = _probe(
        "https://push2.eastmoney.com/api/qt/stock/kline/get"
        f"?secid=90.BK0477&fields1=f1,f2,f3,f4,f5,f6&fields2={_KLINE_FIELDS_Q}"
        f"&klt=101&fqt=1&beg=20260801&end=20500101"
    )
    if r["ok"]:
        dt = _last_kline_date(r["raw"])
        if dt:
            out["kline_push2_ok"] = True
            out["kline_push2_date"] = dt

    # 3) 行业 K 线 —— push2his（旧主源，部分网络环境下被掐）
    r = _probe(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid=90.BK0477&fields1=f1,f2,f3,f4,f5,f6&fields2={_KLINE_FIELDS_Q}"
        f"&klt=101&fqt=1&beg=20260801&end=20500101"
    )
    if r["ok"]:
        dt = _last_kline_date(r["raw"])
        if dt:
            out["kline_push2his_ok"] = True
            out["kline_push2his_date"] = dt

    # 4) 实时快照 push2（行情条用）
    r = _probe(
        "https://push2.eastmoney.com/api/qt/stock/get"
        "?secid=1.600519&fields=f43,f57,f58,f169,f170,f86"
    )
    if r["ok"]:
        try:
            d = json.loads(r["raw"])
            if d.get("data"):
                out["quote_ok"] = True
        except Exception:
            pass

    return out


def verdict(probe: dict) -> str:
    """根据探针结果给出人话结论。"""
    if probe["kline_push2_ok"] or probe["kline_push2his_ok"]:
        dt = probe["kline_push2_date"] or probe["kline_push2his_date"]
        if dt and dt > "2026-07-30":
            return f"✅ 东财行业 K 线可达，抽样板块最新 {dt}，数据应已到最新交易日"
        return f"⚠️ 东财 K 线可达但抽样最新仅 {dt}，仍偏旧（可能部分板块滞后）"
    if probe["clist_ok"]:
        return "❌ 东财行业 K 线不可达（clist 通但 K 线被掐），将回退 AkShare → 数据滞后至 7.30"
    return "❌ 东财整体不可达（clist/K线均失败），将回退 AkShare → 数据滞后至 7.30"


# ============================================================
# 同花顺探针（ths 主源）
# ============================================================
_THS_PROBE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://q.10jqka.com.cn/",
}


def _probe_ths(url: str, timeout: int = 6) -> dict:
    try:
        req = urllib.request.Request(url, headers=_THS_PROBE_HEADERS)
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        return {"ok": True, "raw": raw, "err": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "raw": None, "err": f"{type(e).__name__}: {e}"}


def _ths_last_kline_date(raw: str) -> str:
    """从同花顺 K 线 JSONP 响应里取最后一根 K 线的日期(YYYYMMDD)。"""
    try:
        s = raw.index("(")
        e = raw.rindex(")")
        obj = json.loads(raw[s + 1:e])
        data = obj.get("data") or ""
        if data:
            last = data.split(";")[-1]
            if last:
                return last.split(",")[0]
    except Exception:
        pass
    return ""


def probe_ths() -> dict:
    """探测同花顺各接口可达性，返回结构化结果。"""
    out = {
        "list_ok": False,
        "list_count": 0,
        "kline_ok": False,
        "kline_date": "",
        "realtime_ok": False,
        "sample_name": "",
    }

    # 1) 行业清单（thshy 行业列表，无需登录）
    r = _probe_ths(
        "https://q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/1/ajax/1/"
    )
    if r["ok"]:
        found = re.findall(r'thshy/detail/code/(\d+)/">([^<]+)</a>', r["raw"])
        if found:
            out["list_ok"] = True
            out["list_count"] = len(found)
            out["sample_name"] = found[0][1]

    # 2) 行业 K 线（半导体 881121，最新交易日）
    r = _probe_ths("https://d.10jqka.com.cn/v6/line/bk_881121/01/last20.js")
    if r["ok"]:
        dt = _ths_last_kline_date(r["raw"])
        if dt:
            out["kline_ok"] = True
            out["kline_date"] = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"

    # 3) 实时行情（同行业列表接口，已含各行业涨跌幅列）
    r = _probe_ths(
        "https://q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/1/ajax/1/"
    )
    if r["ok"] and re.search(
        r'thshy/detail/code/\d+/">[^<]+</a>\s*</td>\s*<td[^>]*>[\d.\-]+</td>', r["raw"]
    ):
        out["realtime_ok"] = True

    return out


def verdict_ths(probe: dict) -> str:
    """根据同花顺探针结果给出人话结论。"""
    if probe["kline_ok"]:
        dt = probe["kline_date"]
        if dt and dt > "2026-07-30":
            return (f"✅ 同花顺行业K线可达，抽样(半导体881121)最新 {dt}，"
                    f"数据应已到最新交易日")
        return f"⚠️ 同花顺K线可达但抽样最新仅 {dt}，仍偏旧（可能部分板块滞后）"
    if probe["list_ok"]:
        return "❌ 同花顺行业清单可达但K线失败，将回退 AkShare → 数据可能滞后"
    return "❌ 同花顺整体不可达（清单/K线均失败），将回退 AkShare → 数据滞后"

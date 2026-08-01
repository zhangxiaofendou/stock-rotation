"""
数据源连通性自检
================
解释「手动刷新后行业数据仍卡在 7.30」的根因：分别探测东方财富
clist(行业清单) / push2 K线 / push2his K线 / push2 实时 四个接口的可达性，
并抽样一个行业板块(银行 BK0477)返回其最新数据日期。

看板侧栏「数据源诊断」调用本模块，让用户在云端一眼看到哪个接口被网络掐断、
行业数据为何滞后。所有探测均短超时，失败不抛异常。
"""

import json
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

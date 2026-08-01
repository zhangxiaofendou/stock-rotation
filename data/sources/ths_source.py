"""
同花顺实时/历史数据源（THSDataSource）
=====================================
基于同花顺公开行情接口，无需 token：
  - 行业板块 K 线（主）：d.10jqka.com.cn/v6/line/bk_881xxx/01/last800.js
  - 行业板块清单 + 实时涨跌：q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/{p}/ajax/1/
  - 个股 K 线：d.10jqka.com.cn/v6/line/hs_600519/01/last800.js

定位：
  作为「数据到最新交易日」的主数据源。同花顺行业板块 K 线收盘后即更新
  （实测已含 2026-07-31），解决 AkShare / 申万上游停滞 7.30 的痛点。

覆盖策略（继承 BaseDataSource，实现全部 20 个抽象方法）：
  - 行业板块 K 线 / 清单 / 实时涨跌 / 个股 K 线：使用同花顺接口（数据到当日收盘）。
    其中行业板块代码采用同花顺行业指数体系（881xxx）。
  - 基准 / 成分股 / 资金流 / 北向 / 融资融券 / 涨停 / ETF / 交易日历等同花顺免费接口
    不便稳定覆盖的：惰性委托 AkShareSource，保证功能完整不丢失。
"""

import re
import time
import json
import urllib.request
from typing import Optional
import pandas as pd

from config.logger import get_logger
from config.settings import AKSHARE_RETRY_CONFIG
from .base import BaseDataSource

logger = get_logger(__name__)

# 与 AkShare stock_board_industry_hist_em / stock_zh_a_hist 对齐的中文列
_KLINE_EN = ["date", "open", "close", "high", "low", "volume",
             "amount", "amplitude", "pct_change", "change", "turnover"]
_CN_COLS = {
    "date": "日期", "open": "开盘", "close": "收盘", "high": "最高",
    "low": "最低", "volume": "成交量", "amount": "成交额",
    "amplitude": "振幅", "pct_change": "涨跌幅", "change": "涨跌额",
    "turnover": "换手率",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://q.10jqka.com.cn/",
}

_THS_KLINE_HOST = "https://d.10jqka.com.cn"
_THS_LIST_HOST = "https://q.10jqka.com.cn"


def _to_float(v) -> float:
    """把字段(可能是 str/'-'/None)安全转 float，失败返回 0.0。"""
    try:
        if v in (None, "", "-"):
            return 0.0
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _norm_sector_code(symbol: str) -> str:
    """同花顺行业代码归一：提取纯数字（881xxx）。兼容 bk881121 / 881121.SI / 881121。"""
    s = str(symbol).strip().upper()
    s = s.replace("BK", "").replace("HS", "")
    return re.sub(r"\D", "", s)


def _norm_stock_code(symbol: str) -> str:
    """个股代码归一：提取 6 位数字（600519），去掉 sh/sz/bj 前缀与点号后缀。"""
    s = str(symbol).strip().lower()
    s = re.sub(r"^(sh|sz|bj)", "", s)
    s = s.split(".")[0]
    return re.sub(r"\D", "", s)


class THSDataSource(BaseDataSource):
    """同花顺实时/历史数据源。"""

    def __init__(self):
        self._ak = None  # 惰性：仅 fallback 时初始化
        self._retries = AKSHARE_RETRY_CONFIG["max_retries"]
        self._retry_interval = AKSHARE_RETRY_CONFIG["retry_interval"]
        self._backoff = AKSHARE_RETRY_CONFIG["backoff_factor"]
        logger.info("THSDataSource 初始化（同花顺公开接口，无需 token）。")

    # ============================================================
    # 惰性 AkShare 委托（非核心维度兜底层）
    # ============================================================
    def _lazy_ak(self):
        if self._ak is None:
            try:
                from data.sources.akshare_source import AkShareSource
                self._ak = AkShareSource()
            except Exception as e:
                logger.warning("AkShare 惰性加载失败（同花顺主路径不受影响）：%s", e)
                self._ak = None
        return self._ak

    def _fb(self, method: str, *args, **kwargs):
        ak = self._lazy_ak()
        if ak is None:
            logger.warning("THS 委托 %s 失败：AkShare 不可用", method)
            return None
        return getattr(ak, method)(*args, **kwargs)

    # ============================================================
    # HTTP 工具
    # ============================================================
    def _http_get(self, url: str, timeout: int = 15) -> Optional[str]:
        for attempt in range(self._retries + 1):
            try:
                req = urllib.request.Request(url, headers=_HEADERS)
                return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
            except Exception as e:  # noqa: BLE001
                logger.debug("THS 请求失败 %s: %s", url, e)
                if attempt < self._retries:
                    time.sleep(self._retry_interval * (self._backoff ** attempt))
                    continue
                return None
        return None

    # ============================================================
    # K 线（行业 / 个股）
    # ============================================================
    def _ths_kline(self, prefix: str, code: str, count: int = 800) -> Optional[pd.DataFrame]:
        """同花顺 K 线（日线）。prefix=bk 行业 / hs 个股。返回中文列 DataFrame 或 None。

        响应为 JSONP：quotebridge_v6_line_xxx({"num":..., "data":"日期,开,高,低,收,量,额,空,空,空,状态;..."})
        字段顺序（逗号）：日期(YYYYMMDD), 开盘, 最高, 最低, 收盘, 成交量, 成交额, 空, 空, 空, 状态位
        """
        url = f"{_THS_KLINE_HOST}/v6/line/{prefix}_{code}/01/last{count}.js"
        raw = self._http_get(url)
        if not raw:
            return None
        try:
            s = raw.index("(")
            e = raw.rindex(")")
            obj = json.loads(raw[s + 1:e])
        except Exception as e:  # noqa: BLE001
            logger.debug("THS K线解析失败 %s: %s", code, e)
            return None
        data = obj.get("data")
        if not data:
            return None
        rows = []
        prev_close = None
        for kl in data.split(";"):
            if not kl:
                continue
            p = kl.split(",")
            if len(p) < 7:
                continue
            try:
                d = p[0]
                o = _to_float(p[1]); h = _to_float(p[2]); l = _to_float(p[3]); c = _to_float(p[4])
                v = _to_float(p[5]); a = _to_float(p[6])
            except (ValueError, IndexError):
                continue
            if prev_close:
                pct = (c - prev_close) / prev_close * 100.0
                chg = c - prev_close
                amp = (h - l) / prev_close * 100.0
            else:
                pct = chg = amp = 0.0
            rows.append({
                "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
                "open": o, "close": c, "high": h, "low": l,
                "volume": v, "amount": a,
                "amplitude": round(amp, 4), "pct_change": round(pct, 4),
                "change": round(chg, 4), "turnover": None,
            })
            prev_close = c
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=_KLINE_EN)
        return df.rename(columns=_CN_COLS)

    # ============================================================
    # 行业清单 + 实时涨跌（同一接口，无需登录）
    # ============================================================
    def _fetch_ths_industry_rows(self) -> list:
        """翻页拉取同花顺全部行业（代码/名称/涨跌幅）。返回 list[dict]。

        HTML 每行结构（Markdown 渲染视角）：
          [板块名](https://q.10jqka.com.cn/thshy/detail/code/881xxx/) 涨跌幅 成交量 ...
        用正则提取 (code, name, pct)。带去重，避免不分页时重复。
        """
        rows = []
        seen = set()
        for page in range(1, 12):
            url = (f"{_THS_LIST_HOST}/thshy/index/field/199112/order/desc/"
                   f"page/{page}/ajax/1/")
            html = self._http_get(url, timeout=15)
            if not html:
                break
            found = re.findall(
                r'thshy/detail/code/(\d+)/">([^<]+)</a>\s*</td>\s*<td[^>]*>([\d.\-]+)</td>',
                html,
            )
            if not found:
                break
            new_in_page = 0
            for code, name, pct in found:
                if code in seen:
                    continue
                seen.add(code)
                new_in_page += 1
                rows.append({"code": code, "name": name, "pct": _to_float(pct)})
            if new_in_page == 0:
                break  # 整页都是重复（接口不分页）→ 停止
            if len(found) < 50:
                break  # 已是末页
        return rows

    def get_em_industry_list(self) -> Optional[dict]:
        """同花顺行业板块清单 {881xxx: 名称}。"""
        rows = self._fetch_ths_industry_rows()
        if not rows:
            return None
        return {r["code"]: r["name"] for r in rows}

    def get_realtime_sector_quotes(self, secids: Optional[list] = None) -> list:
        """同花顺行业板块实时快照（盘中涨跌）。返回 list[dict]{name,code,pct}。

        供看板顶部「盘中实时行情条」使用，红涨绿跌展示各行业涨跌幅。
        """
        rows = self._fetch_ths_industry_rows()
        quotes = []
        for r in rows:
            quotes.append({"name": r["name"], "code": r["code"], "pct": r["pct"]})
        return quotes

    # ============================================================
    # BaseDataSource 抽象方法实现
    # ============================================================
    def get_sw_level1_info(self, *args, **kwargs):
        return self._fb("get_sw_level1_info", *args, **kwargs)

    def get_sw_level2_info(self, *args, **kwargs):
        return self._fb("get_sw_level2_info", *args, **kwargs)

    def get_sw_index_hist(self, symbol: str, period: str = "daily") -> Optional[pd.DataFrame]:
        code = _norm_sector_code(symbol)
        if not code:
            return None
        df = self._ths_kline("bk", code)
        if df is not None and not df.empty:
            return df
        logger.warning("同花顺行业K线 %s 拉取失败，回退 AkShare", symbol)
        return self._fb("get_sw_index_hist", symbol, period)

    def get_index_component(self, symbol: str, *args, **kwargs):
        return self._fb("get_index_component", symbol, *args, **kwargs)

    def get_em_industry_hist(self, symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
        code = _norm_sector_code(symbol)
        if not code:
            return None
        df = self._ths_kline("bk", code)
        if df is not None and not df.empty:
            return df
        logger.warning("同花顺行业历史 %s 拉取失败，回退 AkShare", symbol)
        return self._fb("get_em_industry_hist", symbol, start, end)

    def get_em_industry_cons(self, symbol: str, *args, **kwargs):
        return self._fb("get_em_industry_cons", symbol, *args, **kwargs)

    def get_stock_hist(self, symbol: str, start: str = "", end: str = "",
                       adjust: str = "qfq") -> Optional[pd.DataFrame]:
        code = _norm_stock_code(symbol)
        if not code:
            return None
        df = self._ths_kline("hs", code)
        if df is not None and not df.empty:
            return df
        return self._fb("get_stock_hist", symbol, start, end, adjust)

    def get_market_fund_flow(self, *args, **kwargs):
        return self._fb("get_market_fund_flow", *args, **kwargs)

    def get_concept_fund_flow(self, *args, **kwargs):
        return self._fb("get_concept_fund_flow", *args, **kwargs)

    def get_sector_fund_flow_rank(self, indicator: str = "今日", *args, **kwargs):
        return self._fb("get_sector_fund_flow_rank", indicator, *args, **kwargs)

    def get_stock_individual_fund_flow(self, stock: str, market: str = "sh", *args, **kwargs):
        return self._fb("get_stock_individual_fund_flow", stock, market, *args, **kwargs)

    def get_north_fund_summary(self, *args, **kwargs):
        return self._fb("get_north_fund_summary", *args, **kwargs)

    def get_north_hist(self, symbol: str, *args, **kwargs):
        return self._fb("get_north_hist", symbol, *args, **kwargs)

    def get_margin_detail_sse(self, date: str, *args, **kwargs):
        return self._fb("get_margin_detail_sse", date, *args, **kwargs)

    def get_margin_detail_szse(self, date: str, *args, **kwargs):
        return self._fb("get_margin_detail_szse", date, *args, **kwargs)

    def get_benchmark_hist(self, symbol: str, *args, **kwargs) -> Optional[pd.DataFrame]:
        # 基准（沪深300等）不卡 7.30，同花顺指数 K 线前缀不稳，委托 AkShare
        return self._fb("get_benchmark_hist", symbol, *args, **kwargs)

    def get_trade_calendar(self, *args, **kwargs):
        return self._fb("get_trade_calendar", *args, **kwargs)

    def get_zt_pool(self, date: str, *args, **kwargs):
        return self._fb("get_zt_pool", date, *args, **kwargs)

    def get_etf_list(self, *args, **kwargs):
        return self._fb("get_etf_list", *args, **kwargs)

"""
东方财富实时/历史数据源（EastMoneyLiveSource）
=============================================
基于东方财富公开行情接口，无需 token：
  - 历史 K 线（主）：push2his.eastmoney.com/api/qt/stock/kline/get
  - 历史 K 线（兜底）：web.ifzq.gtimg.cn 腾讯财经 K 线（push2his 不稳时自动回溯）
  - 实时快照：push2.eastmoney.com/api/qt/stock/get

定位：
  作为「更新鲜」的主数据源接入板块轮动系统。相比 AkShare 的部分接口
  （申万指数历史、基准指数等）存在 T+1+ 滞后，东方财富 K 线接口通常能
  在当日收盘后即刻提供最新交易日数据（实测已含 2026-07-31 收盘）。

覆盖策略（继承 BaseDataSource，实现全部 20 个抽象方法）：
  - 指数 / 个股 / 东方财富行业板块：使用东方财富 K 线（数据到当日收盘，准实时）；
    其中指数与个股的 K 线在 push2his 不可达时自动回退腾讯财经 K 线。
  - 申万指数（东方财富无直接 secid，90.801xxx 被解析为概念板块）：惰性委托
    内部 AkShareSource 实例。
  - 分类 / 成分股 / 资金流 / 北向 / 融资融券 / 涨停 / ETF / 交易日历等东方财富
    不便覆盖的：全部惰性委托 AkShareSource，保证功能完整不丢失。

惰性初始化：
  内部 AkShareSource 仅在 fallback 路径首次触发时才 import，使东方财富主路径
  （基准 / 个股 / 东财行业板块）在无 akshare 环境也能独立运行，互不拖累。

返回列风格：
  与 AkShareSource 同名方法尽量保持一致（基准=英文列，个股/行业板块=中文列），
  下游（update_benchmarks / 个股下钻 等）零改动。
"""

import time
import json
import urllib.request
from typing import Optional
import pandas as pd

from config.logger import get_logger
from config.settings import AKSHARE_RETRY_CONFIG
from .base import BaseDataSource

logger = get_logger(__name__)

# 东财 K 线字段顺序（fields2=f51..f61）
# f51=日期 f52=开盘 f53=收盘 f54=最高 f55=最低 f56=成交量(手)
# f57=成交额(元) f58=振幅(%) f59=涨跌幅(%) f60=涨跌额 f61=换手率(%)
_KLINE_FIELDS = ["date", "open", "close", "high", "low", "volume",
                 "amount", "amplitude", "pct_change", "change", "turnover"]

# 中文列名（与 AkShare stock_zh_a_hist / stock_board_industry_hist_em 对齐）
_CN_COLS = {
    "date": "日期", "open": "开盘", "close": "收盘", "high": "最高",
    "low": "最低", "volume": "成交量", "amount": "成交额",
    "amplitude": "振幅", "pct_change": "涨跌幅", "change": "涨跌额",
    "turnover": "换手率",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://quote.eastmoney.com/",
}

# 腾讯财经 K 线兜底头
_TENCENT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://gu.qq.com/",
}


def _to_float(v) -> float:
    """把东财字段(可能是 str/'-'/None)安全转 float，失败返回 0.0。"""
    try:
        if v in (None, "", "-"):
            return 0.0
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _secid_of_index(code: str) -> Optional[str]:
    """把基准指数代码（sh000300 / 000300.SH / 000300）转为东财 secid（1.000300）。"""
    code = str(code).strip()
    market, num = None, None
    if code.startswith("sh"):
        market, num = "1", code[2:]
    elif code.startswith("sz"):
        market, num = "0", code[2:]
    elif code.endswith(".SH") or code.endswith(".si"):
        market, num = "1", code.split(".")[0]
    elif code.endswith(".SZ"):
        market, num = "0", code.split(".")[0]
    else:
        # 纯数字：沪市 6/9 开头 -> 1，深市 0/3 开头 -> 0
        num = code
        if code[0] in ("6", "9"):
            market = "1"
        elif code[0] in ("0", "3"):
            market = "0"
    if not market or not num:
        return None
    return f"{market}.{num}"


def _secid_of_stock(code: str) -> Optional[str]:
    """把个股代码（600519）转为东财 secid（1.600519）。"""
    code = str(code).strip()
    if code[0] in ("6", "9"):
        return f"1.{code}"
    if code[0] in ("0", "3"):
        return f"0.{code}"
    return None


def _secid_to_tencent(secid: str) -> Optional[str]:
    """东财 secid -> 腾讯代码。仅 1./0. 前缀(指数/个股)可映射；90.BK 行业板块不可。"""
    if not secid or "." not in secid:
        return None
    market, num = secid.split(".", 1)
    if market == "1":
        return "sh" + num
    if market == "0":
        return "sz" + num
    return None


class EastMoneyLiveSource(BaseDataSource):
    """东方财富实时/历史数据源。"""

    def __init__(self):
        self._ak = None  # 惰性：仅 fallback 时初始化
        self._retries = AKSHARE_RETRY_CONFIG["max_retries"]
        self._retry_interval = AKSHARE_RETRY_CONFIG["retry_interval"]
        self._backoff = AKSHARE_RETRY_CONFIG["backoff_factor"]
        logger.info("EastMoneyLiveSource 初始化（东方财富公开接口，无需 token）。")

    # ============================================================
    # 内部工具
    # ============================================================
    def _lazy_ak(self):
        """惰性加载 AkShareSource（仅 fallback 路径触发）。"""
        if self._ak is None:
            try:
                from data.sources.akshare_source import AkShareSource
                self._ak = AkShareSource()
            except Exception as e:
                logger.warning("AkShare 惰性加载失败（东方财富主路径不受影响）：%s", e)
                self._ak = None
        return self._ak

    def _fb(self, method: str, *args, **kwargs):
        """委托给内部 AkShareSource；不可用时返回 None。"""
        ak = self._lazy_ak()
        if ak is None:
            logger.warning("EastMoney 委托 %s 失败：AkShare 不可用", method)
            return None
        return getattr(ak, method)(*args, **kwargs)

    def _kline(self, secid: str, start: str = "19900101", end: str = "20500101",
               fqt: int = 1, klt: int = 101) -> Optional[pd.DataFrame]:
        """东方财富 K 线（日线）拉取 + 标准化；主源(push2his)失败时自动回溯腾讯 K 线。"""
        if not secid:
            return None
        # 1) 主源：东方财富
        df = self._kline_eastmoney(secid, start, end, fqt, klt)
        if df is not None and not df.empty:
            return df
        # 2) 兜底：腾讯财经 K 线（本沙箱/部分地区 push2his 不稳时生效）
        df = self._kline_tencent(secid, start, end, fqt)
        if df is not None and not df.empty:
            return df
        return None

    def _kline_eastmoney(self, secid: str, start: str, end: str, fqt: int, klt: int) -> Optional[pd.DataFrame]:
        """东方财富 K 线。

        主机优先级：先试 ``push2.eastmoney.com``（与 clist / 实时快照同域名，
        云端通常可达），再试 ``push2his.eastmoney.com`` 兜底。任一可用即返回，
        全部失败返回 None（交由上层回退腾讯/AkShare）。
        """
        hosts = [
            "https://push2.eastmoney.com/api/qt/stock/kline/get",
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        ]
        last_err = None
        for host in hosts:
            url = (
                f"{host}"
                f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
                f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
                f"&klt={klt}&fqt={fqt}&beg={start}&end={end}"
            )
            try:
                req = urllib.request.Request(url, headers=_HEADERS)
                raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
                d = json.loads(raw)
                data = d.get("data")
                if not data or not data.get("klines"):
                    logger.debug("东财K线 %s 无数据(host=%s)", secid, host)
                    continue  # 换个 host 再试
                rows = []
                for kl in data["klines"]:
                    p = kl.split(",")
                    if len(p) < 7:
                        continue
                    try:
                        rows.append({
                            "date": p[0],
                            "open": float(p[1]), "close": float(p[2]),
                            "high": float(p[3]), "low": float(p[4]),
                            "volume": float(p[5]), "amount": float(p[6]),
                            "amplitude": float(p[7]) if len(p) > 7 else None,
                            "pct_change": float(p[8]) if len(p) > 8 else None,
                            "change": float(p[9]) if len(p) > 9 else None,
                            "turnover": float(p[10]) if len(p) > 10 else None,
                        })
                    except (ValueError, IndexError):
                        continue
                if not rows:
                    continue  # 换个 host 再试
                df = pd.DataFrame(rows, columns=_KLINE_FIELDS)
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                return df
            except Exception as e:
                last_err = e
                logger.debug("东财K线 %s 失败(host=%s): %s", secid, host, e)
                continue  # 换个 host 再试
        if last_err:
            logger.debug("东财K线 %s 所有 host 失败: %s", secid, last_err)
        return None

    def _kline_tencent(self, secid: str, start: str, end: str, fqt: int) -> Optional[pd.DataFrame]:
        """腾讯财经 K 线兜底（web.ifzq.gtimg.cn）。仅支持指数/个股（sh/sz 代码）；
        东财行业板块(90.BKxxxx)无法映射，返回 None（调用方再回退 AkShare）。"""
        tcode = _secid_to_tencent(secid)
        if not tcode:
            return None
        qfq = {1: "qfq", 2: "hfq", 0: ""}.get(fqt, "qfq")
        # 腾讯要求 YYYY-MM-DD 横杠格式
        def _fmt(s):
            s = str(s).strip()
            if len(s) == 8 and s.isdigit():
                return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
            return s
        s = _fmt(start) if len(str(start)) >= 8 else "1990-01-01"
        e = _fmt(end) if len(str(end)) >= 8 else "2050-01-01"
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={tcode},day,{s},{e},1000,{qfq}"
        )
        try:
            req = urllib.request.Request(url, headers=_TENCENT_HEADERS)
            raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
            d = json.loads(raw)
            data = d.get("data", {}).get(tcode)
            if not data:
                return None
            arr = data.get("qfqday") or data.get("hfqday") or data.get("day")
            if not arr:
                return None
            rows = []
            for r in arr:
                if len(r) < 6:
                    continue
                try:
                    rows.append({
                        "date": r[0],
                        "open": float(r[1]), "close": float(r[2]),
                        "high": float(r[3]), "low": float(r[4]),
                        "volume": float(r[5]),
                        "amount": float(r[6]) if len(r) > 6 else None,
                        "amplitude": None, "pct_change": None,
                        "change": None, "turnover": None,
                    })
                except (ValueError, IndexError, TypeError):
                    continue
            if not rows:
                return None
            df = pd.DataFrame(rows, columns=_KLINE_FIELDS)
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            return df
        except Exception as ex:
            logger.debug("腾讯K线 %s 失败: %s", secid, ex)
            return None

    def _quote(self, secids, fields="f43,f57,f58,f169,f170,f86"):
        """东方财富 push2 实时快照。返回 list[dict]，失败返回 []。"""
        if isinstance(secids, str):
            secids = [secids]
        secids = ",".join(secids)
        url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secids}&fields={fields}"
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
            d = json.loads(raw)
            if d.get("data"):
                return [d["data"]]
        except Exception as e:
            logger.warning("东财实时快照失败: %s", e)
        return []

    def get_realtime_sector_quotes(self, secids: Optional[list] = None) -> list:
        """批量实时快照（东方财富 ulist.np/get），一次请求拿全部行业板块的现价/涨跌幅。

        返回 list[dict]{code, name, price, pct, timestamp}；失败返回 []。
        不传 secids 时取当前东财行业宇宙全部 BKxxxx。
        """
        if secids is None:
            try:
                from config.sector_map import get_all_level2_codes
                secids = [f"90.{c}" for c in get_all_level2_codes()]
            except Exception:
                secids = []
        if not secids:
            return []

        out = []
        batch = 200  # 单批上限，避免 URL 过长
        for i in range(0, len(secids), batch):
            chunk = secids[i:i + batch]
            secids_str = ",".join(chunk)
            url = (
                "https://push2.eastmoney.com/api/qt/ulist.np/get"
                f"?secids={secids_str}&fields=f12,f13,f14,f2,f3,f62,f86&np=1"
            )
            try:
                req = urllib.request.Request(url, headers=_HEADERS)
                raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
                d = json.loads(raw)
                items = d.get("data", {}).get("diff") or []
                for it in items:
                    out.append({
                        "code": it.get("f12"),
                        "name": it.get("f14"),
                        "price": _to_float(it.get("f2")),
                        "pct": _to_float(it.get("f3")),
                        "main_net": _to_float(it.get("f62")),
                        "timestamp": it.get("f86"),
                    })
            except Exception as e:
                logger.warning("东财实时批量快照失败(批次 %d): %s", i, e)
                continue
        return out

    # ============================================================
    # 申万行业分类（委托 AkShare）
    # ============================================================
    def get_sw_level1_info(self) -> Optional[pd.DataFrame]:
        return self._fb("get_sw_level1_info")

    def get_sw_level2_info(self) -> Optional[pd.DataFrame]:
        return self._fb("get_sw_level2_info")

    def get_index_component(self, symbol: str) -> Optional[pd.DataFrame]:
        return self._fb("get_index_component", symbol)

    # ============================================================
    # 申万指数历史（东方财富无直接 secid，委托 AkShare）
    # ============================================================
    def get_sw_index_hist(self, symbol: str, period: str = "day") -> Optional[pd.DataFrame]:
        """板块指数历史。

        东方财富行业板块（BKxxxx / 90.BKxxxx）走自有 K 线（到最新交易日收盘，
        返回中文列：日期/开盘/收盘/最高/最低…，与下游指标层 schema 一致）。
        申万代码（801xxx 等无东财 secid）回退 AkShare，保证遗留路径不丢。
        """
        s = str(symbol).strip()
        if s.upper().startswith("BK") or s.startswith("90."):
            secid = s if s.startswith("90.") else f"90.{s}"
            df = self._kline(secid)
            if df is not None and not df.empty:
                return df.rename(columns=_CN_COLS)
            # 主源(东财双 host)均失败 → 回退 AkShare 东财行业历史。
            # 注意：AkShare 行业数据常滞后至 T-1+（实测停于 7.30），属次优，必须告警。
            logger.warning(
                "东财行业板块 %s K线不可达(push2/push2his 均失败)，回退 AkShare（数据可能滞后）", s
            )
            ak = self._lazy_ak()
            if ak is not None:
                return ak.get_em_industry_hist(s)
            return None
        # 其余（如遗留申万代码）委托 AkShare
        return self._fb("get_sw_index_hist", symbol, period)

    # ============================================================
    # 东方财富行业板块（自有 K 线，到最新交易日收盘）
    # ============================================================
    def get_em_industry_list(self) -> Optional[dict]:
        """东方财富行业板块清单（{BKxxxx: 名称}）。主源 clist，失败回退 AkShare。"""
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            "?pn=1&pz=600&fs=m:90+t:2&fields=f12,f13,f14&fid=f3&np=1"
        )
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
            d = json.loads(raw)
            items = d.get("data", {}).get("diff") or []
            out = {}
            for it in items:
                code = it.get("f12")
                name = it.get("f14")
                if code and name:
                    out[str(code)] = str(name)
            if out:
                return out
            logger.debug("东财行业清单(clist)返回空")
        except Exception as e:
            logger.warning("东财行业清单(clist)失败: %s", e)
        # 兜底 AkShare
        ak = self._lazy_ak()
        if ak is not None and hasattr(ak, "get_em_industry_list"):
            try:
                df = ak.get_em_industry_list()
                if df is not None and not getattr(df, "empty", True):
                    return {str(r["板块代码"]): str(r["板块名称"]) for _, r in df.iterrows()}
            except Exception as e:
                logger.warning("AkShare 行业清单兜底失败: %s", e)
        return None

    def get_em_industry_hist(self, symbol: str, start: str = "20180101",
                             end: str = "20500101") -> Optional[pd.DataFrame]:
        """东方财富行业板块历史（BK0477 等）。东财 secid = 90.BKxxxx。
        腾讯无法映射行业板块(90.BK)，主源失败后再回退 AkShare。"""
        secid = symbol if symbol.startswith("90.") else f"90.{symbol}"
        df = self._kline(secid, start=start, end=end)
        if df is None or df.empty:
            ak = self._lazy_ak()
            if ak is not None:
                return ak.get_em_industry_hist(symbol, start, end)
            return None
        return df.rename(columns=_CN_COLS)

    def get_em_industry_cons(self, symbol: str) -> Optional[pd.DataFrame]:
        return self._fb("get_em_industry_cons", symbol)

    # ============================================================
    # 个股行情（东方财富 K 线，到最新交易日收盘）
    # ============================================================
    def get_stock_hist(self, symbol: str, start: str, end: str,
                       adjust: str = "qfq") -> Optional[pd.DataFrame]:
        """个股日线。adjust: qfq=前复权(1) hfq=后复权(2) ''=不复权(0)。"""
        fqt = {"qfq": 1, "hfq": 2, "": 0}.get(adjust, 1)
        secid = _secid_of_stock(symbol)
        df = self._kline(secid, start=start, end=end, fqt=fqt)
        if df is None or df.empty:
            return None
        return df.rename(columns=_CN_COLS)

    # ============================================================
    # 基准指数（东方财富 K 线，到最新交易日收盘）
    # ============================================================
    def get_benchmark_hist(self, symbol: str = "sh000300") -> Optional[pd.DataFrame]:
        """基准指数历史。symbol 如 sh000300 / 000300.SH。"""
        secid = _secid_of_index(symbol)
        df = self._kline(secid)
        if df is None or df.empty:
            return None
        return df

    # ============================================================
    # 资金流（委托 AkShare）
    # ============================================================
    def get_market_fund_flow(self) -> Optional[pd.DataFrame]:
        return self._fb("get_market_fund_flow")

    def get_concept_fund_flow(self) -> Optional[pd.DataFrame]:
        return self._fb("get_concept_fund_flow")

    def get_sector_fund_flow_rank(self, indicator: str = "今日") -> Optional[pd.DataFrame]:
        return self._fb("get_sector_fund_flow_rank", indicator)

    def get_stock_individual_fund_flow(self, stock: str = "600000",
                                       market: str = "sh") -> Optional[pd.DataFrame]:
        return self._fb("get_stock_individual_fund_flow", stock, market)

    # ============================================================
    # 北向资金（委托 AkShare）
    # ============================================================
    def get_north_fund_summary(self) -> Optional[pd.DataFrame]:
        return self._fb("get_north_fund_summary")

    def get_north_hist(self, symbol: str = "沪股通") -> Optional[pd.DataFrame]:
        return self._fb("get_north_hist", symbol)

    # ============================================================
    # 融资融券（委托 AkShare）
    # ============================================================
    def get_margin_detail_sse(self, date: str = "20240101") -> Optional[pd.DataFrame]:
        return self._fb("get_margin_detail_sse", date)

    def get_margin_detail_szse(self, date: str = "20240101") -> Optional[pd.DataFrame]:
        return self._fb("get_margin_detail_szse", date)

    # ============================================================
    # 交易日历（委托 AkShare）
    # ============================================================
    def get_trade_calendar(self) -> Optional[pd.DataFrame]:
        return self._fb("get_trade_calendar")

    # ============================================================
    # 涨停板（委托 AkShare）
    # ============================================================
    def get_zt_pool(self, date: str = "20240101") -> Optional[pd.DataFrame]:
        return self._fb("get_zt_pool", date)

    # ============================================================
    # ETF（委托 AkShare）
    # ============================================================
    def get_etf_list(self) -> Optional[pd.DataFrame]:
        return self._fb("get_etf_list")

    # ============================================================
    # 实时快照（公开扩展能力，供前端实时组件复用）
    # ============================================================
    def get_live_quote(self, secids, fields: str = "f43,f57,f58,f169,f170,f86"):
        """实时行情快照。secids 可为 str 或 list[str]（东财 secid，如 '1.000300'）。"""
        return self._quote(secids, fields)

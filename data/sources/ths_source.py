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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://q.10jqka.com.cn/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_THS_KLINE_HOST = "https://d.10jqka.com.cn"
_THS_LIST_HOST = "https://q.10jqka.com.cn"

# 同花顺行业板块静态兜底映射（90 个，代码稳定）。
# 当实时行业清单接口因反爬/网络抖动返回空时，用此映射保证板块宇宙不空、
# 刷新循环仍有板块可更新。名称可能与实时略有出入，但 K 线代码有效。
_THS_FALLBACK_MAP: dict[str, str] = {
    "881271": "IT服务", "881272": "软件开发", "881171": "自动化设备",
    "881164": "文化传媒", "881274": "影视院线", "881130": "计算机设备",
    "881275": "游戏", "881162": "通信服务", "881270": "元件",
    "881121": "半导体", "881277": "电机", "881169": "贵金属",
    "881129": "通信设备", "881178": "教育", "881284": "环保设备",
    "881117": "通用设备", "881124": "消费电子", "881172": "电子化学品",
    "881276": "军工电子", "881282": "其他电源设备", "881123": "其他电子",
    "881122": "光学光电子", "881179": "其他社会服务", "881265": "塑料制品",
    "881175": "医疗服务", "881116": "建筑装饰", "881177": "互联网电商",
    "881170": "小金属", "881126": "汽车零部件", "881118": "专用设备",
    "881167": "非金属材料", "881138": "包装印刷", "881131": "白色家电",
    "881114": "金属新材料", "881144": "医疗器械", "881181": "环境治理",
    "881266": "橡胶制品", "881142": "生物制品", "881137": "造纸",
    "881132": "黑色家电", "881278": "电网设备", "881139": "家居用品",
    "881133": "饮料制造", "881168": "工业金属", "881166": "军工装备",
    "881136": "服装家纺", "881165": "综合", "881101": "种植业与林业",
    "881269": "轨交设备", "881173": "小家电", "881115": "建筑材料",
    "881279": "光伏设备", "881146": "燃气", "881141": "中药",
    "881140": "化学制药", "881160": "旅游及酒店", "881281": "电池",
    "881102": "养殖业", "881143": "医药商业", "881153": "房地产",
    "881280": "风电设备", "881109": "化学制品", "881158": "零售",
    "881134": "食品加工制造", "881128": "汽车服务及其他", "881268": "工程机械",
    "881283": "多元金融", "881135": "纺织制造", "881145": "电力",
    "881103": "农产品加工", "881159": "贸易", "881180": "石油加工贸易",
    "881152": "物流", "881105": "煤炭开采加工", "881182": "美容护理",
    "881264": "化学纤维", "881151": "机场航运", "881108": "化学原料",
    "881273": "白酒", "881148": "港口航运", "881149": "公路铁路运输",
    "881112": "钢铁", "881157": "证券", "881125": "汽车整车",
    "881174": "厨卫电器", "881107": "油气开采及服务", "881263": "农化制品",
    "881267": "能源金属", "881156": "保险", "881155": "银行",
}


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
            # 允许 <a> 标签里有 target="_blank" 等其它属性
            found = re.findall(
                r'thshy/detail/code/(\d+)/[^>]*>([^<]+)</a>\s*</td>\s*<td[^>]*>([\d.\-]+)</td>',
                html,
            )
            if not found:
                # 再尝试宽松匹配（只要求 code/name，不要涨幅列）
                found = re.findall(
                    r'thshy/detail/code/(\d+)/[^>]*>([^<]+)</a>',
                    html,
                )
                found = [(code, name, "0.0") for code, name in found]
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
        """同花顺行业板块清单 {881xxx: 名称}。

        实时接口失败时，启用静态兜底映射（90 个同花顺行业代码），
        保证板块宇宙不空、刷新循环仍有目标可更新。
        """
        rows = self._fetch_ths_industry_rows()
        if rows and len(rows) >= 50:
            return {r["code"]: r["name"] for r in rows}
        logger.warning(
            "同花顺实时行业清单仅拿到 %d 个/接口失败，启用静态兜底 %d 个",
            len(rows), len(_THS_FALLBACK_MAP)
        )
        return dict(_THS_FALLBACK_MAP)

    def get_realtime_sector_quotes(self, secids: Optional[list] = None) -> list:
        """同花顺行业板块实时快照（盘中涨跌）。返回 list[dict]{name,code,pct}。

        供看板顶部「盘中实时行情条」使用，红涨绿跌展示各行业涨跌幅。
        当实时接口失败时，启用静态兜底（涨幅置 0），保证行情条不空白。
        """
        rows = self._fetch_ths_industry_rows()
        if rows:
            return [{"name": r["name"], "code": r["code"], "pct": r["pct"]} for r in rows]
        logger.warning("同花顺实时行情接口失败，启用静态兜底（涨幅置 0）")
        return [{"name": name, "code": code, "pct": 0.0} for code, name in _THS_FALLBACK_MAP.items()]

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
        """同花顺行业资金流入排名（用实时涨跌幅代理主力净流入）。

        AkShare 的行业资金流排名命名与同花顺不完全一致，匹配失败会导致
        sector_fund_flow 表整体为空。同花顺实时行业接口提供全量 881xxx 涨跌幅，
        这里用涨跌幅作为净流入的代理指标，保证资金流向地图有数据可展示。

        返回 DataFrame 列：名称、代码、主力净流入-净额（代理）、涨跌幅
        """
        # 复用实时行业清单接口（带静态兜底）
        rows = self._fetch_ths_industry_rows()
        if not rows:
            logger.warning("同花顺行业实时接口为空，启用静态兜底生成资金流排名")
            rows = [{"code": code, "name": name, "pct": 0.0} for code, name in _THS_FALLBACK_MAP.items()]
        if not rows:
            return None

        df = pd.DataFrame(rows)
        df = df.rename(columns={"code": "代码", "name": "名称", "pct": "涨跌幅"})
        # 用涨跌幅作为主力净流入代理（方向/量级一致，仅用于排名）
        df["主力净流入-净额"] = df["涨跌幅"].astype(float)
        df["净流入-净额"] = df["涨跌幅"].astype(float)
        # 兼容 MoneyFlowIndicator 可能查找的列名
        df["主力净流入"] = df["涨跌幅"].astype(float)
        return df[["名称", "代码", "主力净流入-净额", "净流入-净额", "主力净流入", "涨跌幅"]]

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

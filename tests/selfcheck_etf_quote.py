"""ETF 行情链路测试：覆盖 secid 识别、_quote 重试、load_live_quotes_for_portfolio 腾讯兜底。

背景：东财 push2 实时快照对 ETF（1/5 开头）经常「Remote end closed」或被限流；
之前 _secid_of_stock 完全不识别 1/5 开头，导致 ETF 持仓的「当前市值/浮盈亏」永远显示
「暂无行情」。本套件一次性覆盖：ETF secid 解析、_quote 重试、整批主源失败时腾讯兜底。
"""

import json
import sys
import unittest.mock as mock

sys.path.insert(0, ".")

from data.sources import eastmoney_source  # noqa: E402
from data.sources.eastmoney_source import (  # noqa: E402
    EastMoneyLiveSource,
    _secid_of_stock,
)
from dashboard.views.portfolio import load_live_quotes_for_portfolio  # noqa: E402

results = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond)))
    if not cond:
        print(f"  FAIL  {name}  {detail}")


# ============================================================
# 1. _secid_of_stock 必须识别 ETF（1/5 开头）
# ============================================================
# 沪市 ETF：5 开头
check("secid: 510300(沪ETF) → 1.510300", _secid_of_stock("510300") == "1.510300")
check("secid: 588000(科创50ETF) → 1.588000", _secid_of_stock("588000") == "1.588000")
# 深市 ETF：1 开头
check("secid: 159766(深ETF) → 0.159766", _secid_of_stock("159766") == "0.159766")
check("secid: 159915(创业板ETF) → 0.159915", _secid_of_stock("159915") == "0.159915")
# 已有股票规则不能破坏
check("secid: 600519(沪股) → 1.600519", _secid_of_stock("600519") == "1.600519")
check("secid: 000001(深股) → 0.000001", _secid_of_stock("000001") == "0.000001")
check("secid: 300750(创业板) → 0.300750", _secid_of_stock("300750") == "0.300750")
check("secid: 688981(科创板) → 1.688981", _secid_of_stock("688981") == "1.688981")
check("secid: 830799(北交所) → 0.830799", _secid_of_stock("830799") == "0.830799")
# 非法输入
check("secid: 空串 → None", _secid_of_stock("") is None)
check("secid: 5 位 → None", _secid_of_stock("60051") is None)
check("secid: 7 位 → None", _secid_of_stock("6000510") is None)
check("secid: 2 开头 → None", _secid_of_stock("200519") is None)


# ============================================================
# 2. _quote 重试：东财「Remote end closed」时 1-2 次重试可恢复
# ============================================================
def _resp(body: bytes) -> mock.MagicMock:
    r = mock.MagicMock()
    r.read.return_value = body
    r.__enter__.return_value = r
    return r


def test_quote_retries_then_succeeds():
    """模拟第一次断连、第二次成功 → 应返回数据。"""
    em_body = json.dumps({"rc": 0, "data": {"f43": 58100, "f57": "159766", "f58": "旅游ETF富国"}}).encode()
    call_count = {"n": 0}

    def side_effect(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("Remote end closed connection without response")
        return _resp(em_body)

    src = EastMoneyLiveSource()
    with mock.patch.object(eastmoney_source.urllib.request, "urlopen", side_effect=side_effect):
        out = src._quote(["0.159766"], fields="f43,f57,f58")
    check("_quote: 首次断连后第二次成功 → 返回数据", len(out) == 1 and out[0]["f57"] == "159766", f"out={out}")
    check("_quote: 至少尝试 2 次", call_count["n"] >= 2, f"calls={call_count['n']}")


def test_quote_all_retries_fail():
    """两次都断连 → 返回空，不抛异常。"""
    call_count = {"n": 0}

    def side_effect(req, timeout=None):
        call_count["n"] += 1
        raise OSError("Remote end closed")

    src = EastMoneyLiveSource()
    with mock.patch.object(eastmoney_source.urllib.request, "urlopen", side_effect=side_effect):
        out = src._quote(["0.159766"], fields="f43,f57,f58")
    check("_quote: 全部失败 → 返回 []", out == [], f"out={out}")
    check("_quote: 不抛异常", True)


test_quote_retries_then_succeeds()
test_quote_all_retries_fail()


# ============================================================
# 3. load_live_quotes_for_portfolio: ETF 主源失败时自动回退腾讯
# ============================================================
def test_load_quotes_etf_falls_back_to_tencent():
    """159766 走东财被「Remote end closed」，应回退腾讯拿到价 0.581。"""
    tx_body = (
        b'v_sz159766="51~\xe6\x97\x85\xe6\xb8\xb8ETF\xe5\xaf\x8c\xe5\x9b\xbd~159766~0.581~0.579~0.579'
        b'~3085034~1536028~1549006~0.580~32995~0.579~13744~0.578~21761~0.577~20562~'
    )

    def side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "eastmoney.com" in url:
            # 东财重试用尽全部失败
            raise OSError("Remote end closed connection without response")
        if "qt.gtimg.cn" in url:
            return _resp(tx_body)
        raise AssertionError(f"unexpected url: {url}")

    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        df = load_live_quotes_for_portfolio(("159766",))

    check("兜底: 拿到 159766 行情", len(df) == 1, f"df={df.to_dict('records') if len(df) else df}")
    if len(df):
        check("兜底: 价格 0.581", abs(float(df.iloc[0]["market_price"]) - 0.581) < 1e-4, str(df.iloc[0]["market_price"]))
        check("兜底: 数据源标记为 tencent_realtime", df.iloc[0]["quote_source"] == "tencent_realtime", str(df.iloc[0]["quote_source"]))


def test_load_quotes_main_source_succeeds_for_stock():
    """股票走东财主源拿价，不去打扰腾讯。"""
    em_body = json.dumps({"rc": 0, "data": {"f43": 40000, "f57": "000001", "f58": "平安银行"}}).encode()

    def em_side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "eastmoney.com" in url:
            return _resp(em_body)
        raise AssertionError(f"unexpected url: {url}")

    with mock.patch("urllib.request.urlopen", side_effect=em_side_effect):
        df = load_live_quotes_for_portfolio(("000001",))
    check("主源: 拿到 000001 行情", len(df) == 1, f"df={df.to_dict('records') if len(df) else df}")
    if len(df):
        check("主源: 价格 400.0", abs(float(df.iloc[0]["market_price"]) - 400.0) < 1e-4, str(df.iloc[0]["market_price"]))
        check("主源: 数据源标记为 eastmoney_realtime", df.iloc[0]["quote_source"] == "eastmoney_realtime", str(df.iloc[0]["quote_source"]))


def test_load_quotes_mixed_etf_and_stock():
    """股票主源成功 + ETF 走腾讯兜底。"""
    em_body = json.dumps({"rc": 0, "data": {"f43": 40000, "f57": "000001", "f58": "平安银行"}}).encode()
    tx_body = (
        b'v_sz159766="51~\xe6\x97\x85\xe6\xb8\xb8ETF\xe5\xaf\x8c\xe5\x9b\xbd~159766~0.581~0.579'
        b'~0.579~3085034~1536028~1549006~0.580~32995~0.579~13744~0.578~21761~0.577~20562~'
    )

    def side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "eastmoney.com" in url:
            return _resp(em_body)
        if "qt.gtimg.cn" in url:
            return _resp(tx_body)
        raise AssertionError(f"unexpected url: {url}")

    with mock.patch("urllib.request.urlopen", side_effect=side_effect):
        df = load_live_quotes_for_portfolio(("000001", "159766"))
    check("混合: 拿到 2 条行情", len(df) == 2, f"df={df.to_dict('records') if len(df) else df}")
    if len(df) == 2:
        sources = sorted(df["quote_source"].tolist())
        check("混合: 1 东财 + 1 腾讯", sources == ["eastmoney_realtime", "tencent_realtime"], str(sources))
        codes = sorted(df["security_code"].tolist())
        check("混合: 包含 000001 + 159766", codes == ["000001", "159766"], str(codes))


test_load_quotes_etf_falls_back_to_tencent()
test_load_quotes_main_source_succeeds_for_stock()
test_load_quotes_mixed_etf_and_stock()


passed = sum(1 for _, c in results if c)
total = len(results)
print(f"\n=== 自检汇总  结果：{passed} 通过 / {total - passed} 失败 ===")
if passed != total:
    sys.exit(1)

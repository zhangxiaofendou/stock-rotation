"""持仓录入自动补全模块的无头回归测试。

覆盖：代码规范化、东财解析（含 ×100 价格换算）、腾讯兜底解析、
网络异常容错、东财失败回退腾讯、双源失败返回 None、进程级缓存命中。
"""

import json
import sys
import unittest.mock as mock
import urllib.error
import urllib.request

sys.path.insert(0, ".")

from portfolio import stock_lookup  # noqa: E402
from portfolio.stock_lookup import (  # noqa: E402
    clear_cache,
    eastmoney_secid,
    lookup_stock_info,
    market_prefix,
    normalize_code,
)


def _fake_resp(body: bytes, code: int = 200) -> mock.MagicMock:
    """构造带 read() 的 HTTP 响应替身。"""
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    return resp


def _route(urlopen_mock, em_body=None, tx_body=None, em_raise=None, tx_raise=None):
    """按 URL 分发东财/腾讯的响应或异常。"""

    def _side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if "eastmoney.com" in str(url):
            if em_raise:
                raise em_raise
            if em_body is None:
                return _fake_resp(b'{"rc":0,"data":null}')
            return _fake_resp(em_body)
        if "qt.gtimg.cn" in str(url):
            if tx_raise:
                raise tx_raise
            if tx_body is None:
                return _fake_resp(b"")
            return _fake_resp(tx_body)
        raise AssertionError(f"unexpected url: {url}")

    urlopen_mock.side_effect = _side_effect


_EM_MAOTAI = json.dumps(
    {"rc": 0, "data": {"f43": 135060, "f57": "600519", "f58": "贵州茅台", "f127": "白酒Ⅱ"}}
).encode("utf-8")

_TX_MAOTAI = (
    'v_sh600519="1~贵州茅台~600519~1350.60~1361.76~1330.03~55128~0~0~1350.60~1~'
    '20260731161450~-11.16~-0.82~1355.72~1325.77~1350.60/55128/7373462605~'
    '55128~737346~0.44~20.41~"'
).encode("gbk", "ignore")

results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, bool(cond)))
    if not cond:
        print(f"  FAIL  {name}  {detail}")


# ============================================================
# 1. 代码规范化
# ============================================================
check("normalize: 沪市主板", normalize_code("600519") == "600519")
check("normalize: 带空格", normalize_code(" 600519 ") == "600519")
check("normalize: 深市创业板", normalize_code("300750") == "300750")
check("normalize: 北交所", normalize_code("830799") == "830799")
check("normalize: 粘贴 sh 前缀", normalize_code("sh600519") == "600519")
check("normalize: 粘贴 SH 后缀", normalize_code("600519.SH") == "600519")
check("normalize: 空串", normalize_code("") is None)
check("normalize: 5位", normalize_code("60051") is None)
check("normalize: 无数字", normalize_code("abc") is None)
check("normalize: 数字不足6位", normalize_code("ab6005xy") is None)
check("prefix: 600→sh", market_prefix("600519") == "sh")
check("prefix: 688→sh", market_prefix("688981") == "sh")
check("prefix: 000→sz", market_prefix("000001") == "sz")
check("prefix: 300→sz", market_prefix("300750") == "sz")
check("prefix: 830→bj", market_prefix("830799") == "bj")
check("secid: 600→1.", eastmoney_secid("600519") == "1.600519")
check("secid: 000→0.", eastmoney_secid("000001") == "0.000001")
check("secid: 830→0.", eastmoney_secid("830799") == "0.830799")

# ============================================================
# 2. 东财主源解析（×100 价格换算 + 行业名）
# ============================================================
clear_cache()
with mock.patch.object(stock_lookup.urllib.request, "urlopen") as m:
    _route(m, em_body=_EM_MAOTAI)
    info = lookup_stock_info("600519")
    check("东财: 名称", info and info["name"] == "贵州茅台", str(info))
    check("东财: 价格 ÷100", info and info["price"] == 1350.6, str(info))
    check("东财: 行业名", info and info["sector_name"] == "白酒Ⅱ", str(info))

# 低价股（40 元 → f43=4000，必须正确换算为 40.0）
clear_cache()
with mock.patch.object(stock_lookup.urllib.request, "urlopen") as m:
    _route(m, em_body=json.dumps(
        {"rc": 0, "data": {"f43": 4000, "f57": "000001", "f58": "平安银行", "f127": "银行"}}
    ).encode("utf-8"))
    info = lookup_stock_info("000001")
    check("东财: 低价股价格正确", info and info["price"] == 40.0, str(info))

# ============================================================
# 3. 腾讯兜底解析
# ============================================================
clear_cache()
with mock.patch.object(stock_lookup.urllib.request, "urlopen") as m:
    _route(m, em_body=None, tx_body=_TX_MAOTAI)
    info = lookup_stock_info("600519")
    check("腾讯兜底: 名称", info and info["name"] == "贵州茅台", str(info))
    check("腾讯兜底: 价格", info and info["price"] == 1350.6, str(info))
    check("腾讯兜底: 行业为 None", info and info["sector_name"] is None, str(info))

# ============================================================
# 4. 容错：东财异常 → 回退腾讯
# ============================================================
clear_cache()
with mock.patch.object(stock_lookup.urllib.request, "urlopen") as m:
    _route(m, em_raise=urllib.error.URLError("boom"), tx_body=_TX_MAOTAI)
    info = lookup_stock_info("600519")
    check("容错: 东财异常回退腾讯", info and info["name"] == "贵州茅台", str(info))

# 5. 双源失败 → None，不抛异常
clear_cache()
with mock.patch.object(stock_lookup.urllib.request, "urlopen") as m:
    _route(m, em_raise=Exception("em down"), tx_raise=Exception("tx down"))
    info = lookup_stock_info("600519")
    check("容错: 双源失败返回 None", info is None)

# 6. 东财无数据 → 回退腾讯
clear_cache()
with mock.patch.object(stock_lookup.urllib.request, "urlopen") as m:
    _route(m, em_body=b'{"rc":0,"data":null}', tx_body=_TX_MAOTAI)
    info = lookup_stock_info("600519")
    check("容错: 东财空数据回退腾讯", info and info["name"] == "贵州茅台", str(info))

# 7. 非法代码不发起请求
clear_cache()
with mock.patch.object(stock_lookup.urllib.request, "urlopen") as m:
    info = lookup_stock_info("abc")
    check("非法代码: 返回 None", info is None)
    check("非法代码: 未发请求", m.call_count == 0)

# 8. 缓存命中（第二次不请求网络）
clear_cache()
with mock.patch.object(stock_lookup.urllib.request, "urlopen") as m:
    _route(m, em_body=_EM_MAOTAI)
    a = lookup_stock_info("600519")
    b = lookup_stock_info("600519")
    check("缓存: 两次结果一致", a == b)
    check("缓存: 第二次不请求", m.call_count == 1, f"calls={m.call_count}")


passed = sum(1 for _, c in results if c)
total = len(results)
print(f"\n=== 自检汇总  结果：{passed} 通过 / {total - passed} 失败 ===")
if passed != total:
    sys.exit(1)

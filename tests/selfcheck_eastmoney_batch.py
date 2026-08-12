"""实时批量快照 + 持仓行业回退 的回归测试。

诊断依据：
- 持仓管理页面 603887 行 "所属行业" 显示 None，截图复现。
- 排查发现 `data/sources/eastmoney_source.py::_quote` 第 311-312 行无条件 `return [d["data"]]`，
  假设东财 `stock/get` 单 secid 返回 dict，多 secid 也按 dict 处理；但多 secid 时东财
  实际返回 `data` 为 dict 列表（每 secid 一项），导致下游 `zip(secids, [list])` 解包异常，
  `item.get("f57")` 抛 AttributeError → 整批 fallback 腾讯 → 腾讯不带行业名 → 603887
  失去 quote_sector_name，加上录入时 stored sector_name 为空 → 显示「数据不足」。

修复点：
1. `_quote` 同时兼容单 secid (data=dict) 与多 secid (data=list) 两种返回。
2. `_build_position_analysis` 在 stored + quote_sector_name 都为空时，按 code 调一次
   `lookup_stock_info` 兜底，不再让存量「录入时未带出行业」的数据永远显示数据不足。

红绿验证步骤：先不修代码跑一遍确认红，再改 `_quote` 与 `_build_position_analysis` 跑绿。
"""

import json
import sys
import unittest.mock as mock

sys.path.insert(0, ".")

from data.sources import eastmoney_source  # noqa: E402
from data.sources.eastmoney_source import EastMoneyLiveSource  # noqa: E402


def _http_response(body: bytes) -> mock.MagicMock:
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    return resp


def _test_quote_handles_batch_list():
    """Case 1：多 secid 时东财返回 data 为 list（每项一个 secid）。_quote 必须返回该 list。"""
    body = json.dumps({
        "rc": 0,
        "data": [
            {"f43": 574, "f57": "159766", "f58": "旅游ETF", "f169": 6, "f170": 106, "f128": ""},
            {"f43": 929, "f57": "603887", "f58": "城地香江", "f127": "IT服务Ⅱ", "f169": -8, "f170": -85, "f128": "上海板块"},
        ],
    }).encode("utf-8")
    src = EastMoneyLiveSource()
    with mock.patch.object(eastmoney_source.urllib.request, "urlopen", return_value=_http_response(body)):
        rows = src._quote(["1.159766", "1.603887"], fields="f43,f57,f58,f127,f169,f170")
    assert isinstance(rows, list), f"_quote 应返回 list，实际 {type(rows).__name__}"
    assert len(rows) == 2, f"应有 2 行，实际 {len(rows)}（单 secid 假设把其他丢掉）"
    codes = sorted(r.get("f57") for r in rows)
    assert codes == ["159766", "603887"], f"丢失了 603887: {codes}"
    s603887 = next(r for r in rows if r.get("f57") == "603887")
    assert s603887.get("f127") == "IT服务Ⅱ", f"603887 的行业未带出: {s603887.get('f127')}"
    print("  [OK] Case 1: 多 secid 返回 list 时 _quote 不丢行且带出行业")


def _test_quote_single_dict_still_works():
    """Case 2：单 secid 时东财返回 data 为单 dict。_quote 仍返回 [dict]，不能被新逻辑破坏。"""
    body = json.dumps({
        "rc": 0,
        "data": {"f43": 929, "f57": "603887", "f58": "城地香江", "f127": "IT服务Ⅱ"},
    }).encode("utf-8")
    src = EastMoneyLiveSource()
    with mock.patch.object(eastmoney_source.urllib.request, "urlopen", return_value=_http_response(body)):
        rows = src._quote(["1.603887"], fields="f43,f57,f58,f127")
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0].get("f127") == "IT服务Ⅱ"
    print("  [OK] Case 2: 单 secid 单 dict 返回 [dict]（兼容旧行为）")


def _test_quote_empty_data_returns_empty():
    """Case 3：data 为 None（停牌/不存在）时返回 []，不能 [None]。"""
    body = json.dumps({"rc": -1, "data": None}).encode("utf-8")
    src = EastMoneyLiveSource()
    with mock.patch.object(eastmoney_source.urllib.request, "urlopen", return_value=_http_response(body)):
        rows = src._quote(["1.000000"], fields="f43,f57,f58")
    assert rows == [], f"应返回 []，实际 {rows}"
    print("  [OK] Case 3: data=None 时返回 []")


def _test_fetch_quotes_batch_propagates_sector():
    """Case 4：_fetch_quotes_batch 在 batch 返回 list 时，每个 secid 都拿到 quote_sector_name。"""
    body = json.dumps({
        "rc": 0,
        "data": [
            {"f43": 574, "f57": "159766", "f58": "旅游ETF", "f127": "", "f169": 6, "f170": 106},
            {"f43": 929, "f57": "603887", "f58": "城地香江", "f127": "IT服务Ⅱ", "f169": -8, "f170": -85},
        ],
    }).encode("utf-8")
    # 替换为本地伪造模块，绕过真实 streamlit import 链
    import types
    fake_em_module = types.ModuleType("data.sources.eastmoney_source")
    fake_em_module.EastMoneyLiveSource = EastMoneyLiveSource
    fake_em_module._secid_of_stock = eastmoney_source._secid_of_stock
    sys.modules["data.sources.eastmoney_source"] = fake_em_module
    try:
        from dashboard.views import portfolio as portfolio_page  # noqa: F401
        with mock.patch.object(eastmoney_source.urllib.request, "urlopen", return_value=_http_response(body)):
            rows = portfolio_page._fetch_quotes_batch(("159766", "603887"))
    finally:
        sys.modules["data.sources.eastmoney_source"] = eastmoney_source

    assert len(rows) == 2, f"_fetch_quotes_batch 应产出 2 行，实际 {len(rows)}: {rows}"
    r603887 = next((r for r in rows if r["security_code"] == "603887"), None)
    assert r603887 is not None, "603887 行丢失（_quote 把 list 当 dict 用了）"
    assert r603887["quote_sector_name"] == "IT服务Ⅱ", f"603887 的行业未带出: {r603887}"
    print("  [OK] Case 4: _fetch_quotes_batch 把 603887 的 quote_sector_name 带上")


def _test_build_position_analysis_per_code_fallback():
    """Case 5：stored sector_name=None 且 quote_sector_name=None 时，渲染前按 code 兜底查询。

    即使历史持仓录入时未带出行业、或实时批拉失败，页面也应通过 per-code lookup 兜底，
    而非永远显示「数据不足」。
    """
    # 屏蔽真实 streamlit 渲染
    from dashboard.views import portfolio as portfolio_page  # noqa: F401
    positions = pd.DataFrame([{
        "security_code": "603887",
        "security_name": "城地香江",
        "sector_name": None,
        "sector_code": None,
        "asset_type": "stock",
        "quantity": 100,
        "avg_cost": 13.0,
        "target_weight": None,
        "stop_loss": None,
    }])
    quotes = pd.DataFrame([{
        "security_code": "603887",
        "market_price": 9.30,
        "quote_name": "城地香江",
        "quote_sector_name": None,  # 实时批拉失败
        "quote_pct": -30.74,
        "quote_source": "tencent_realtime",
    }])
    states = pd.DataFrame([{
        "sector_code": "881271",
        "sector_name": "IT服务",
        "state": "⑤中性震荡",
        "trend": "neutral",
        "date": "2026-08-07",
    }])
    # 拦截 per-code lookup，模拟 eastmoney 现在能正常返回行业
    import portfolio.stock_lookup as stock_lookup
    stock_lookup.clear_cache()
    def _fake_lookup(code):
        return {"name": "城地香江", "price": 9.30, "sector_name": "IT服务Ⅱ", "asset_type": "stock"}
    # `from portfolio.stock_lookup import lookup_stock_info` 会把函数绑到 portfolio_page
    # 上自己的名字，必须 patch 目标模块里的别名，patch 源模块不影响。
    with mock.patch.object(portfolio_page, "lookup_stock_info", side_effect=_fake_lookup):
        out = portfolio_page._build_position_analysis(positions, quotes, states)
    row = out.iloc[0]
    assert row["sector_name"] in ("IT服务Ⅱ", "IT服务"), f"sector_name 未通过 per-code 兜底带出: {row['sector_name']!r}"
    assert row["sector_code"] == "881271", f"sector_code 未反查 881271: {row['sector_code']!r}"
    assert row["sector_state"] == "⑤中性震荡", f"板块状态未命中: {row['sector_state']!r}"
    assert row["priority"] != "数据不足", f"行业兜底后仍显示「数据不足」: {row['priority']}"
    print("  [OK] Case 5: per-code lookup 兜底让 603887 正确显示行业与板块状态")


# imports 在函数内（避免循环）
import pandas as pd  # noqa: E402


def main() -> int:
    print("== 实时批量快照 + 行业回退 自检 ==")
    failures = []
    for fn in (
        _test_quote_handles_batch_list,
        _test_quote_single_dict_still_works,
        _test_quote_empty_data_returns_empty,
        _test_fetch_quotes_batch_propagates_sector,
        _test_build_position_analysis_per_code_fallback,
    ):
        try:
            fn()
        except AssertionError as e:
            print(f"  [FAIL] {fn.__name__}: {e}")
            failures.append(fn.__name__)
        except Exception as e:
            print(f"  [ERR ] {fn.__name__}: {type(e).__name__}: {e}")
            failures.append(fn.__name__)
    if failures:
        print(f"\n结果：0 通过 / {len(failures)} 失败")
        return 1
    print(f"\n结果：5 通过 / 0 失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
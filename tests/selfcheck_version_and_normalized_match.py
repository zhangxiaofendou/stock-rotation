"""selfcheck_version_and_normalized_match.py

覆盖两个新增/加固点：
1) version_indicator: 子进程回退到 .git 文件读取、模块 mtime 兜底，绝不抛异常。
2) portfolio._build_position_analysis 的归一化名兜底：state_map 里有
   "name:IT服务"，持仓行带 sector_name="IT服务Ⅱ"（带 Ⅱ 后缀）也能命中，
   不再永远停在「数据不足」。
3) 真实状态机：881271 / 881160 在本地 state_snapshot 里有 state；模拟
   _build_position_analysis 的归一化索引 + 多级 state_map lookup，应
   找到 state，不会落进「数据不足」。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = []
FAIL = []


def _check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK  {name}")
    else:
        FAIL.append(f"{name}: {detail}")
        print(f"  XX  {name}  -- {detail}")


# ============================================================
# 1) version_indicator
# ============================================================
print("== version_indicator ==")

from dashboard.components.version_indicator import get_deploy_version, _read_git_file

info = get_deploy_version()
_check("get_deploy_version returns dict", isinstance(info, dict), repr(info))
_check("get_deploy_version has 'short'", isinstance(info.get("short"), str), repr(info))

# 直接测 _read_git_file：用 _find_repo_root 找出真正的仓库根（tests/ 向上找 .git）
from pathlib import Path
repo_root_path = Path(__file__).resolve().parent
from dashboard.components.version_indicator import _find_repo_root
found_root = _find_repo_root(repo_root_path)
_check("_find_repo_root finds actual repo root", found_root is not None and (found_root / ".git").exists(), repr(found_root))
git_pair = _read_git_file(found_root) if found_root else None
_check("_read_git_file returns (short, when) tuple", isinstance(git_pair, tuple) and len(git_pair) == 2, repr(git_pair))
if git_pair:
    sha_short = git_pair[0]
    _check("_read_git_file short sha is hex (7+ chars)", len(sha_short) >= 7 and all(c in "0123456789abcdef" for c in sha_short), repr(sha_short))


# ============================================================
# 2) _build_position_analysis 的归一化名兜底（fake streamlit）
# ============================================================
print("\n== _build_position_analysis 归一化兜底 ==")

# fake streamlit：完全 passthrough
import types
fake_st = types.ModuleType("streamlit")
fake_st.cache_data = lambda *a, **kw: (lambda fn: fn)
fake_st.cache_resource = lambda *a, **kw: (lambda fn: fn)
fake_st.session_state = {}
fake_st.columns = lambda n: [types.SimpleNamespace(__enter__=lambda s: s, __exit__=lambda *a: None, metric=lambda *a, **kw: None, dataframe=lambda *a, **kw: None, expander=lambda *a, **kw: types.SimpleNamespace(__enter__=lambda s: s, __exit__=lambda *a: None), caption=lambda *a, **kw: None, button=lambda *a, **kw: False, selectbox=lambda *a, **kw: None, text_input=lambda *a, **kw: "", number_input=lambda *a, **kw: 0.0, form=lambda *a, **kw: types.SimpleNamespace(__enter__=lambda s: s, __exit__=lambda *a: None, form_submit_button=lambda *a, **kw: False), warning=lambda *a, **kw: None, error=lambda *a, **kw: None, success=lambda *a, **kw: None, info=lambda *a, **kw: None) for _ in range(int(n) if isinstance(n, int) else len(n))]
fake_st.markdown = lambda *a, **kw: None
fake_st.dataframe = lambda *a, **kw: None
fake_st.metric = lambda *a, **kw: None
fake_st.expander = lambda *a, **kw: types.SimpleNamespace(__enter__=lambda s: s, __exit__=lambda *a: None)
fake_st.caption = lambda *a, **kw: None
fake_st.subheader = lambda *a, **kw: None
fake_st.title = lambda *a, **kw: None
fake_st.info = lambda *a, **kw: None
fake_st.warning = lambda *a, **kw: None
fake_st.error = lambda *a, **kw: None
fake_st.success = lambda *a, **kw: None
fake_st.rerun = lambda: None
fake_st.button = lambda *a, **kw: False
fake_st.selectbox = lambda *a, **kw: None
fake_st.text_input = lambda *a, **kw: ""
fake_st.number_input = lambda *a, **kw: 0.0
fake_st.form = lambda *a, **kw: types.SimpleNamespace(__enter__=lambda s: s, __exit__=lambda *a: None)
fake_st.form_submit_button = lambda *a, **kw: False
fake_st.fragment = lambda *a, **kw: (lambda fn: fn)
fake_st.plotly_chart = lambda *a, **kw: None
fake_st.set_page_config = lambda *a, **kw: None
fake_st.spinner = lambda *a, **kw: types.SimpleNamespace(__enter__=lambda s: s, __exit__=lambda *a: None)
fake_st.stop = lambda: None
fake_st.write = lambda *a, **kw: None
fake_st.code = lambda *a, **kw: None
fake_st.balloons = lambda: None
fake_st.snow = lambda: None
fake_st.toast = lambda *a, **kw: None
fake_st.empty = lambda: None
fake_st.container = lambda: types.SimpleNamespace()
fake_st.sidebar = types.SimpleNamespace(expander=lambda *a, **kw: types.SimpleNamespace(__enter__=lambda s: s, __exit__=lambda *a: None, markdown=lambda *a, **kw: None, caption=lambda *a, **kw: None))
fake_st.warning = lambda *a, **kw: None

sys.modules["streamlit"] = fake_st

# 重新加载 portfolio（确保 fake_st 生效）
import importlib
if "dashboard.pages.portfolio" in sys.modules:
    importlib.reload(sys.modules["dashboard.pages.portfolio"])
portfolio_page = importlib.import_module("dashboard.pages.portfolio")

import pandas as pd

# 构造场景 A：state_map 里有 "name:IT服务"，持仓 sector_name="IT服务Ⅱ"（带 Ⅱ）
states = pd.DataFrame([
    {"sector_code": "881271", "sector_name": "IT服务", "state": "④强转弱", "trend": "下行", "date": "2026-08-07"},
    {"sector_code": "881160", "sector_name": "旅游及酒店", "state": "④强转弱", "trend": "下行", "date": "2026-08-07"},
])

# 场景 1：持仓 sector_name="IT服务Ⅱ"（带 Ⅱ）→ 应通过归一化命中 name_index
positions = pd.DataFrame([
    {"security_code": "603887", "security_name": "城地香江", "asset_type": "stock",
     "sector_code": "", "sector_name": "IT服务Ⅱ", "quantity": 100, "avg_cost": 13.6, "stop_loss": 8.0},
    {"security_code": "159766", "security_name": "旅游ETF", "asset_type": "etf",
     "sector_code": "", "sector_name": "旅游", "quantity": 100, "avg_cost": 0.6, "stop_loss": None},
])
quotes = pd.DataFrame([
    {"security_code": "603887", "market_price": 9.33, "quote_sector_name": "IT服务Ⅱ", "quote_pct": -31.0, "quote_source": "em"},
    {"security_code": "159766", "market_price": 0.57, "quote_sector_name": "旅游", "quote_pct": -1.4, "quote_source": "em"},
])

# 截断外网：用 patch 让 lookup_stock_info 立刻返回 sector_name=None（模拟网络失败）
from portfolio import stock_lookup
stock_lookup.clear_cache()
import unittest.mock as mock
def _lookup_fail(code):
    return {"name": "x", "price": 1.0, "sector_name": None, "asset_type": "stock"}
# 同时让 resolve_sector 只返回板块组（不返回 code），逼出 name 归一化兜底路径
def _resolve_no_code(sn):
    return (None, sn, "其他")
with mock.patch.object(stock_lookup, "lookup_stock_info", side_effect=_lookup_fail), \
     mock.patch.object(portfolio_page, "resolve_sector", side_effect=_resolve_no_code):
    out = portfolio_page._build_position_analysis(positions, quotes, states)

_check("IT服务Ⅱ 行 sector_state 不是「—」", out.iloc[0]["sector_state"] != "—", f"got {out.iloc[0]['sector_state']!r}")
_check("IT服务Ⅱ 行 priority 不是「数据不足」", out.iloc[0]["priority"] != "数据不足", f"got {out.iloc[0]['priority']!r}")
# sector_code 在该测试场景下刻意被 mock 成空，验的是「板块状态能被反查到」即可
_check("IT服务Ⅱ 行 sector_state 命中 ④强转弱", out.iloc[0]["sector_state"] == "④强转弱", f"got {out.iloc[0]['sector_state']!r}")

_check("旅游 行 sector_state 不是「—」", out.iloc[1]["sector_state"] != "—", f"got {out.iloc[1]['sector_state']!r}")
_check("旅游 行 priority 不是「数据不足」", out.iloc[1]["priority"] != "数据不足", f"got {out.iloc[1]['priority']!r}")
_check("旅游 行 sector_state 命中 ④强转弱", out.iloc[1]["sector_state"] == "④强转弱", f"got {out.iloc[1]['sector_state']!r}")

# 场景 2：用户事先手动存了 sector_code=881271（理想路径），quote_sector_name=None
positions2 = pd.DataFrame([
    {"security_code": "603887", "security_name": "城地香江", "asset_type": "stock",
     "sector_code": "881271", "sector_name": "IT服务Ⅱ", "quantity": 100, "avg_cost": 13.6, "stop_loss": 8.0},
])
quotes2 = pd.DataFrame([
    {"security_code": "603887", "market_price": 9.33, "quote_sector_name": None, "quote_pct": -31.0, "quote_source": "em"},
])
out2 = portfolio_page._build_position_analysis(positions2, quotes2, states)
_check("已存 sector_code 时按 code 命中", out2.iloc[0]["sector_state"] == "④强转弱", f"got {out2.iloc[0]['sector_state']!r}")

# 场景 3：state_map 兜底（不带归一化也没事，因为 code 路径已命中）
states_partial = states[states["sector_code"].isin(["881271"])]  # 只留 881271
positions3 = positions.copy()
quotes3 = quotes.copy()
out3 = portfolio_page._build_position_analysis(positions3, quotes3, states_partial)
_check("881271 行依旧命中（只剩一个 sector 也能算）", out3.iloc[0]["sector_state"] == "④强转弱", f"got {out3.iloc[0]['sector_state']!r}")


# ============================================================
# 总结
# ============================================================
print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
if FAIL:
    for f in FAIL:
        print(f"  FAIL: {f}")
    sys.exit(1)
sys.exit(0)
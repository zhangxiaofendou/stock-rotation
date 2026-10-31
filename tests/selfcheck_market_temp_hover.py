"""市场温度计环形图悬停行业列表 防假绿测试。

需求：鼠标悬浮九宫格状态分布饼图扇区时，显示该状态下的行业（板块）列表。

验证方式（内存 exec，不改工作区文件）：
1. 用「移除 custom_data/hovertemplate」的源码变体 = 修复前实现 ->
   饼图 trace 无 customdata，断言 FAIL（复现缺失）。
2. 用当前工作区源码 = 修复后实现 ->
   饼图 trace customdata 必须包含各状态全部行业名称（换行拼好的文本）。
"""
import os
import re
import sys
import types
from unittest.mock import MagicMock

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "dashboard", "views", "market_temp.py")

_STUB = None

# 修复时新增的两段（用于构造"修复前"变体）
NEW_DIST_BLOCK = '''            # 悬停用行业列表：代码→名称，每行 6 个避免 tooltip 过宽溢出
            names = [get_sector_name(c) for c in codes]
            sectors_text = "<br>".join(
                "、".join(names[i:i + 6]) for i in range(0, len(names), 6)
            ) if names else "—"
'''
NEW_HOVER_BLOCK = '''            custom_data=["sectors"],
'''
NEW_SECTORS_LINE = '''                "sectors": sectors_text,
'''


def install_streamlit_stub():
    st = MagicMock()
    st.cache_data = lambda *a, **k: (lambda f: f)
    st.cache_resource = lambda *a, **k: (lambda f: f)

    def columns(spec):
        n = spec if isinstance(spec, int) else len(spec)
        return [MagicMock() for _ in range(n)]

    st.columns = columns

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    st.spinner = lambda *a, **k: _Ctx()
    st.expander = lambda *a, **k: _Ctx()
    sys.modules["streamlit"] = st
    return st


def load_variant(strip_hover=False):
    with open(TARGET, encoding="utf-8") as f:
        src = f.read()
    if strip_hover:
        assert NEW_DIST_BLOCK in src, "锚点1不存在，脚本过期"
        assert NEW_HOVER_BLOCK in src, "锚点2不存在，脚本过期"
        assert NEW_SECTORS_LINE in src, "锚点3不存在，脚本过期"
        src = src.replace(NEW_DIST_BLOCK, "", 1)
        src = src.replace(NEW_HOVER_BLOCK, "", 1)
        src = src.replace(NEW_SECTORS_LINE, "", 1)
    mod = types.ModuleType("market_temp_hover_under_test")
    mod.__file__ = TARGET
    exec(compile(src, TARGET, "exec"), mod.__dict__)
    return mod


def pie_customdata(mod):
    """跑 _render_market_overview，返回饼图 trace 的 customdata（无则 None）。"""
    state_df = pd.DataFrame(
        {"date": ["2026-10-31"] * 2, "sector_code": ["881101", "881102"]}
    )
    state_dist = {
        "⑦持续杀跌": ["881101", "881102", "881103"],
        "⑨底背离": ["881104"],
    }
    group_stats = {}
    mod.load_market_data = lambda: (
        {"mode": "normal", "reason": "测试"}, state_df, state_dist,
        20, 10, 5, group_stats,
    )
    mod.render_src_badge = lambda *a, **k: None
    mod.get_sector_name = lambda c: f"行业{c}"
    mod._render_market_overview()

    figs = [c.args[0] for c in _STUB.plotly_chart.call_args_list
            if c.args and hasattr(c.args[0], "data")]
    for fig in figs:
        for tr in fig.data:
            if tr.type == "pie":
                return getattr(tr, "customdata", None)
    return None


def main():
    global _STUB
    _STUB = install_streamlit_stub()
    fails = 0

    print("[1/2] 修复前变体（无 custom_data）悬停应不含行业名 ...")
    old_cd = pie_customdata(load_variant(strip_hover=True))
    old_text = "" if old_cd is None else " ".join(
        str(v) for row in old_cd for v in (row if hasattr(row, "__iter__") else [row])
    )
    if "行业" not in old_text:
        print("      OK: 复现成功 -> 旧实现饼图悬停无行业列表")
    else:
        print("      FAIL: 修复前居然已含行业名，测试无效")
        fails += 1

    print("[2/2] 当前实现：悬停 customdata 应含全部行业名称 ...")
    _STUB.reset_mock()  # 清掉第1步的调用记录，避免捞到旧变体的饼图
    cd = pie_customdata(load_variant())
    if cd is None:
        print("      FAIL: 饼图 trace 无 customdata")
        fails += 1
    else:
        # customdata 每行可能是 [行业列表, 标签] 多列，取含"行业"前缀的那列
        joined = " ".join(str(v) for row in cd for v in (row if hasattr(row, "__iter__") else [row]))
        need = ["行业881101", "行业881102", "行业881103", "行业881104"]
        missing = [n for n in need if n not in joined]
        if missing:
            print(f"      FAIL: customdata 缺少行业名: {missing}")
            fails += 1
        else:
            print(f"      OK: 通过，{len(cd)} 个扇区均携带行业列表（换行文本）")

    print("结果：全部通过" if fails == 0 else f"结果：{2 - fails} 通过 / {fails} 失败")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

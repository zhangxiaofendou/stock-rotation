"""涨跌分布柱状图「鼠标悬浮显示板块列表」防假绿测试。

需求：悬浮 上涨/横盘/下跌 柱子时，tooltip 列出该方向下的具体板块名 + 当日涨跌幅。

验证方式（内存 exec 源码变体，不改工作区文件）：
1. 去掉 customdata/hovertemplate 的「修复前」变体 -> 柱图 trace 无板块名，断言 FAIL（复现缺失）；
2. 当前工作区源码 -> 柱图 customdata 必须含各分类全部板块名与涨跌幅；
3. build_breadth_hover_text 纯函数：每行 6 个、HTML 转义、空列表兜底；
4. 兼容旧 7 元组 load_market_data（历史单测 mock）不报错。
"""
import os
import sys
import types
from unittest.mock import MagicMock

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "dashboard", "views", "market_temp.py")

# 修复时给柱状图新增的两段（用于构造「修复前」变体）
NEW_CUSTOMDATA = '''            customdata=[[t] for t in hover_texts],
            hovertemplate=(
                "<b>%{x}板块：%{y} 个</b><br>"
                "%{customdata[0]}<extra></extra>"
            ),
'''

_STUB = None
BREADTH = {
    "上涨": [("光伏设备", 8.02, "881279"), ("贵金属", 4.95, "881168")],
    "横盘": [("银行", 0.0, "881155")],
    "下跌": [("煤炭开采加工", -1.38, "881105")],
}


def install_streamlit_stub():
    st = MagicMock()
    st.cache_data = lambda *a, **k: (lambda f: f)
    st.cache_resource = lambda *a, **k: (lambda f: f)
    st.columns = lambda spec: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]

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
        assert NEW_CUSTOMDATA in src, "锚点不存在，脚本过期（柱状图 customdata 段已被改动）"
        src = src.replace(NEW_CUSTOMDATA, "", 1)
    mod = types.ModuleType("breadth_bar_under_test")
    mod.__file__ = TARGET
    exec(compile(src, TARGET, "exec"), mod.__dict__)
    return mod


def run_overview(mod, eight=True):
    state_df = pd.DataFrame(
        {"date": ["2026-11-05"], "sector_code": ["881101"], "sector_name": ["测试板块"]}
    )
    state_dist = {"⑤中性震荡": ["881101"]}
    base = ({"mode": "normal", "reason": "测试"}, state_df, state_dist,
            2, 1, 1, {})
    mod.load_market_data = lambda: base + (BREADTH,) if eight else base
    mod.render_src_badge = lambda *a, **k: None
    mod._render_market_overview()


def bar_customdata(stub):
    """从所有 plotly_chart 调用里找出「涨跌分布」柱图，返回其 customdata。"""
    for call in stub.plotly_chart.call_args_list:
        fig = call.args[0] if call.args else None
        if not hasattr(fig, "data"):
            continue
        for tr in fig.data:
            if tr.type != "bar":
                continue
            xs = [str(v) for v in (tr.x if tr.x is not None else [])]
            if "上涨" in xs and "横盘" in xs and "下跌" in xs:
                return getattr(tr, "customdata", None)
    return None


def flatten(cd):
    if cd is None:
        return ""
    out = []
    for row in cd:
        vals = row if hasattr(row, "__iter__") and not isinstance(row, str) else [row]
        out.extend(str(v) for v in vals)
    return " ".join(out)


def main():
    global _STUB
    fails = 0
    _STUB = install_streamlit_stub()

    print("[1/4] 修复前变体（无 customdata）悬浮应不含板块名 ...")
    run_overview(load_variant(strip_hover=True))
    old_text = flatten(bar_customdata(_STUB))
    if "光伏设备" not in old_text:
        print("      OK: 复现成功 -> 旧实现柱图悬浮无板块列表")
    else:
        print("      FAIL: 修复前已含板块名，测试无效")
        fails += 1

    print("[2/4] 当前实现：悬浮 customdata 应含各分类全部板块名与涨跌幅 ...")
    _STUB.reset_mock()
    run_overview(load_variant())
    cd = bar_customdata(_STUB)
    new_text = flatten(cd)
    need = ["光伏设备", "+8.02%", "贵金属", "+4.95%", "银行", "+0.00%",
            "煤炭开采加工", "-1.38%"]
    missing = [n for n in need if n not in new_text]
    if cd is None:
        print("      FAIL: 柱图 trace 无 customdata")
        fails += 1
    elif missing:
        print(f"      FAIL: customdata 缺少: {missing}")
        fails += 1
    else:
        print(f"      OK: 通过，3 根柱子均携带板块列表（{len(cd)} 项）")

    print("[3/4] build_breadth_hover_text：换行/转义/空列表兜底 ...")
    mod = load_variant()
    txt = mod.build_breadth_hover_text(
        [(f"板块{i}", i / 10, "x") for i in range(8)], per_line=6)
    assert txt.count("<br>") == 1, "每行 6 个时应恰好换 1 次行"
    esc = mod.build_breadth_hover_text([("A&B<c>", 1.0, "x")])
    assert "&amp;" in esc and "&lt;" in esc, "HTML 转义失效"
    assert mod.build_breadth_hover_text([]) == "—", "空列表兜底失效"
    print("      OK: 换行=1、转义生效、空列表返回 —")

    print("[4/4] 兼容旧 7 元组 load_market_data（历史单测 mock）...")
    _STUB.reset_mock()
    try:
        run_overview(load_variant(), eight=False)
        print("      OK: 7 元组不报错，hover 走空列表兜底")
    except Exception as exc:
        print(f"      FAIL: 7 元组兼容失败 -> {exc}")
        fails += 1

    print("结果：全部通过" if fails == 0 else f"结果：{4 - fails} 通过 / {fails} 失败")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

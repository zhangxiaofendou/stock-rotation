"""运行时验证 market_temp.py 的 px 未定义问题（复现 -> 修复）。

验证方式：
1. 用「移除新增 px import 行」的源码变体 = 修复前实现 -> 必须抛 NameError
2. 用当前工作区源码 = 修复后实现 -> 必须正常执行到 px.pie 并返回
两个变体都在内存里 exec，不修改工作区文件。

这个脚本的价值：防止将来有人再把 px 的懒加载 import 挪走/删掉。
"""
import os
import sys
import types
from unittest.mock import MagicMock

import pandas as pd

TARGET = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "dashboard", "views", "market_temp.py",
)

# 修复时新增的那行（用于构造"修复前"变体）
NEW_PX_LINE = (
    "    import plotly.express as px        "
    "# 懒加载:饼图 px.pie 用,必须在本函数作用域内导入\n"
)


def install_streamlit_stub():
    """把 streamlit 换成可控桩，保留 cache_data/cache_resource 的装饰器语义。"""
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


def load_variant(drop_new_line=False):
    """加载 market_temp 模块；drop_new_line=True 时还原成修复前源码。"""
    with open(TARGET, encoding="utf-8") as f:
        src = f.read()
    if drop_new_line:
        assert NEW_PX_LINE in src, "锚点行不存在，脚本已过期，请更新 NEW_PX_LINE"
        src = src.replace(NEW_PX_LINE, "", 1)
    mod = types.ModuleType("market_temp_under_test")
    mod.__file__ = TARGET
    exec(compile(src, TARGET, "exec"), mod.__dict__)
    return mod


def fake_market_data(mod):
    state_df = pd.DataFrame({"date": ["2026-10-11"], "sector_code": ["881101"]})
    state_dist = {s: ["881101"] for s in mod.ALL_STATES[:3]}
    group_stats = {"消费": {"total": 10, "buy_count": 3, "sell_count": 2}}
    return ({"mode": "normal", "reason": "测试"}, state_df, state_dist,
            20, 10, 5, group_stats)


def run(mod):
    mod.load_market_data = lambda: fake_market_data(mod)
    mod.render_src_badge = lambda *a, **k: None
    mod._render_market_overview()


def main():
    install_streamlit_stub()

    print("[1/2] 修复前变体（无 px import）应抛 NameError ...")
    old_mod = load_variant(drop_new_line=True)
    try:
        run(old_mod)
    except NameError as e:
        print(f"      OK: 复现成功 -> NameError: {e}")
    else:
        print("      FAIL: 修复前居然没报错，测试无效（可能锚点行已失效）")
        return 1

    print("[2/2] 当前实现应正常执行 ...")
    new_mod = load_variant()
    try:
        run(new_mod)
    except Exception as e:
        print(f"      FAIL: 仍然报错 -> {type(e).__name__}: {e}")
        return 1
    print("      OK: 通过，px.pie 正常渲染")
    return 0


if __name__ == "__main__":
    sys.exit(main())

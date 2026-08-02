"""登录页与自检面板的渲染冒烟测试。

用 mock 替换 streamlit，真实执行渲染函数，确保用户点下去不会白屏或报错。
纯逻辑测试永远发现不了「页面一点就崩」这类问题，因此必须补这一层。
"""

import os
import sys
import tempfile
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 隔离数据目录，避免污染真实凭证
os.environ["PERSISTENT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="uismoke_")
os.environ.pop("DATABASE_URL", None)

_passed = 0
_failed = 0


def check(name, fn):
    global _passed, _failed
    try:
        fn()
        _passed += 1
        print(f"  [OK]   {name}")
    except Exception as e:
        _failed += 1
        print(f"  [FAIL] {name} -> {type(e).__name__}: {e}")


class QueryParams(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def build_st():
    st = mock.MagicMock()
    st.session_state = {}
    st.query_params = QueryParams()
    st.columns.side_effect = lambda spec, **kw: [
        mock.MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))
    ]
    st.tabs.side_effect = lambda labels, **kw: [mock.MagicMock() for _ in labels]
    st.secrets = {}
    return st


print("=" * 62)
print("  登录页 / 自检面板 渲染冒烟")
print("=" * 62)

st_mock = build_st()
sys.modules["streamlit"] = st_mock
import auth  # noqa: E402

print("\n[1] 自检面板")


def _panel_not_clicked():
    st_mock.button.return_value = False
    auth.render_diagnostics_panel()


def _panel_clicked_main():
    st_mock.button.return_value = True
    auth.render_diagnostics_panel(in_sidebar=False)


def _panel_clicked_sidebar():
    st_mock.button.return_value = True
    auth.render_diagnostics_panel(in_sidebar=True)


check("未点击时不执行检查", _panel_not_clicked)
check("点击后主区域完整渲染", _panel_clicked_main)
check("点击后侧边栏完整渲染", _panel_clicked_sidebar)

print("\n[2] 报告内容正确性")


def _report_content():
    import diagnostics
    rep = diagnostics.run_all()
    text = diagnostics.format_text(rep)
    assert "诊断报告" in text, "缺少标题"
    assert "结论：" in text, "缺少结论"
    assert rep.counts()["PASS"] > 0, "无任何通过项，诊断器可能失效"
    # 报告不得泄露任何密码原文
    assert "pwd_hash" not in text
    for chk in rep.checks:
        assert chk.status in ("PASS", "WARN", "FAIL", "SKIP"), f"非法状态 {chk.status}"
        if chk.status == "FAIL":
            assert chk.fix or chk.detail, f"{chk.name} 失败却没给出任何说明"


check("报告结构与状态合法", _report_content)

print("\n[3] 登录页渲染")


def _render_login_page():
    st_mock.button.return_value = False
    st_mock.form_submit_button.return_value = False
    auth._render_auth()


def _render_login_page_submitted():
    # 模拟点了登录按钮但账号不存在：应走到 st.error，不得抛异常
    st_mock.button.return_value = False
    st_mock.form_submit_button.return_value = True
    st_mock.text_input.return_value = "nobody"
    auth._render_auth()


check("空表单渲染", _render_login_page)
check("提交不存在的账号不崩溃", _render_login_page_submitted)

print("\n[4] 守卫行为")


def _guard_blocks_anonymous():
    st_mock.session_state.clear()
    st_mock.query_params.clear()
    st_mock.button.return_value = False
    st_mock.form_submit_button.return_value = False
    assert auth.guard() is None, "未登录时守卫必须返回 None"


def _guard_allows_logged_in():
    auth.register("smokeuser", "smokepass123")
    ok, token = auth.login("smokeuser", "smokepass123")
    assert ok, token
    st_mock.session_state.clear()
    st_mock.query_params.clear()
    auth.set_session(token)
    assert auth.guard() == "smokeuser", "已登录用户应被放行"


check("未登录被拦截", _guard_blocks_anonymous)
check("已登录被放行", _guard_allows_logged_in)

print("\n" + "=" * 62)
print(f"  结果：{_passed} 通过 / {_failed} 失败")
print("=" * 62)
sys.exit(1 if _failed else 0)

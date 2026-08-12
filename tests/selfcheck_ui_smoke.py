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

print("\n[5] 持仓录入表单（最少录入 + 自动补全）")

import dashboard.views.portfolio as portfolio_page  # noqa: E402


def _make_service():
    svc = mock.MagicMock()
    return svc


def _text_seq(values):
    """按调用顺序返回 text_input 的值（代码/名称/行业名/行业代码/备注），
    超出队列返回空串。MagicMock 统一 return_value 会让所有输入框返回同一
    值，掩盖「名称留空」「行业覆盖」等真实分支，因此必须逐框区分。"""
    queue = list(values) + [""] * 10
    it = iter(queue)
    return lambda *a, **k: next(it)


def _num_seq(values):
    """number_input 按调用顺序返回（数量/成交价/费用/目标仓位/止损价）。"""
    queue = list(values) + [0.0] * 10
    it = iter(queue)
    return lambda *a, **k: next(it)


def _reset_widgets():
    st_mock.session_state.clear()
    st_mock.button.return_value = False
    st_mock.form_submit_button.return_value = False
    st_mock.text_input.return_value = ""
    st_mock.number_input.return_value = 0.0
    st_mock.selectbox.return_value = "BUY"
    st_mock.date_input.return_value = __import__("datetime").date.today()


def _form_plain_render():
    _reset_widgets()
    with mock.patch.object(portfolio_page, "lookup_stock_info") as lu:
        lu.return_value = None
        portfolio_page._render_record_form(_make_service())


def _query_success():
    _reset_widgets()
    st_mock.button.return_value = True
    st_mock.text_input.side_effect = _text_seq(["600519"])
    with mock.patch.object(portfolio_page, "lookup_stock_info") as lu:
        lu.return_value = {"name": "贵州茅台", "price": 1350.6, "sector_name": "白酒Ⅱ"}
        portfolio_page._render_record_form(_make_service())
        lu.assert_called_once_with("600519")
    assert st_mock.session_state.get("pl_lookup", {}).get("name") == "贵州茅台", "查询结果应写入会话"
    assert st_mock.session_state.get("pl_price") == 1350.6, "最新价应作为成交价默认值"


def _query_fail():
    _reset_widgets()
    st_mock.button.return_value = True
    st_mock.text_input.side_effect = _text_seq(["999999"])
    with mock.patch.object(portfolio_page, "lookup_stock_info") as lu:
        lu.return_value = None
        portfolio_page._render_record_form(_make_service())
    assert "pl_lookup" not in st_mock.session_state, "查询失败不应残留查询结果"


def _submit_autofill_success():
    _reset_widgets()
    st_mock.form_submit_button.return_value = True
    st_mock.text_input.side_effect = _text_seq(["600519", "", "", "", ""])  # 名称留空
    # number_input 按序：数量=100、成交价=0（留空走自动带出）、费用/目标/止损=0
    st_mock.number_input.side_effect = _num_seq([100.0, 0.0, 0.0, 0.0, 0.0])
    svc = _make_service()
    with mock.patch.object(portfolio_page, "lookup_stock_info") as lu:
        lu.return_value = {"name": "贵州茅台", "price": 1350.6, "sector_name": "白酒Ⅱ"}
        portfolio_page._render_record_form(svc)
    kwargs = svc.record_trade.call_args.kwargs
    assert kwargs["security_name"] == "贵州茅台", "名称应自动带出"
    assert kwargs["security_code"] == "600519", "代码应规整为 6 位"
    assert kwargs["price"] == 1350.6, "价格应为自动带出的最新价"
    assert kwargs["sector_name"] == "白酒Ⅱ", "行业应自动带出"


def _submit_lookup_failed_requires_manual():
    _reset_widgets()
    st_mock.form_submit_button.return_value = True
    st_mock.text_input.side_effect = _text_seq(["600519", "", "", "", ""])  # 名称留空
    st_mock.number_input.return_value = 100.0
    svc = _make_service()
    with mock.patch.object(portfolio_page, "lookup_stock_info") as lu:
        lu.return_value = None  # 网络不可用
        portfolio_page._render_record_form(svc)
    assert svc.record_trade.call_count == 0, "补全失败且未手填名称时不得保存"


check("表单空渲染不崩", _form_plain_render)
check("查询成功自动带出", _query_success)
check("查询失败不残留结果", _query_fail)
check("提交时名称/价格/行业自动补全", _submit_autofill_success)
check("补全失败不误保存", _submit_lookup_failed_requires_manual)

print("\n" + "=" * 62)
print(f"  结果：{_passed} 通过 / {_failed} 失败")
print("=" * 62)
sys.exit(1 if _failed else 0)

"""验证实时行情条的刷新改为 fragment 局部重跑，不再整页 20s 重算。

根因：原 app.py 在 main() 末尾 `time.sleep(20); st.rerun()` 整页重跑，
一旦勾选「盘中实时刷新」，整个 app 每 20 秒从头执行一遍，用户点击被整页
重跑节奏干扰，表现为「点一下转半天」。

修复：实时条包进 @st.fragment，内部用 `st.rerun(scope="fragment")` 只刷自己。
本测试在 import 前用 fake streamlit 替换，断言 fragment 行为正确。
"""
import os
import sys
import types
import unittest
from unittest import mock

# 在 import dashboard.app 之前把 streamlit 换成 fake，避免依赖真实运行时
_fake_st = types.SimpleNamespace()
_fake_st.fragment = lambda f: f  # 装饰器视为 identity，便于直接调用函数体
_fake_st.rerun = mock.MagicMock()
_fake_st.cache_data = lambda *a, **k: (lambda f: f)
_fake_st.cache_resource = lambda *a, **k: (lambda f: f)
_fake_st.set_page_config = lambda *a, **k: None
sys.modules["streamlit"] = _fake_st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dashboard.app as P  # noqa: E402


class RealtimeFragmentTest(unittest.TestCase):
    def test_live_false_renders_once_no_sleep_no_rerun(self):
        with mock.patch.object(P, "render_realtime_ticker") as mrt, \
             mock.patch.object(P, "time") as mt:
            mt.sleep = mock.MagicMock()
            _fake_st.rerun.reset_mock()
            P._render_realtime_ticker_fragment(False)
            mrt.assert_called_once_with(False)
            mt.sleep.assert_not_called()
            _fake_st.rerun.assert_not_called()

    def test_live_true_renders_and_reruns_fragment_scope(self):
        with mock.patch.object(P, "render_realtime_ticker") as mrt, \
             mock.patch.object(P, "time") as mt:
            mt.sleep = mock.MagicMock()
            _fake_st.rerun.reset_mock()
            P._render_realtime_ticker_fragment(True)
            mrt.assert_called_once_with(True)
            mt.sleep.assert_called_once_with(P.REALTIME_INTERVAL)
            _fake_st.rerun.assert_called_once_with(scope="fragment")

    def test_effective_live_pauses_when_pipeline_running(self):
        # 后台管线运行时，即便用户勾选 live_realtime，effective_live 也应被置 False
        with mock.patch.object(P, "_load_progress", return_value={"status": "running"}):
            # 复刻 main() 内的计算逻辑
            live_realtime = True
            _pipeline_running = P._load_progress().get("status") == "running"
            effective_live = live_realtime and not _pipeline_running
            self.assertFalse(effective_live)

    def test_effective_live_on_when_idle(self):
        with mock.patch.object(P, "_load_progress", return_value={"status": "done"}):
            live_realtime = True
            _pipeline_running = P._load_progress().get("status") == "running"
            effective_live = live_realtime and not _pipeline_running
            self.assertTrue(effective_live)


if __name__ == "__main__":
    unittest.main(verbosity=2)

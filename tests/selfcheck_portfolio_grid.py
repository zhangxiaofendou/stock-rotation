"""
自检：持仓页「行业九宫格分布 + 状态变化轨迹」
============================================
在 import 前用 fake streamlit 替换，断言 UI 层的关键行为：

  1. 空持仓 → 走提示分支，不画图
  2. 散点按「状态标签」定位到正确格子（而非按 RS 分位反推）
  3. 缺行业状态的标的不入图，并单独 warning 列出
  4. 同格多标的的错位偏移是确定性的（刷新不跳位）且不超出格子边界
  5. 状态变化表按行业去重（同行业两只标的只出一行，持有标的合并）
  6. 甘特时间线的色块数 = 状态段总数，且时间轴用日期基准
  7. 行业无历史数据时降级为「暂无历史状态数据」而不是崩溃
"""
import os
import sys
import types
import unittest
from unittest import mock

import pandas as pd


def _passthrough(*args, **kwargs):
    """同时兼容 @st.cache_resource 和 @st.cache_data(ttl=...) 两种写法。"""
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return lambda f: f


_fake_st = mock.MagicMock()
_fake_st.cache_data = _passthrough
_fake_st.cache_resource = _passthrough
_fake_st.fragment = lambda f: f
sys.modules["streamlit"] = _fake_st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dashboard.pages.portfolio as P  # noqa: E402
from model.state_history import STATE_CELL  # noqa: E402


def _analysis(rows):
    return pd.DataFrame(rows)


def _series(pairs):
    return pd.DataFrame({"date": [p[0] for p in pairs], "state": [p[1] for p in pairs]})


def _last_figure():
    """取最后一次 st.plotly_chart 传入的 figure。"""
    calls = _fake_st.plotly_chart.call_args_list
    assert calls, "应调用 st.plotly_chart 绘图"
    return calls[-1].args[0]


class PortfolioGridTest(unittest.TestCase):
    def setUp(self):
        _fake_st.reset_mock()
        self._badge = mock.patch.object(P, "render_src_badge")
        self._badge.start()

    def tearDown(self):
        self._badge.stop()

    # --------------------------------------------------------
    def test_empty_analysis_shows_info_no_chart(self):
        P._render_portfolio_grid(pd.DataFrame())
        _fake_st.info.assert_called()
        _fake_st.plotly_chart.assert_not_called()

    def test_points_placed_by_state_label(self):
        """点必须落在与其状态标签一致的格子里。"""
        df = _analysis([
            {"security_code": "600000", "security_name": "浦发银行", "sector_name": "银行",
             "sector_code": "881155", "sector_state": "⑥弱转强",
             "market_value": 10000.0, "profit_pct": 5.0},
            {"security_code": "601888", "security_name": "中国中免", "sector_name": "旅游及酒店",
             "sector_code": "881160", "sector_state": "⑦持续杀跌",
             "market_value": 20000.0, "profit_pct": -12.0},
        ])
        P._render_portfolio_grid(df)
        fig = _last_figure()
        scatters = [t for t in fig.data if t.type == "scatter"]
        self.assertEqual(len(scatters), 1, "应只有一条散点 trace")
        xs, ys = list(scatters[0].x), list(scatters[0].y)
        self.assertEqual(len(xs), 2, f"应有 2 个持仓点，实际 {len(xs)}")

        # 单只标的独占一格 → 偏移为 0，坐标应精确等于格心
        want = {STATE_CELL["⑥弱转强"], STATE_CELL["⑦持续杀跌"]}
        got = {(round(x), round(y)) for x, y in zip(xs, ys)}
        self.assertEqual(got, want, f"点应落在 {want}，实际 {got}")
        for x, y in zip(xs, ys):
            self.assertAlmostEqual(x, round(x), places=6, msg="单点应无偏移")
            self.assertAlmostEqual(y, round(y), places=6, msg="单点应无偏移")

    def test_profit_loss_colors_follow_cn_convention(self):
        """A 股习惯：浮盈红、浮亏绿。"""
        df = _analysis([
            {"security_code": "A", "security_name": "盈利股", "sector_name": "银行",
             "sector_code": "881155", "sector_state": "②稳健上行",
             "market_value": 100.0, "profit_pct": 8.0},
            {"security_code": "B", "security_name": "亏损股", "sector_name": "银行",
             "sector_code": "881155", "sector_state": "②稳健上行",
             "market_value": 100.0, "profit_pct": -8.0},
        ])
        P._render_portfolio_grid(df)
        colors = list(_last_figure().data[0].marker.color)
        self.assertEqual(colors[0], "#e23c3c", "浮盈应为红色")
        self.assertEqual(colors[1], "#16a34a", "浮亏应为绿色")

    def test_missing_state_excluded_and_warned(self):
        df = _analysis([
            {"security_code": "600000", "security_name": "浦发银行", "sector_name": "银行",
             "sector_code": "881155", "sector_state": "⑥弱转强",
             "market_value": 10000.0, "profit_pct": 1.0},
            {"security_code": "000001", "security_name": "无映射股", "sector_name": "",
             "sector_code": "", "sector_state": "—",
             "market_value": 5000.0, "profit_pct": -3.0},
        ])
        P._render_portfolio_grid(df)
        xs = list(_last_figure().data[0].x)
        self.assertEqual(len(xs), 1, "无状态的标的不应入图")
        _fake_st.warning.assert_called()
        msg = _fake_st.warning.call_args.args[0]
        self.assertIn("无映射股", msg, "warning 应点名缺失的标的")

    def test_all_missing_state_shows_warning_no_chart(self):
        df = _analysis([
            {"security_code": "X", "security_name": "甲", "sector_name": "",
             "sector_code": "", "sector_state": "—", "market_value": 1.0, "profit_pct": 0.0},
        ])
        P._render_portfolio_grid(df)
        _fake_st.warning.assert_called()
        _fake_st.plotly_chart.assert_not_called()

    def test_grid_offsets_deterministic_and_in_cell(self):
        """同格多点的偏移必须确定（刷新不跳位）且不越出 ±0.5 格边界。"""
        self.assertEqual(P._grid_offsets(1), [(0.0, 0.0)])
        for k in (2, 3, 5, 8):
            a, b = P._grid_offsets(k), P._grid_offsets(k)
            self.assertEqual(a, b, f"k={k} 两次调用结果应完全一致（不能用随机偏移）")
            self.assertEqual(len(a), k)
            for dx, dy in a:
                self.assertLess(abs(dx), 0.5, f"x 偏移越界: {dx}")
                self.assertLess(abs(dy), 0.5, f"y 偏移越界: {dy}")


class StateTransitionsTest(unittest.TestCase):
    def setUp(self):
        _fake_st.reset_mock()
        self._badge = mock.patch.object(P, "render_src_badge")
        self._badge.start()

    def tearDown(self):
        self._badge.stop()

    def _run(self, df, series_map):
        def fake_loader(code):
            return series_map.get(code, pd.DataFrame())
        with mock.patch.object(P, "load_state_series_cached", side_effect=fake_loader):
            P._render_state_transitions(df, n_changes=3)

    def _summary_df(self):
        calls = _fake_st.dataframe.call_args_list
        assert calls, "应调用 st.dataframe 输出汇总表"
        return calls[0].args[0]

    def test_dedup_by_sector(self):
        """同一行业的两只标的只出一行，持有标的合并展示。"""
        df = _analysis([
            {"security_code": "600000", "security_name": "浦发银行", "sector_name": "银行",
             "sector_code": "881155", "sector_state": "⑥弱转强"},
            {"security_code": "601398", "security_name": "工商银行", "sector_name": "银行",
             "sector_code": "881155", "sector_state": "⑥弱转强"},
            {"security_code": "601888", "security_name": "中国中免", "sector_name": "旅游及酒店",
             "sector_code": "881160", "sector_state": "④强转弱"},
        ])
        series_map = {
            "881155": _series([("2026-08-03", "⑤中性震荡"), ("2026-08-04", "⑥弱转强"),
                               ("2026-08-05", "⑥弱转强")]),
            "881160": _series([("2026-08-03", "⑤中性震荡"), ("2026-08-04", "④强转弱")]),
        }
        self._run(df, series_map)
        summary = self._summary_df()
        self.assertEqual(len(summary), 2, f"两个行业应只有 2 行，实际 {len(summary)}")
        bank = summary[summary["行业代码"] == "881155"].iloc[0]
        self.assertIn("浦发银行", bank["持有标的"])
        self.assertIn("工商银行", bank["持有标的"])
        self.assertEqual(bank["当前状态"], "⑥弱转强")
        self.assertIn("2", str(bank["已持续"]), f"⑥应持续 2 个交易日，实际 {bank['已持续']}")
        self.assertEqual(bank["最近变化日"], "2026-08-04")

    def test_sector_without_history_degrades(self):
        df = _analysis([
            {"security_code": "600000", "security_name": "浦发银行", "sector_name": "银行",
             "sector_code": "881155", "sector_state": "⑥弱转强"},
        ])
        self._run(df, {})  # 无任何历史序列
        summary = self._summary_df()
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary.iloc[0]["当前状态"], "—")
        _fake_st.plotly_chart.assert_not_called()

    def test_no_sector_code_warns(self):
        df = _analysis([
            {"security_code": "X", "security_name": "甲", "sector_name": "",
             "sector_code": "", "sector_state": "—"},
        ])
        self._run(df, {})
        _fake_st.warning.assert_called()
        _fake_st.dataframe.assert_not_called()

    def test_timeline_bar_count_matches_runs(self):
        """甘特图色块数应等于所有行业的状态段总数，且横轴为日期类型。"""
        df = _analysis([
            {"security_code": "600000", "security_name": "浦发银行", "sector_name": "银行",
             "sector_code": "881155", "sector_state": "⑥弱转强"},
        ])
        # 3 段：⑧(2日) → ⑤(1日) → ⑥(2日)
        series_map = {
            "881155": _series([
                ("2026-08-01", "⑧下跌中继"), ("2026-08-02", "⑧下跌中继"),
                ("2026-08-03", "⑤中性震荡"),
                ("2026-08-04", "⑥弱转强"), ("2026-08-05", "⑥弱转强"),
            ]),
        }
        self._run(df, series_map)
        fig = _last_figure()
        bars = [t for t in fig.data if t.type == "bar"]
        self.assertEqual(len(bars), 3, f"应有 3 个状态色块，实际 {len(bars)}")
        self.assertEqual(fig.layout.xaxis.type, "date", "时间线横轴应为日期类型")
        # 每段宽度 = (end+1天 - start) 的毫秒数，第一段 8-01~8-02 应为 2 天
        day_ms = 86400000.0
        self.assertAlmostEqual(bars[0].x[0], 2 * day_ms, delta=1.0,
                               msg=f"首段应覆盖 2 天，实际 {bars[0].x[0] / day_ms} 天")
        self.assertAlmostEqual(bars[1].x[0], 1 * day_ms, delta=1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

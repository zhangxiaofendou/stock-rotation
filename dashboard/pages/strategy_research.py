"""策略研究页面：策略回测 / 历史回放 / 实验记录。

与实时看板、真实持仓完全隔离；只读冻结历史行情，事件驱动 T+1 回测。
红涨绿跌（中国习惯）：收益为正用红色，为负用绿色。
"""

import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from datetime import date

from config.sector_map import SW_LEVEL2_MAP, get_sector_name
from backtest.engine import BacktestEngine
from backtest.strategies import MomentumStrategy, NineGridStrategy, MirrorPairStrategy
from backtest import metrics as bt_metrics
from backtest import experiments as bt_exp
from backtest import replay as bt_replay
from dashboard.components.data_source_badge import render_src_badge

RED = "#e74c3c"
GREEN = "#27ae60"
GRAY = "#7f8c8d"

STRATEGY_NAMES = ["策略1·动量轮动", "策略2·九宫格状态", "策略3·镜像对"]

# 默认回测区间（PRD：2018 起，覆盖牛熊）
DEFAULT_START = "2018-01-01"
DEFAULT_END = date.today().isoformat()


def _fmt_pct(value, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value * 100:.{digits}f}%"


def _fmt_num(value, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value:.{digits}f}"


# ============================================================
# 回测执行
# ============================================================
@st.cache_data(ttl=3600)
def _run_backtest(strategy_name: str, params_key: str, start: str, end: str, cost: float, stop: float):
    """实际执行回测（带缓存，参数变化才重算）。params_key 为 JSON 字符串（可哈希）。"""
    import json
    params = json.loads(params_key)
    codes = list(SW_LEVEL2_MAP.keys())
    engine = BacktestEngine.from_parquet_store(
        codes, bench_code="000300.SH", cost=cost, start=start, end=end
    )
    if strategy_name == STRATEGY_NAMES[0]:
        strat = MomentumStrategy(
            lookback=params.get("lookback", 20),
            top_n=params.get("top_n", 5),
            cash_when_bear=params.get("cash_when_bear", 0.5),
        )
    elif strategy_name == STRATEGY_NAMES[1]:
        strat = NineGridStrategy(
            persist_days=params.get("persist_days", 3),
            base_weight=params.get("base_weight", 0.10),
        )
    else:
        strat = MirrorPairStrategy(base_weight=params.get("base_weight", 0.10))

    decisions = strat.build(engine)
    result = engine.run(decisions, stop_loss_pct=stop if stop > 0 else None, start=start, end=end)
    m = bt_metrics.compute_metrics(result)
    return result, m


def _params_for(strategy_name) -> dict:
    p = {}
    if strategy_name == STRATEGY_NAMES[0]:
        col1, col2, col3 = st.columns(3)
        with col1:
            p["lookback"] = st.number_input("动量回看天数", 5, 120, 20, 5)
        with col2:
            p["top_n"] = st.number_input("入选数量(前N)", 1, 20, 5, 1)
        with col3:
            p["cash_when_bear"] = st.slider("熊市仓位系数", 0.0, 1.0, 0.5, 0.05)
    else:
        col1, col2 = st.columns(2)
        with col1:
            p["base_weight"] = st.slider("单板块计划仓位", 0.02, 0.30, 0.10, 0.01)
        with col2:
            if strategy_name == STRATEGY_NAMES[1]:
                p["persist_days"] = st.number_input("⑨持续天数才建仓", 1, 10, 3, 1)
            else:
                p["persist_days"] = 1
    return p


# ============================================================
# 渲染：策略回测
# ============================================================
def _render_backtest():
    st.subheader("策略回测")
    render_src_badge("ths", "derived", base=["ths_kline", "em_flow"])
    st.caption("事件驱动、T+1 开盘撮合、双边成本；与实时看板/真实持仓隔离。回测目的是证伪与理解策略性格，"
               "回测好看 ≠ 实盘能赚。")

    strategy_name = st.selectbox("选择策略", STRATEGY_NAMES,
                                 help="策略1动量轮动 / 策略2九宫格左侧交易 / 策略3镜像对确认")
    params = _params_for(strategy_name)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        start = st.date_input("开始日期", value=pd.to_datetime(DEFAULT_START)).strftime("%Y-%m-%d")
    with c2:
        end = st.date_input("结束日期", value=pd.to_datetime(DEFAULT_END)).strftime("%Y-%m-%d")
    with c3:
        cost = st.number_input("单边成本", 0.0, 0.01, 0.00075, 0.00025, format="%.5f")
    with c4:
        stop = st.number_input("单标的止损(%)", 0.0, 50.0, 10.0, 1.0)

    run = st.button("▶ 运行回测", type="primary")
    if not run:
        st.info("设置参数后点击「运行回测」。策略2/3 需预计算全部板块状态序列，首次运行约 10–30 秒。")
        return

    stop_frac = stop / 100.0
    cost_total = cost * 2  # 双边
    import json
    params_key = json.dumps(params, sort_keys=True)
    with st.spinner(f"正在运行 {strategy_name}（{start} ~ {end}）..."):
        try:
            result, m = _run_backtest(strategy_name, params_key, start, end, cost_total, stop_frac)
        except Exception as e:  # noqa: BLE001
            st.exception(e)
            return

    _render_metrics(m, strategy_name)
    _render_equity_chart(result)
    _render_annual(m)
    _render_contributions(result)
    _render_trades(result)
    _render_save_experiment(strategy_name, params, m, start, end)


def _render_metrics(m: dict, strategy_name: str):
    st.subheader("核心指标")
    render_src_badge("derived", base=["ths_kline", "em_flow"])
    bench = m.get("benchmark") or {}
    cols = st.columns(4)
    cols[0].metric("总收益", _fmt_pct(m.get("total_ret")), _fmt_pct(bench.get("excess_total_ret", 0), 1))
    cols[1].metric("年化收益", _fmt_pct(m.get("cagr")), _fmt_pct(bench.get("excess_cagr", 0), 1))
    cols[2].metric("最大回撤", _fmt_pct(m.get("max_drawdown")))
    cols[3].metric("夏普比率", _fmt_num(m.get("sharpe")))
    cols = st.columns(4)
    cols[0].metric("胜率", _fmt_pct(m.get("win_rate")))
    cols[1].metric("盈亏比", _fmt_num(m.get("profit_loss_ratio")))
    cols[2].metric("年化换手", _fmt_pct(m.get("turnover_annual")))
    cols[3].metric("交易回合", str(m.get("n_rounds", 0)))
    if bench:
        st.caption(f"对比基准 沪深300：基准总收益 {_fmt_pct(bench.get('bench_total_ret'))}，"
                   f"超额总收益 {_fmt_pct(bench.get('excess_total_ret'))}")


def _render_equity_chart(result):
    eq = result.equity_curve.reset_index()
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq["date"], y=eq["equity"], name="策略净值",
        line=dict(color=RED, width=2),
    ))
    if "benchmark" in eq.columns:
        fig.add_trace(go.Scatter(
            x=eq["date"], y=eq["benchmark"], name="沪深300",
            line=dict(color=GRAY, width=1.5, dash="dash"),
        ))
    fig.update_layout(
        title="权益曲线（初始 1,000,000）", height=360,
        margin=dict(l=40, r=20, t=40, b=30),
        legend=dict(orientation="h", y=1.02), hovermode="x unified",
    )
    fig.update_yaxes(title_text="净值")
    st.plotly_chart(fig, use_container_width=True)


def _render_annual(m: dict):
    annual = m.get("annual_returns") or {}
    if not annual:
        return
    st.subheader("分年度收益")
    render_src_badge("derived", base=["ths_kline", "em_flow"])
    df = pd.DataFrame([{"年度": k, "收益": v} for k, v in annual.items()])
    fig = go.Figure()
    colors = [RED if v >= 0 else GREEN for v in df["收益"]]
    fig.add_trace(go.Bar(x=df["年度"], y=df["收益"] * 100, marker_color=colors))
    fig.update_layout(title="分年度收益（红涨绿跌）", height=300,
                      margin=dict(l=40, r=20, t=40, b=30), yaxis_title="%")
    st.plotly_chart(fig, use_container_width=True)


def _render_contributions(result):
    st.subheader("板块收益贡献（Top/Bottom）")
    render_src_badge("derived", base=["ths_kline", "em_flow"])
    contrib = result.contributions.copy()
    contrib["板块"] = contrib["code"].map(lambda c: f"{get_sector_name(c)}")
    contrib = contrib.sort_values("contribution", ascending=False)
    top = contrib.head(10)
    fig = go.Figure()
    colors = [RED if v >= 0 else GREEN for v in top["contribution"]]
    fig.add_trace(go.Bar(x=top["板块"], y=top["contribution"] * 100, marker_color=colors))
    fig.update_layout(title="收益贡献 Top10（权重×区间涨幅）", height=320,
                      margin=dict(l=40, r=20, t=40, b=80), yaxis_title="%")
    st.plotly_chart(fig, use_container_width=True)


def _render_trades(result):
    if not result.trades:
        return
    st.subheader("交易明细")
    render_src_badge("derived", base=["ths_kline", "em_flow"])
    rows = []
    for t in result.trades:
        rows.append({
            "日期": t.date, "板块": get_sector_name(t.code), "方向": t.side,
            "成交价": _fmt_num(t.price), "金额": _fmt_num(t.notional),
            "权重": _fmt_pct(t.weight_after),
        })
    disp = pd.DataFrame(rows)
    st.dataframe(disp, hide_index=True, width="stretch")


def _render_save_experiment(strategy_name: str, params: dict, m: dict, start: str, end: str):
    st.subheader("保存实验记录")
    render_src_badge("derived", base=["user"])
    note = st.text_input("实验备注（可选）", placeholder="如：参数鲁棒性测试 / 熊市表现")
    if st.button("💾 保存本次实验"):
        exp_id = bt_exp.save_experiment(strategy_name, params, m, start, end, note)
        st.success(f"已保存实验记录：{exp_id}")


# ============================================================
# 渲染：历史回放
# ============================================================
def _render_replay():
    st.subheader("历史信号回放")
    render_src_badge("derived", base=["history"])
    st.caption("选择任一历史日期，只读展示当天市场全貌（九宫格状态分布 / 镜像对 / 动作建议）。")
    rep_date = st.date_input("回放日期", value=pd.to_datetime(DEFAULT_END))
    rep_date_str = rep_date.strftime("%Y-%m-%d")
    with st.spinner("正在装配回放数据..."):
        try:
            data = bt_replay.build_replay(rep_date_str)
        except Exception as e:  # noqa: BLE001
            st.exception(e)
            return
    if data.get("state_df") is None:
        st.warning(data.get("summary", "该日期无数据"))
        return

    s = data["summary"]
    cols = st.columns(4)
    cols[0].metric("覆盖板块", s["n_sectors"])
    cols[1].metric("买入信号", s["n_buy"])
    cols[2].metric("镜像对", s["n_mirror_pairs"])
    cols[3].metric("减/清仓", s["action_counts"].get("减仓", 0) + s["action_counts"].get("清仓", 0))

    st.subheader("九宫格状态分布")
    render_src_badge("derived", base=["ths_kline"])
    dist = data["distribution"]
    if dist:
        df = pd.DataFrame([{"状态": k, "数量": v} for k, v in dist.items()])
        fig = go.Figure()
        palette = {
            "⑥弱转强": RED, "⑨底背离": RED, "②稳健上行": RED, "③加速冲顶": "#e67e22",
            "①领涨减速": "#e67e22", "④强转弱": "#e67e22", "⑦持续杀跌": GREEN,
            "⑧下跌中继": GREEN, "⑤中性震荡": GRAY,
        }
        colors = [palette.get(k, GRAY) for k in df["状态"]]
        fig.add_trace(go.Bar(x=df["状态"], y=df["数量"], marker_color=colors))
        fig.update_layout(title=f"{rep_date_str} 各状态板块数量", height=300,
                          margin=dict(l=40, r=20, t=40, b=60))
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("镜像对（关联组内④↔⑥ / ③↔⑦）")
    render_src_badge("derived", base=["ths_kline", "em_flow"])
    if data["mirror_pairs"]:
        mp_rows = []
        for p in data["mirror_pairs"]:
            mp_rows.append({
                "组别": p.get("group"), "类型": p.get("pair_type"),
                "强端": f'{get_sector_name(p["strong_sector"])}',
                "弱端": f'{get_sector_name(p["weak_sector"])}',
                "置信度": _fmt_pct(p.get("confidence")),
            })
        st.dataframe(pd.DataFrame(mp_rows), hide_index=True, width="stretch")
    else:
        st.info("该日期未发现镜像对。")


# ============================================================
# 渲染：实验记录
# ============================================================
def _render_experiments():
    st.subheader("实验记录")
    render_src_badge("derived", base=["user"])
    st.caption("每次回测的参数快照 + 核心指标 + git hash，用于策略对比与参数调优追溯。")
    recs = bt_exp.list_experiments()
    if not recs:
        st.info("暂无实验记录。在「策略回测」页运行后点击「保存本次实验」即可记录。")
        return

    sel = st.selectbox("选择实验", [f'{r["timestamp"]} · {r["strategy_name"]}' for r in recs])
    rec = recs[[f'{r["timestamp"]} · {r["strategy_name"]}' for r in recs].index(sel)]
    m = rec.get("metrics") or {}
    bench = m.get("benchmark") or {}
    st.write(f"**实验ID**：`{rec['experiment_id']}` ｜ **git**：`{rec.get('git_hash')}`")
    if rec.get("note"):
        st.write(f"**备注**：{rec['note']}")

    cols = st.columns(4)
    cols[0].metric("总收益", _fmt_pct(m.get("total_ret")), _fmt_pct(bench.get("excess_total_ret", 0), 1))
    cols[1].metric("年化", _fmt_pct(m.get("cagr")))
    cols[2].metric("最大回撤", _fmt_pct(m.get("max_drawdown")))
    cols[3].metric("夏普", _fmt_num(m.get("sharpe")))
    st.write("**参数快照**：", rec.get("params"))


# ============================================================
# 主入口
# ============================================================
def render():
    st.title("🔬 策略研究")
    st.caption("事件驱动回测 + 信号回放 + 实验记录。与实时看板、真实持仓完全隔离，只读冻结历史行情。")
    from dashboard.components.nav_state import persistent_tabs
    active = persistent_tabs(
        "strategy_research_tab",
        ["策略回测", "历史回放", "实验记录"],
    )
    if active == "策略回测":
        _render_backtest()
    elif active == "历史回放":
        _render_replay()
    elif active == "实验记录":
        _render_experiments()

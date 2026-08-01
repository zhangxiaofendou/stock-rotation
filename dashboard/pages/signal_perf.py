"""信号绩效页面：基于 signal_performance 账本后验跟踪信号质量。

页签：绩效总览 / 路径分析 / 信号明细 / 失效预警。
所有数据来自已冻结的后续表现，不重算状态机、不混入回测模拟收益。
"""

import pandas as pd
import streamlit as st

from data.storage.sqlite_store import SQLiteStore
from signal_tracker.performance import (
    PerformanceConfig,
    aggregate_overview,
    failure_alerts,
    path_analysis,
)
from dashboard.components.data_source_badge import render_src_badge


@st.cache_data(ttl=3600)
def load_perf_df() -> pd.DataFrame:
    """加载信号后续表现账本（内存缓存，1 小时）。"""
    return SQLiteStore().get_signal_performance()


def _fmt_pct(value, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value * 100:.{digits}f}%"


def _render_overview(perf: pd.DataFrame):
    window = st.radio("统计窗口", [30, 60, 90], index=2, horizontal=True,
                      format_func=lambda x: f"近 {x} 日")
    ov = aggregate_overview(window, perf_df=perf)
    if ov is None:
        st.info("所选窗口内暂无信号后续表现数据。")
        return
    st.caption(f"数据区间：{ov['anchor'].date()} ~ {ov['max_date'].date()}（共 {ov['n']} 个信号）")

    d = ov["direction"]
    cols = st.columns(4)
    for col, dir_name in zip(cols, ["BUY", "SELL", "HOLD", "AVOID"]):
        row = d[d["signal_direction"] == dir_name]
        if not row.empty:
            r = row.iloc[0]
            col.metric(
                dir_name,
                _fmt_pct(r["win_rate"]),
                f"样本 {int(r['samples'])} · 均20日 {_fmt_pct(r['avg_return_t20'])}",
            )

    st.subheader("按信号类型（进入状态）")
    render_src_badge("derived", base=["history"])
    to_tbl = ov["to_state"].copy()
    to_tbl["胜率"] = to_tbl["win_rate"].map(_fmt_pct)
    to_tbl["平均20日收益"] = to_tbl["avg_return_t20"].map(_fmt_pct)
    to_tbl["平均超额"] = to_tbl["avg_excess_t20"].map(_fmt_pct)
    to_tbl = to_tbl.rename(columns={
        "to_state": "信号类型", "samples": "样本", "success": "成功",
        "failure": "失败", "neutral": "中性",
    })
    st.dataframe(
        to_tbl[["信号类型", "样本", "成功", "失败", "中性", "胜率", "平均20日收益", "平均超额"]],
        hide_index=True, width="stretch",
        column_config={"胜率": st.column_config.TextColumn("胜率", width="small")},
    )

    st.subheader("按信号源（离开状态）")
    render_src_badge("derived", base=["history"])
    from_tbl = ov["from_state"].copy()
    from_tbl["胜率"] = from_tbl["win_rate"].map(_fmt_pct)
    from_tbl["平均20日收益"] = from_tbl["avg_return_t20"].map(_fmt_pct)
    from_tbl = from_tbl.rename(columns={
        "from_state": "信号源", "samples": "样本", "success": "成功",
        "failure": "失败", "neutral": "中性",
    })
    st.dataframe(
        from_tbl[["信号源", "样本", "成功", "失败", "中性", "胜率", "平均20日收益"]],
        hide_index=True, width="stretch",
    )


def _render_paths(perf: pd.DataFrame):
    st.subheader("关键路径后续表现")
    render_src_badge("derived", base=["history"])
    pa = path_analysis(perf_df=perf)
    if pa.empty:
        st.info("暂无路径数据。")
        return
    pa_disp = pa.copy()
    pa_disp["胜率"] = pa_disp["win_rate"].map(_fmt_pct)
    pa_disp["平均5日收益"] = pa_disp["avg_return_t5"].map(_fmt_pct)
    pa_disp["平均20日收益"] = pa_disp["avg_return_t20"].map(_fmt_pct)
    pa_disp["平均超额"] = pa_disp["avg_excess_t20"].map(_fmt_pct)
    pa_disp = pa_disp.rename(columns={
        "path": "路径", "samples": "样本", "success": "成功",
        "failure": "失败", "neutral": "中性",
    })
    st.dataframe(
        pa_disp[["路径", "样本", "成功", "失败", "中性", "胜率", "平均5日收益", "平均20日收益", "平均超额"]],
        hide_index=True, width="stretch",
    )
    chart = pa.dropna(subset=["win_rate"]).set_index("path")["win_rate"]
    if not chart.empty:
        st.bar_chart(chart, height=320)


def _render_detail(perf: pd.DataFrame):
    st.subheader("信号明细")
    render_src_badge("derived", base=["history"])
    col1, col2, col3 = st.columns(3)
    with col1:
        sector = st.text_input("板块代码/名称筛选", placeholder="如 801080 或 半导体")
    with col2:
        to_states = ["全部"] + sorted(perf["to_state"].dropna().unique().tolist())
        to_state = st.selectbox("进入状态", to_states)
    with col3:
        outcome = st.selectbox("判定", ["全部", "success", "failure", "neutral"])

    df = perf.copy()
    if sector:
        df = df[df["sector_code"].astype(str).str.contains(sector, na=False)
                | df["sector_name"].astype(str).str.contains(sector, na=False)]
    if to_state != "全部":
        df = df[df["to_state"] == to_state]
    if outcome != "全部":
        df = df[df["outcome"] == outcome]
    df = df.sort_values("event_date", ascending=False).head(1000)

    disp = df.copy()
    disp["信号日"] = disp["event_date"].dt.strftime("%Y-%m-%d")
    disp["路径"] = disp["from_state"].astype(str) + "→" + disp["to_state"].astype(str)
    disp["T+1基准"] = disp["base_price"].map(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    disp["T+5收益"] = disp["return_t5"].map(lambda x: f"{x * 100:+.1f}%" if pd.notna(x) else "—")
    disp["T+20收益"] = disp["return_t20"].map(lambda x: f"{x * 100:+.1f}%" if pd.notna(x) else "—")
    disp["T+20超额"] = disp["excess_t20"].map(lambda x: f"{x * 100:+.1f}%" if pd.notna(x) else "—")
    disp["判定"] = disp["outcome"].map({"success": "成功", "failure": "失败", "neutral": "中性"})
    disp = disp.rename(columns={
        "sector_name": "板块", "signal_direction": "方向",
        "state_t5": "T+5状态", "state_t20": "T+20状态",
    })
    st.caption(f"展示最近 {len(disp)} 条（最多 1000）")
    st.dataframe(
        disp[["信号日", "板块", "路径", "方向", "T+1基准", "T+5状态", "T+20状态",
              "T+5收益", "T+20收益", "T+20超额", "判定"]],
        hide_index=True, width="stretch",
        column_config={"T+5收益": st.column_config.TextColumn("T+5收益", width="small"),
                       "T+20收益": st.column_config.TextColumn("T+20收益", width="small"),
                       "T+20超额": st.column_config.TextColumn("T+20超额", width="small")},
    )


def _render_alerts(perf: pd.DataFrame):
    st.subheader("失效预警")
    render_src_badge("derived", base=["history"])
    c1, c2, c3 = st.columns(3)
    with c1:
        window = st.selectbox("统计窗口", [30, 60, 90, 180, 0],
                              index=2, format_func=lambda x: "全样本" if x == 0 else f"近 {x} 日")
    with c2:
        min_samples = st.number_input("最小样本量", min_value=5, max_value=500, value=30, step=5)
    with c3:
        fail_thr = st.number_input("失效胜率阈值 (%)", min_value=5, max_value=90, value=40, step=5)
    cfg = PerformanceConfig(min_samples=int(min_samples), fail_threshold=float(fail_thr) / 100)
    res = failure_alerts(config=cfg, perf_df=perf, window_days=window)

    if res.get("window") is not None:
        st.caption(f"窗口起点：{res['window'].date()}（仅统计此后发生的信号）")
    if res["alerts"].empty:
        st.success(f"当前无信号类型在样本≥{min_samples}且胜率<{fail_thr}%条件下触发失效预警。")
    else:
        st.warning(f"发现 {len(res['alerts'])} 个信号类型触发失效预警：")
        al = res["alerts"].copy()
        al["胜率"] = al["win_rate"].map(_fmt_pct)
        al["平均20日收益"] = al["avg_return_t20"].map(_fmt_pct)
        al = al.rename(columns={"to_state": "信号类型", "samples": "样本", "success": "成功", "failure": "失败"})
        st.dataframe(
            al[["信号类型", "样本", "成功", "失败", "胜率", "平均20日收益"]],
            hide_index=True, width="stretch",
        )

    st.subheader("全部信号类型表现（当前窗口）")
    render_src_badge("derived", base=["history"])
    allt = res["all"].copy()
    allt["胜率"] = allt["win_rate"].map(_fmt_pct)
    allt["平均20日收益"] = allt["avg_return_t20"].map(_fmt_pct)
    allt = allt.rename(columns={
        "to_state": "信号类型", "samples": "样本", "success": "成功",
        "failure": "失败", "neutral": "中性",
    })
    st.dataframe(
        allt[["信号类型", "样本", "成功", "失败", "中性", "胜率", "平均20日收益"]],
        hide_index=True, width="stretch",
    )


def _render_init_prompt():
    """账本为空（云端首次部署常见）时的自初始化引导。"""
    st.info(
        "信号后续表现数据尚未生成（云端首次部署时常见）。\n"
        "点击下方按钮，用**已提交的历史行情**在本地/云端重建账本"
        "（约 1–3 分钟，仅首次需要，重建后看板会立即刷新）。"
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button("▶ 开始初始化", key="init_signal_perf", type="primary"):
            _run_init(force=False)
    with col2:
        if st.button("🔄 强制重建", key="reinit_signal_perf"):
            _run_init(force=True)


def _run_init(force: bool = False):
    from data.runtime_init import ensure_signal_performance
    with st.spinner("正在从已提交的历史行情重建信号表现账本（首次约 1–3 分钟）..."):
        try:
            res = ensure_signal_performance(force=force)
        except Exception as e:  # noqa: BLE001
            st.exception(e)
            return
    if res["built"]:
        msg = f"初始化完成：写入 {res['perf']} 条信号表现、{res['events']} 条信号事件。"
        if not res["benchmark_ok"]:
            msg += "（基准下载失败，超额收益列暂为空；运行每日管线后可补全）"
        st.success(msg)
    else:
        st.info("数据已存在，无需重建。")
    try:
        load_perf_df.clear()
    except Exception:  # noqa: BLE001
        pass
    st.rerun()


def render():
    """信号绩效主入口。"""
    st.title("📈 信号绩效")
    st.caption("基于信号事件账本冻结的后续表现（T+1 开盘基准、T+5/T+20 收益、超额收益与成败判定）。"
               "系统不重算状态机，仅后验跟踪；与回测模拟收益严格区分。")
    perf = load_perf_df()
    if perf is None or perf.empty:
        _render_init_prompt()
        return
    perf["event_date"] = pd.to_datetime(perf["event_date"])

    tabs = st.tabs(["绩效总览", "路径分析", "信号明细", "失效预警"])
    with tabs[0]:
        _render_overview(perf)
    with tabs[1]:
        _render_paths(perf)
    with tabs[2]:
        _render_detail(perf)
    with tabs[3]:
        _render_alerts(perf)

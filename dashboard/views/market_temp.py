"""
市场温度计页面
==============
展示市场环境状态、九宫格分布、风格雷达、涨跌统计。
"""

import streamlit as st
import pandas as pd
import numpy as np
import sys
import os

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from model.state_machine import StateMachine
from model.circuit_breaker import CircuitBreaker
from config.sector_map import SECTOR_GROUPS, get_sector_name
from dashboard.components.data_source_badge import render_src_badge

# 状态颜色
STATE_COLORS = {
    "①领涨减速": "#FF9800",
    "②稳健上行": "#81C784",
    "③加速冲顶": "#FFC107",
    "④强转弱":   "#FF9800",
    "⑤中性震荡": "#9E9E9E",
    "⑥弱转强":   "#4CAF50",
    "⑦持续杀跌": "#F44336",
    "⑧下跌中继": "#E53935",
    "⑨底背离":   "#2E7D32",
}

ALL_STATES = [
    "①领涨减速", "②稳健上行", "③加速冲顶",
    "④强转弱", "⑤中性震荡", "⑥弱转强",
    "⑦持续杀跌", "⑧下跌中继", "⑨底背离",
]


@st.cache_resource
def get_stores():
    """获取数据存储实例（缓存）"""
    parquet_store = ParquetStore()
    sqlite_store = SQLiteStore()
    return parquet_store, sqlite_store


@st.cache_resource
def get_state_machine():
    """获取状态机实例"""
    parquet_store, sqlite_store = get_stores()
    return StateMachine(parquet_store, sqlite_store)


@st.cache_resource
def get_circuit_breaker():
    """获取熔断器实例"""
    parquet_store, sqlite_store = get_stores()
    sm = get_state_machine()
    return CircuitBreaker(parquet_store, sm)


@st.cache_data(ttl=86400)
def load_market_data():
    """加载市场数据（1小时缓存）"""
    sm = get_state_machine()
    cb = get_circuit_breaker()
    parquet_store, _ = get_stores()

    # 市场状态
    market_status = cb.check_market_status()

    # 所有板块状态
    state_df = sm.calc_all_sectors_state()

    # 状态分布
    state_dist = sm.get_state_distribution()

    # 计算当日涨跌分布（基于 parquet index_hist 最新日涨跌幅，而非均线趋势）
    up_count = down_count = flat_count = 0
    if state_df is not None and not state_df.empty:
        parquet_store, _ = get_stores()
        latest_date = state_df["date"].iloc[0]
        pct_rows = []
        for _, row in state_df.iterrows():
            code = row["sector_code"]
            df = parquet_store.load_index_hist(code)
            if df is None or df.empty:
                continue
            df = df.copy()
            # 同花顺 K 线落盘为中文列，统一映射
            col_map = {"日期": "date", "涨跌幅": "pct_change"}
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            if "date" not in df.columns or "pct_change" not in df.columns:
                continue
            df["date"] = df["date"].astype(str).str[:10]
            row_today = df[df["date"] == str(latest_date)]
            if row_today.empty:
                continue
            try:
                pct = float(row_today["pct_change"].iloc[-1])
                pct_rows.append(pct)
            except Exception:
                continue
        if pct_rows:
            arr = np.array(pct_rows)
            up_count = int((arr > 0.01).sum())      # > +0.01%
            down_count = int((arr < -0.01).sum())   # < -0.01%
            flat_count = int(((arr >= -0.01) & (arr <= 0.01)).sum())

    # 板块分组统计
    group_stats = {}
    if state_df is not None:
        for group_name, group_info in SECTOR_GROUPS.items():
            group_codes = set(group_info["level2_codes"])
            group_df = state_df[state_df["sector_code"].isin(group_codes)]
            if len(group_df) > 0:
                buy_states = group_df[group_df["state"].isin(["⑥弱转强", "⑨底背离"])]
                sell_states = group_df[group_df["state"].isin(["①领涨减速", "④强转弱", "⑦持续杀跌"])]
                group_stats[group_name] = {
                    "total": len(group_df),
                    "buy_count": len(buy_states),
                    "sell_count": len(sell_states),
                }

    return market_status, state_df, state_dist, up_count, down_count, flat_count, group_stats


def render():
    """渲染市场温度计页面"""
    import plotly.graph_objects as go  # 懒加载:plotly ~10MB,只在打开本页时加载
    import plotly.express as px        # 同上
    st.title("市场温度计")
    st.markdown("监控整体市场环境、板块状态分布和风格强弱")

    from dashboard.components.nav_state import persistent_tabs
    from dashboard.views.mirror_pair import render as render_mirror

    MARKET_TEMP_TABS = ["市场概览", "镜像对监控"]
    active = persistent_tabs("market_temp_tab", MARKET_TEMP_TABS)

    if active == "市场概览":
        _render_market_overview()
    elif active == "镜像对监控":
        render_mirror(show_header=False)


@st.cache_data(ttl=3600)
def _build_fund_flow_figures():
    """构建资金流向地图图表（缓存，避免每次 tab 切换重算）。"""
    import plotly.graph_objects as go  # 懒加载:仅在被 _render_fund_flow_map 调用时加载
    sqlite = SQLiteStore()
    latest = sqlite.get_latest_fund_flow_date()
    if latest is None:
        return {"warning": "no_data", "row_count": len(sqlite.get_sector_fund_flow())}

    df = sqlite.get_sector_fund_flow(date=latest)
    if df is None or df.empty:
        return {"warning": "empty_day", "latest": latest}

    if "main_net_inflow" not in df.columns:
        df["main_net_inflow"] = None
    df = df.copy()
    df["name"] = df["sector_code"].map(lambda c: get_sector_name(c))
    valid = df.dropna(subset=["main_net_inflow"])
    if valid.empty:
        return {"warning": "no_valid", "latest": latest}

    df = valid.sort_values("main_net_inflow", ascending=False)
    top_in = df.head(10)
    top_out = df.tail(10).sort_values("main_net_inflow")

    fig_in = go.Figure()
    fig_in.add_trace(go.Bar(
        x=top_in["main_net_inflow"], y=top_in["name"], orientation="h",
        marker_color="#d4380d",
        text=top_in["main_net_inflow"].map(lambda v: f"{v:+.1f}亿"), textposition="auto",
    ))
    fig_in.update_layout(
        height=320, margin={"l": 10, "r": 10, "t": 10, "b": 10},
        xaxis_title="主力净流入（亿元）", yaxis={"autorange": "reversed"},
    )

    fig_out = go.Figure()
    fig_out.add_trace(go.Bar(
        x=top_out["main_net_inflow"], y=top_out["name"], orientation="h",
        marker_color="#389e0d",
        text=top_out["main_net_inflow"].map(lambda v: f"{v:+.1f}亿"), textposition="auto",
    ))
    fig_out.update_layout(
        height=320, margin={"l": 10, "r": 10, "t": 10, "b": 10},
        xaxis_title="主力净流入（亿元）", yaxis={"autorange": "reversed"},
    )

    return {"latest": latest, "fig_in": fig_in, "fig_out": fig_out}


def _render_fund_flow_map():
    """资金流向地图：展示当日行业真实主力资金流净流入/净流出排名（来自每日管线落盘）。"""
    st.subheader("资金流向地图")
    render_src_badge("em")
    st.caption(
        "行业真实主力资金流（同花顺行业，AkShare 数据源；单位：亿元）。"
        "🔴 红 = 净流入（资金涌入），🟢 绿 = 净流出（资金撤离）。"
    )

    result = _build_fund_flow_figures()
    if result.get("warning") == "no_data":
        st.warning(
            "资金流数据尚未落盘。\n\n"
            f"- `sector_fund_flow` 表当前行数：**{result.get('row_count', 0)}**\n\n"
            "请点击侧栏「🔄 手动刷新全部数据」运行完整管线（约 5–10 分钟，后台运行）。"
        )
        return
    if result.get("warning") == "empty_day":
        st.warning(f"资金流表存在（最新日期 **{result['latest']}**），但当日无记录。请重新运行手动刷新。")
        return
    if result.get("warning") == "no_valid":
        st.warning(
            f"当日资金流无真实净流入数据（{result['latest']}）。可能 AkShare 接口不可用且回退失败。"
            "请重新运行手动刷新，或在侧栏查看运行保障日志。"
        )
        return

    latest = result["latest"]
    fig_in = result["fig_in"]
    fig_out = result["fig_out"]

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**净流入前列（{latest}，亿元）**")
        st.plotly_chart(fig_in, width="stretch", key="fund_flow_in")
    with col2:
        st.markdown(f"**净流出前列（{latest}，亿元）**")
        st.plotly_chart(fig_out, width="stretch", key="fund_flow_out")


def _render_market_overview():
    """渲染市场概览（原市场温度计内容）"""
    import plotly.graph_objects as go  # 懒加载:plotly ~10MB,仅在使用此函数时加载
    with st.spinner("加载市场数据..."):
        market_status, state_df, state_dist, up_count, down_count, flat_count, group_stats = load_market_data()

    if state_df is None or state_df.empty:
        st.warning("暂无板块状态数据，请先运行数据更新流程")
        return

    # ================================================================
    # 第一行：市场环境状态 + 涨跌统计
    # ================================================================
    st.subheader("市场环境")
    render_src_badge("ths")

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        mode = market_status.get("mode", "normal")
        if mode == "defense":
            st.error("🛡️ 防御模式")
        else:
            st.success("✅ 正常模式")

    with col2:
        total = up_count + down_count + flat_count
        down_ratio = down_count / total if total > 0 else 0
        st.metric("下跌板块占比", f"{down_ratio:.1%}")

    with col3:
        kill_ratio = market_status.get("kill_ratio", 0)
        st.metric("⑦持续杀跌占比", f"{kill_ratio:.1%}")

    with col4:
        consecutive = market_status.get("consecutive_days", 0)
        st.metric("连续下跌天数", consecutive)

    # 熔断原因
    reason = market_status.get("reason", "")
    if mode == "defense":
        st.error(f"**触发原因**: {reason}")
    else:
        st.info(f"**状态**: {reason}")

    # ================================================================
    # 涨跌统计条（当日涨跌幅口径）
    # ================================================================
    st.subheader("涨跌分布")
    render_src_badge("ths")
    st.caption(f"基于各板块 {state_df['date'].iloc[0] if state_df is not None else ''} 当日涨跌幅统计（>+0.01% 为涨，<-0.01% 为跌）")
    total = up_count + down_count + flat_count
    if total > 0:
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("上涨板块", up_count, delta=f"+{up_count/total:.1%}")
        with c2:
            st.metric("横盘板块", flat_count, delta=f"{flat_count/total:.1%}", delta_color="off")
        with c3:
            st.metric("下跌板块", down_count, delta=f"-{down_count/total:.1%}")

        # 涨跌比柱状图（中国股市惯例：涨红跌绿）
        fig_bar = go.Figure()
        fig_bar.add_trace(go.Bar(
            x=["上涨", "横盘", "下跌"],
            y=[up_count, flat_count, down_count],
            marker_color=["#F44336", "#9E9E9E", "#4CAF50"],
            text=[up_count, flat_count, down_count],
            textposition="auto",
        ))
        fig_bar.update_layout(
            height=250,
            margin={"l": 10, "r": 10, "t": 10, "b": 10},
            xaxis_title="趋势方向",
            yaxis_title="板块数量",
        )
        st.plotly_chart(fig_bar, width="stretch")

    # ================================================================
    # 九宫格状态分布
    # ================================================================
    st.subheader("九宫格状态分布")
    render_src_badge("derived", base=["ths_kline"])

    if state_dist:
        # 准备分布数据
        dist_data = []
        for state in ALL_STATES:
            codes = state_dist.get(state, [])
            dist_data.append({
                "state": state,
                "count": len(codes),
                "color": STATE_COLORS.get(state, "#9E9E9E"),
            })

        dist_df = pd.DataFrame(dist_data)

        # 柱状图
        fig_dist = go.Figure()
        fig_dist.add_trace(go.Bar(
            x=dist_df["state"],
            y=dist_df["count"],
            marker_color=dist_df["color"],
            text=dist_df["count"],
            textposition="auto",
        ))
        fig_dist.update_layout(
            height=350,
            margin={"l": 10, "r": 10, "t": 10, "b": 60},
            xaxis={"tickangle": 30},
            yaxis_title="板块数量",
        )
        st.plotly_chart(fig_dist, width="stretch")

        # 饼图
        fig_pie = px.pie(
            dist_df[dist_df["count"] > 0],
            values="count",
            names="state",
            color="state",
            color_discrete_map={s: STATE_COLORS.get(s, "#9E9E9E") for s in dist_df["state"]},
            hole=0.4,
        )
        fig_pie.update_layout(height=400, margin={"l": 10, "r": 10, "t": 10, "b": 10})
        fig_pie.update_traces(textposition="inside", textinfo="percent+label")
        st.plotly_chart(fig_pie, width="stretch")

    # ================================================================
    # 风格雷达：板块组强弱统计
    # ================================================================
    st.subheader("板块组强弱统计")
    render_src_badge("derived", base=["ths_kline"])

    if group_stats:
        # 准备雷达图数据
        groups = list(group_stats.keys())
        buy_ratios = [group_stats[g]["buy_count"] / max(group_stats[g]["total"], 1) * 100 for g in groups]
        sell_ratios = [group_stats[g]["sell_count"] / max(group_stats[g]["total"], 1) * 100 for g in groups]

        fig_radar = go.Figure()
        fig_radar.add_trace(go.Scatterpolar(
            r=buy_ratios + [buy_ratios[0]],
            theta=groups + [groups[0]],
            fill="toself",
            name="买入信号占比",
            line_color="#4CAF50",
        ))
        fig_radar.add_trace(go.Scatterpolar(
            r=sell_ratios + [sell_ratios[0]],
            theta=groups + [groups[0]],
            fill="toself",
            name="卖出信号占比",
            line_color="#F44336",
        ))
        fig_radar.update_layout(
            polar={"radialaxis": {"visible": True, "range": [0, 100]}},
            height=400,
            margin={"l": 40, "r": 40, "t": 40, "b": 40},
        )
        st.plotly_chart(fig_radar, width="stretch")

        # 表格形式展示
        table_data = []
        for g in groups:
            s = group_stats[g]
            table_data.append({
                "板块组": g,
                "板块数": s["total"],
                "买入信号": s["buy_count"],
                "卖出信号": s["sell_count"],
                "买入占比": f"{s['buy_count']/max(s['total'],1):.1%}",
                "卖出占比": f"{s['sell_count']/max(s['total'],1):.1%}",
            })
        st.dataframe(pd.DataFrame(table_data), width="stretch", hide_index=True)

    # ================================================================
    # 资金流向地图（来自每日管线落盘的 sector_fund_flow）
    # ================================================================
    _render_fund_flow_map()

    # ================================================================
    # 触发条件详情
    # ================================================================
    with st.expander("熔断触发条件详情"):
        triggers = market_status.get("triggers", {})
        if triggers:
            md = triggers.get("market_down", {})
            sr = triggers.get("systematic_risk", {})
            hs = triggers.get("hs300_drop", {})

            col_a, col_b, col_c = st.columns(3)
            with col_a:
                st.markdown(f"**全市场下跌**")
                st.write(f"触发: {'是' if md.get('triggered') else '否'}")
                st.write(f"下跌占比: {md.get('down_ratio', 0):.1%}")
                st.write(f"连续天数: {md.get('consecutive_days', 0)}")
            with col_b:
                st.markdown(f"**系统性风险**")
                st.write(f"触发: {'是' if sr.get('triggered') else '否'}")
                st.write(f"杀跌占比: {sr.get('kill_ratio', 0):.1%}")
            with col_c:
                st.markdown(f"**沪深300**")
                st.write(f"触发: {'是' if hs.get('triggered') else '否'}")
                st.write(f"近5日跌幅: {hs.get('hs300_drop', 0):.2f}%")

    # ================================================================
    # 板块组之间的资金迁移路径（最底部）
    # ================================================================
    st.subheader("板块组之间的资金迁移路径")
    render_src_badge("derived", base=["ths_kline", "em_flow"])
    st.caption("按各板块组的净资金流推导组间迁移：红=净流出板块组(弱势)，绿=净流入板块组(强势)")

    try:
        from dashboard.views.mirror_pair import render_group_capital_path
        render_group_capital_path(state_df)
    except Exception as e:
        st.warning(f"板块组资金迁移路径加载失败：{e}")


if __name__ == "__main__":
    render()

"""
板块轮动监控页面（核心页面）
===========================
展示九宫格热力图、板块强弱排行、重点关注区、板块卡片。
"""

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import numpy as np
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from model.state_machine import StateMachine
from model.scoring import SectorScoring
from model.mirror_pair import MirrorPair
from config.sector_map import get_sector_name
from dashboard.components.state_card import render_state_card, STATE_COLORS, STATE_EMOJI
from dashboard.components.drill_pickers import (
    load_all_sector_states as _load_drill_states,
    render_state_picker,
    render_transition_picker,
    render_sector_picker,
)

# 状态颜色映射
STATE_BG_COLORS = {
    "①领涨减速": "background-color: #FFF3E0;",
    "②稳健上行": "background-color: #E8F5E9;",
    "③加速冲顶": "background-color: #FFFDE7;",
    "④强转弱":   "background-color: #FFF3E0;",
    "⑤中性震荡": "background-color: #F5F5F5;",
    "⑥弱转强":   "background-color: #E8F5E9;",
    "⑦持续杀跌": "background-color: #FFEBEE;",
    "⑧下跌中继": "background-color: #FFEBEE;",
    "⑨底背离":   "background-color: #E8F5E9;",
}


@st.cache_resource
def get_stores():
    parquet_store = ParquetStore()
    sqlite_store = SQLiteStore()
    return parquet_store, sqlite_store


@st.cache_resource
def get_models():
    parquet_store, sqlite_store = get_stores()
    sm = StateMachine(parquet_store, sqlite_store)
    scoring = SectorScoring(parquet_store, sqlite_store, sm)
    mirror = MirrorPair(sqlite_store, sm)
    return sm, scoring, mirror


@st.cache_data(ttl=86400)  # 每日凌晨更新一次，24h TTL
def load_state_df():
    """加载板块状态（独立缓存）"""
    sm, _, _ = get_models()
    return sm.calc_all_sectors_state()


@st.cache_data(ttl=86400)
def load_score_df():
    """加载板块评分排行（独立缓存）"""
    _, scoring, _ = get_models()
    return scoring.calc_all_scores()


@st.cache_data(ttl=86400)
def load_mirror_pairs():
    """加载镜像对（独立缓存）"""
    _, _, mirror = get_models()
    return mirror.find_mirror_pairs()


@st.cache_data(ttl=86400)
def load_sector_rs_history(sector_code: str):
    """加载单个板块的RS历史（用于sparkline）"""
    sm, _, _ = get_models()
    try:
        series = sm.calc_state_series(sector_code)
        if series is not None and len(series) > 0:
            recent = series.tail(20)
            return list(recent["rs_momentum_percentile"].values)
    except Exception:
        pass
    return None


def _make_state_bar(state_counts: dict) -> go.Figure:
    """创建状态分布迷你柱状图"""
    all_states = [
        "①领涨减速", "②稳健上行", "③加速冲顶",
        "④强转弱", "⑤中性震荡", "⑥弱转强",
        "⑦持续杀跌", "⑧下跌中继", "⑨底背离",
    ]
    counts = [state_counts.get(s, 0) for s in all_states]
    colors = [STATE_COLORS.get(s, "#9E9E9E") for s in all_states]

    fig = go.Figure(go.Bar(x=all_states, y=counts, marker_color=colors, text=counts, textposition="auto"))
    fig.update_layout(
        height=250,
        margin={"l": 10, "r": 10, "t": 10, "b": 60},
        xaxis={"tickangle": 30},
        yaxis_title="板块数",
    )
    return fig


def _render_heatmap(state_df: pd.DataFrame):
    """渲染九宫格热力图（自定义HTML表格）"""
    if state_df is None or state_df.empty:
        st.warning("暂无数据")
        return

    # 九宫格定义
    grid = [
        # (row, col, state_label, description)
        (0, 0, "①领涨减速", "领涨减速"),
        (0, 1, "②稳健上行", "稳健上行"),
        (0, 2, "③加速冲顶", "加速冲顶"),
        (1, 0, "④强转弱", "强转弱"),
        (1, 1, "⑤中性震荡", "中性震荡"),
        (1, 2, "⑥弱转强", "弱转强"),
        (2, 0, "⑦持续杀跌", "持续杀跌"),
        (2, 1, "⑧下跌中继", "下跌中继"),
        (2, 2, "⑨底背离", "底背离"),
    ]

    # 统计每个格子的板块
    grid_data = {}
    for state_label in [g[2] for g in grid]:
        subset = state_df[state_df["state"] == state_label]
        grid_data[state_label] = {
            "count": len(subset),
            "sectors": list(subset["sector_name"].values) if len(subset) > 0 else [],
            "codes": list(subset["sector_code"].values) if len(subset) > 0 else [],
        }

    # 构建HTML表格
    html = """
    <style>
    .nine-grid { width: 100%; border-collapse: collapse; table-layout: fixed; }
    .nine-grid td { 
        padding: 12px; text-align: center; vertical-align: top; 
        border: 1px solid #e0e0e0; width: 33%; height: 140px;
    }
    .nine-grid .state-name { font-weight: bold; font-size: 14px; margin-bottom: 4px; }
    .nine-grid .count { font-size: 22px; font-weight: bold; margin-bottom: 4px; }
    .nine-grid .sectors { font-size: 11px; color: #666; line-height: 1.4; }
    </style>
    <table class="nine-grid">
    """

    for row in range(3):
        html += "<tr>"
        for col in range(3):
            cell = [g for g in grid if g[0] == row and g[1] == col][0]
            state_label = cell[2]
            desc = cell[3]
            data = grid_data[state_label]
            color = STATE_COLORS.get(state_label, "#9E9E9E")
            emoji = STATE_EMOJI.get(state_label, "")

            # 根据状态设置背景色
            if state_label in ["⑥弱转强", "⑨底背离"]:
                bg = "#E8F5E9"
            elif state_label == "③加速冲顶":
                bg = "#FFFDE7"
            elif state_label in ["①领涨减速", "④强转弱"]:
                bg = "#FFF3E0"
            elif state_label in ["⑦持续杀跌", "⑧下跌中继"]:
                bg = "#FFEBEE"
            elif state_label == "②稳健上行":
                bg = "#F1F8E9"
            else:
                bg = "#F5F5F5"

            sectors_html = "<br>".join(data["sectors"][:8])
            if len(data["sectors"]) > 8:
                sectors_html += f"<br>...等{len(data['sectors'])}个"

            html += f"""
            <td style="background-color:{bg};">
                <div class="state-name" style="color:{color};">{emoji} {state_label}</div>
                <div class="count" style="color:{color};">{data['count']}</div>
                <div class="sectors">{sectors_html or '-'}</div>
            </td>
            """

        html += "</tr>"

    html += "</table>"

    st.html(html)


def render():
    """渲染板块轮动监控页面"""
    st.title("板块轮动监控")
    st.markdown("九宫格热力图、板块强弱排行、重点关注区")

    with st.spinner("加载轮动数据..."):
        state_df = load_state_df()
        score_df = load_score_df()
        mirror_pairs = load_mirror_pairs()

    if state_df is None or state_df.empty:
        st.warning("暂无数据，请先运行数据更新流程")
        return

    # ================================================================
    # Tab切换：热力图 | 排行表 | 卡片详情
    # ================================================================
    tab1, tab2, tab3 = st.tabs(["九宫格热力图", "板块强弱排行", "板块卡片"])

    # ================================================================
    # Tab 1: 九宫格热力图
    # ================================================================
    with tab1:
        st.subheader("九宫格板块分布热力图")
        st.caption("横轴：RS动量方向 | 纵轴：价格趋势方向")

        _render_heatmap(state_df)

        # 状态分布柱状图
        state_counts = {}
        for s in state_df["state"].value_counts().index:
            state_counts[s] = int(state_df["state"].value_counts()[s])
        fig_bar = _make_state_bar(state_counts)
        st.plotly_chart(fig_bar, use_container_width=True)

        # 重点关注区
        st.subheader("重点关注")
        focus_states = ["⑥弱转强", "⑨底背离", "③加速冲顶"]
        focus_df = state_df[state_df["state"].isin(focus_states)]

        if not focus_df.empty:
            # 按状态分组显示
            for focus_state in focus_states:
                subset = focus_df[focus_df["state"] == focus_state]
                if subset.empty:
                    continue
                color = STATE_COLORS.get(focus_state, "#9E9E9E")
                emoji = STATE_EMOJI.get(focus_state, "")

                with st.expander(f"{emoji} {focus_state} ({len(subset)}个板块)", expanded=(focus_state in ["⑥弱转强", "⑨底背离"])):
                    for _, row in subset.iterrows():
                        c1, c2, c3 = st.columns([3, 1, 1])
                        with c1:
                            st.markdown(f"**{row['sector_name']}**")
                            st.caption(row["sector_code"])
                        with c2:
                            st.markdown(f"RS分位: {row['rs_percentile']:.1f}%" if row["rs_percentile"] is not None else "RS分位: N/A")
                        with c3:
                            st.markdown(f"RS动量: {row['rs_momentum_percentile']:.1f}%")

    # ================================================================
    # Tab 2: 板块强弱排行
    # ================================================================
    with tab2:
        st.subheader("板块综合评分排行")

        if score_df is not None and not score_df.empty:
            # 准备显示数据
            display_df = score_df.copy()
            display_df["排名"] = display_df["rank"]
            display_df["板块名称"] = display_df["sector_name"]
            display_df["板块代码"] = display_df["sector_code"]

            # 状态emoji列
            display_df["状态"] = display_df["state"].apply(
                lambda s: f"{STATE_EMOJI.get(s, '')} {s}" if s else "未知"
            )

            display_df["评分"] = display_df["score"].apply(lambda x: f"{x:.1f}")

            # 子评分
            display_df["RS位置"] = display_df["rs_position_score"].apply(lambda x: f"{x:.1f}")
            display_df["RS动量"] = display_df["rs_momentum_score"].apply(lambda x: f"{x:.1f}")
            display_df["趋势"] = display_df["trend_score"].apply(lambda x: f"{x:.1f}")

            # 选择显示列
            show_cols = ["排名", "板块名称", "板块代码", "状态", "评分", "RS位置", "RS动量", "趋势"]

            # 搜索过滤
            search = st.text_input("搜索板块名称或代码", placeholder="输入关键词筛选...")
            if search:
                mask = (
                    display_df["板块名称"].str.contains(search, na=False)
                    | display_df["板块代码"].str.contains(search, na=False)
                )
                display_df = display_df[mask]

            # 状态筛选
            available_states = sorted(score_df["state"].dropna().unique())
            selected_states = st.multiselect(
                "按状态筛选",
                available_states,
                default=[],
                format_func=lambda s: f"{STATE_EMOJI.get(s, '')} {s}",
            )
            if selected_states:
                display_df = display_df[display_df["state"].isin(selected_states)]

            # 渲染表格（带状态背景色）
            st.dataframe(
                display_df[show_cols],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "排名": st.column_config.NumberColumn(width="small"),
                    "评分": st.column_config.NumberColumn(width="small"),
                    "RS位置": st.column_config.NumberColumn(width="small"),
                    "RS动量": st.column_config.NumberColumn(width="small"),
                    "趋势": st.column_config.NumberColumn(width="small"),
                },
            )

            # 评分分布图
            st.subheader("评分分布")
            fig_score = px.histogram(
                score_df, x="score", nbins=20,
                color_discrete_sequence=["#4CAF50"],
                labels={"score": "综合评分"},
            )
            fig_score.update_layout(height=300, margin={"l": 10, "r": 10, "t": 10, "b": 10})
            st.plotly_chart(fig_score, use_container_width=True)
        else:
            st.warning("暂无评分数据")

    # ================================================================
    # Tab 3: 板块卡片
    # ================================================================
    with tab3:
        st.subheader("板块详情卡片")
        st.caption("三级联动：九宫格状态 / 状态切换 → 行业 → 详情卡片")

        # 加载 drill 状态数据
        drill_states = _load_drill_states()

        if drill_states is None or drill_states.empty:
            st.warning("暂无板块数据")
            return

        # --- 第 1 级：选择筛选维度 ---
        filter_mode = st.radio(
            "筛选维度",
            ["🎯 按九宫格状态筛选", "🔄 按状态切换筛选"],
            horizontal=True,
            key="rotation_tab3_filter",
        )

        # --- 第 2 级：按状态/切换筛选 → 选行业 ---
        if filter_mode.startswith("🎯"):
            _, matching_df = render_state_picker(drill_states)
        else:
            _, matching_df = render_transition_picker(drill_states)

        if matching_df is None or matching_df.empty:
            st.info("👆 请先选择筛选条件")
            return

        st.markdown("---")
        selected_code, sector_label = render_sector_picker(
            matching_df, label="选择行业查看详情", key="rotation_tab3_sector"
        )

        if not selected_code:
            return

        # --- 第 3 级：展示板块卡片 ---
        sector_info = state_df[state_df["sector_code"] == selected_code]
        if sector_info.empty:
            st.warning(f"未找到板块 {selected_code} 的数据")
            return
        sector_info = sector_info.iloc[0]

        # 获取评分
        sector_score = None
        if score_df is not None:
            score_row = score_df[score_df["sector_code"] == selected_code]
            if not score_row.empty:
                sector_score = score_row.iloc[0]["score"]

        # 获取镜像对信息
        mirror_info = None
        for mp in mirror_pairs:
            if mp.get("strong_sector") == selected_code:
                mirror_info = {
                    "name": mp.get("weak_name", get_sector_name(mp.get("weak_sector", ""))),
                    "state": mp.get("weak_state", ""),
                    "pair_type": mp.get("pair_type", ""),
                }
                break
            elif mp.get("weak_sector") == selected_code:
                mirror_info = {
                    "name": mp.get("strong_name", get_sector_name(mp.get("strong_sector", ""))),
                    "state": mp.get("strong_state", ""),
                    "pair_type": mp.get("pair_type", ""),
                }
                break

        # 获取RS历史（用于sparkline，缓存加速）
        rs_history = load_sector_rs_history(selected_code)

        render_state_card(
            sector_code=selected_code,
            sector_name=sector_info["sector_name"],
            state=sector_info["state"],
            score=sector_score,
            trend=sector_info["trend"],
            rs_percentile=sector_info.get("rs_percentile"),
            rs_momentum_percentile=sector_info["rs_momentum_percentile"],
            mirror_info=mirror_info,
            rs_history=rs_history,
        )

        # 镜像对提示
        if mirror_info:
            st.info(
                f"**镜像对验证**: {mirror_info.get('pair_type', '')} - "
                f"镜像板块: {mirror_info.get('name', 'N/A')} ({mirror_info.get('state', '')})"
            )


if __name__ == "__main__":
    render()

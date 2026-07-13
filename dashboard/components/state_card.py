"""
板块状态卡片组件
=================
可复用的板块状态信息卡片，用于轮动监控和板块详情页。
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import numpy as np

from config.sector_map import get_sector_name

# 状态颜色映射
STATE_COLORS = {
    "①领涨减速": "#FF9800",  # 橙色
    "②稳健上行": "#81C784",  # 浅绿
    "③加速冲顶": "#FFC107",  # 黄色
    "④强转弱":   "#FF9800",  # 橙色
    "⑤中性震荡": "#9E9E9E",  # 灰色
    "⑥弱转强":   "#4CAF50",  # 绿色
    "⑦持续杀跌": "#F44336",  # 红色
    "⑧下跌中继": "#E53935",  # 红色
    "⑨底背离":   "#2E7D32",  # 深绿
}

STATE_EMOJI = {
    "①领涨减速": "🟠",
    "②稳健上行": "🟢",
    "③加速冲顶": "🟡",
    "④强转弱":   "🟠",
    "⑤中性震荡": "⚪",
    "⑥弱转强":   "🟢",
    "⑦持续杀跌": "🔴",
    "⑧下跌中继": "🔴",
    "⑨底背离":   "🟢",
}


def _make_sparkline(values: list, color: str = "#4CAF50", height: int = 60) -> go.Figure:
    """创建迷你走势图"""
    if not values or len(values) < 2:
        values = [0, 0]
    fig = go.Figure(
        go.Scatter(
            y=list(values),
            mode="lines",
            line={"color": color, "width": 1.5},
            fill="tozeroy",
            fillcolor=f"rgba({','.join(map(str, _hex_to_rgba(color, 0.2)))})",
        )
    )
    fig.update_layout(
        height=height,
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis={"visible": False},
        yaxis={"visible": False},
        showlegend=False,
    )
    return fig


def _hex_to_rgba(hex_color: str, alpha: float = 1.0) -> tuple:
    """Hex颜色转RGBA元组"""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return (r, g, b, alpha)


def render_state_card(
    sector_code: str,
    sector_name: str = None,
    state: str = None,
    score: float = None,
    trend: str = None,
    rs_percentile: float = None,
    rs_momentum_percentile: float = None,
    mirror_info: dict = None,
    suggestion: str = None,
    rs_history: list = None,
):
    """
    渲染板块状态卡片

    参数:
        sector_code: 板块代码
        sector_name: 板块名称（可选，自动获取）
        state: 九宫格状态
        score: 综合评分 (0-100)
        trend: 价格趋势
        rs_percentile: RS分位数
        rs_momentum_percentile: RS动量分位数
        mirror_info: 镜像对信息 dict: {sector, state}
        suggestion: 操作建议
        rs_history: RS近期走势值列表（用于sparkline）
    """
    if sector_name is None:
        sector_name = get_sector_name(sector_code)

    state_color = STATE_COLORS.get(state, "#9E9E9E")
    state_emoji = STATE_EMOJI.get(state, "⚪")

    # 卡片外层容器
    with st.container(border=True):
        # 标题行：名称 + 状态
        col1, col2 = st.columns([3, 2])
        with col1:
            st.markdown(f"### {sector_name}")
            st.caption(sector_code)
        with col2:
            st.markdown(
                f"<h2 style='color:{state_color};text-align:right;margin:0;'>{state_emoji} {state or '未知'}</h2>",
                unsafe_allow_html=True,
            )

        # 评分进度条
        if score is not None:
            score_pct = score / 100.0
            st.markdown(f"**综合评分**")
            st.progress(score_pct, text=f"{score:.1f}分")

        # 指标行
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            trend_label = {"上涨": "↗", "横盘": "→", "下跌": "↘"}.get(trend, "→")
            st.metric("价格趋势", f"{trend_label} {trend or '未知'}")
        with m2:
            rs_label = f"{rs_percentile:.1f}%" if rs_percentile is not None else "N/A"
            st.metric("RS分位", rs_label)
        with m3:
            mom_label = f"{rs_momentum_percentile:.1f}%" if rs_momentum_percentile is not None else "N/A"
            st.metric("RS动量", mom_label)
        with m4:
            if mirror_info:
                mirror_name = mirror_info.get("name", mirror_info.get("sector", "N/A"))
                mirror_state = mirror_info.get("state", "")
                st.metric("镜像板块", f"{mirror_name}", delta=mirror_state)
            else:
                st.metric("镜像板块", "无")

        # 建议行
        if suggestion:
            st.info(f"**建议**: {suggestion}")

        # RS走势Sparkline
        if rs_history and len(rs_history) >= 2:
            fig = _make_sparkline(rs_history, color=state_color)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

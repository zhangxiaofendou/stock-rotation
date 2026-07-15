"""
板块详情页面
============
展示���定板块的K线图、RS走势、RS动量图、状态历史、当前状态卡片。
"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from model.state_machine import StateMachine
from model.scoring import SectorScoring
from model.transition import TransitionRules
from config.sector_map import get_sector_name
from dashboard.components.state_card import STATE_COLORS, STATE_EMOJI
from dashboard.components.drill_pickers import (
    load_all_sector_states,
    detect_state_transitions,
    render_state_picker,
    render_transition_picker,
    render_sector_picker,
)


@st.cache_resource
def get_stores():
    return ParquetStore(), SQLiteStore()


@st.cache_resource
def get_state_machine():
    ps, ss = get_stores()
    return StateMachine(ps, ss)


@st.cache_resource
def get_scoring():
    ps, ss = get_stores()
    sm = get_state_machine()
    return SectorScoring(ps, ss, sm)


@st.cache_data(ttl=86400)
def load_sector_state_series(sector_code: str):
    """加载板块历史状态序列（独立缓存）"""
    sm = get_state_machine()
    return sm.calc_state_series(sector_code)


@st.cache_data(ttl=86400)
def load_sector_kline(sector_code: str):
    """加载板块K线数据"""
    ps, _ = get_stores()
    df = ps.load_index_hist(sector_code)
    if df is None:
        return None

    # 标准化列名
    col_map = {}
    for col in df.columns:
        if col in ["日期", "date", "trade_date"]:
            col_map[col] = "date"
        elif col in ["收盘", "close"]:
            col_map[col] = "close"
        elif col in ["开盘", "open"]:
            col_map[col] = "open"
        elif col in ["最高", "high"]:
            col_map[col] = "high"
        elif col in ["最低", "low"]:
            col_map[col] = "low"
        elif col in ["成交量", "volume", "vol"]:
            col_map[col] = "volume"

    if col_map:
        df = df.rename(columns=col_map)

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

    # 计算均线
    if "close" in df.columns:
        df["MA5"] = df["close"].rolling(5).mean()
        df["MA20"] = df["close"].rolling(20).mean()
        df["MA60"] = df["close"].rolling(60).mean()

    return df


@st.cache_data(ttl=86400)
def load_sector_rs(sector_code: str):
    """加载板块RS数据"""
    import os
    from config.settings import PARQUET_DIR

    rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "rs")
    safe_code = sector_code.replace(".", "_")
    rs_path = os.path.join(rs_dir, f"{safe_code}.parquet")

    if not os.path.exists(rs_path):
        return None

    df = pd.read_parquet(rs_path)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _render_kline_chart(kline_df: pd.DataFrame, sector_name: str):
    """渲染K线图"""
    if kline_df is None or kline_df.empty:
        st.warning("暂无K线数据")
        return

    # 只显示最近250个交易日
    plot_df = kline_df.tail(250).copy()

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.7, 0.3],
    )

    # K线
    if all(c in plot_df.columns for c in ["open", "high", "low", "close"]):
        fig.add_trace(
            go.Candlestick(
                x=plot_df["date"],
                open=plot_df["open"],
                high=plot_df["high"],
                low=plot_df["low"],
                close=plot_df["close"],
                name="K线",
                increasing_line_color="#F44336",
                decreasing_line_color="#4CAF50",
            ),
            row=1, col=1,
        )

    # 均线
    for ma_name, ma_color in [("MA5", "#FF9800"), ("MA20", "#2196F3"), ("MA60", "#9C27B0")]:
        if ma_name in plot_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=plot_df["date"],
                    y=plot_df[ma_name],
                    mode="lines",
                    name=ma_name,
                    line={"color": ma_color, "width": 1},
                ),
                row=1, col=1,
            )

    # 成交量
    if "volume" in plot_df.columns:
        colors = []
        for i in range(len(plot_df)):
            if i > 0 and "close" in plot_df.columns:
                colors.append("#F44336" if plot_df["close"].iloc[i] >= plot_df["close"].iloc[i - 1] else "#4CAF50")
            else:
                colors.append("#9E9E9E")
        fig.add_trace(
            go.Bar(x=plot_df["date"], y=plot_df["volume"], name="成交量", marker_color=colors, opacity=0.5),
            row=2, col=1,
        )

    fig.update_layout(
        title=f"{sector_name} 日K线图",
        height=550,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1)

    st.plotly_chart(fig, use_container_width=True)


def _render_rs_chart(rs_df: pd.DataFrame):
    """渲染RS走势图"""
    if rs_df is None or rs_df.empty:
        st.warning("暂无RS数据")
        return

    # 只显示最近250天
    plot_df = rs_df.tail(250).copy()

    fig = go.Figure()

    # RS分位区间带
    if "rs_percentile" in plot_df.columns:
        # 250日范围
        fig.add_trace(
            go.Scatter(
                x=list(plot_df["date"]) + list(plot_df["date"])[::-1],
                y=[0] * len(plot_df) + [100] * len(plot_df),
                fill="toself",
                fillcolor="rgba(200,200,200,0.1)",
                line={"width": 0},
                name="全范围",
                showlegend=False,
            )
        )

        fig.add_trace(
            go.Scatter(
                x=plot_df["date"],
                y=plot_df["rs_percentile"],
                mode="lines",
                name="RS分位",
                line={"color": "#4CAF50", "width": 2},
                fill="tozeroy",
                fillcolor="rgba(76,175,80,0.1)",
            )
        )

        # 70/30参考线
        fig.add_hline(y=70, line_dash="dash", line_color="#FF9800", annotation_text="70%")
        fig.add_hline(y=30, line_dash="dash", line_color="#FF9800", annotation_text="30%")
        fig.add_hline(y=50, line_dash="dot", line_color="#9E9E9E")

    fig.update_layout(
        title="RS相对强弱走势",
        height=400,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        yaxis={"range": [0, 100], "title": "RS分位数 (%)"},
        hovermode="x unified",
    )

    st.plotly_chart(fig, use_container_width=True)


def _render_rs_momentum_chart(rs_df: pd.DataFrame):
    """渲染RS动量图"""
    if rs_df is None or rs_df.empty or "rs_momentum_percentile" not in rs_df.columns:
        st.warning("暂无RS动量数据")
        return

    plot_df = rs_df.tail(250).copy()

    fig = go.Figure()

    fig.add_trace(
        go.Scatter(
            x=plot_df["date"],
            y=plot_df["rs_momentum_percentile"],
            mode="lines",
            name="RS动量分位",
            line={"color": "#2196F3", "width": 2},
            fill="tozeroy",
            fillcolor="rgba(33,150,243,0.1)",
        )
    )

    # 阈值线
    fig.add_hline(y=70, line_dash="dash", line_color="#FF9800", annotation_text="70% 增强")
    fig.add_hline(y=30, line_dash="dash", line_color="#FF9800", annotation_text="30% 减弱")
    fig.add_hline(y=50, line_dash="dot", line_color="#9E9E9E")

    fig.update_layout(
        title="RS动量分位数走势",
        height=400,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        yaxis={"range": [0, 100], "title": "RS动量分位数 (%)"},
        hovermode="x unified",
    )

    st.plotly_chart(fig, use_container_width=True)


def _render_state_history(state_series: pd.DataFrame):
    """渲染状态历史"""
    if state_series is None or state_series.empty:
        st.warning("暂无状态历史数据")
        return

    # 最近20天
    recent = state_series.tail(20).copy()

    # 构建状态标签
    labels = []
    for _, row in recent.iterrows():
        date_str = row["date"].strftime("%m-%d") if hasattr(row["date"], "strftime") else str(row["date"])[-5:]
        state_str = row["state"]
        labels.append(f"{date_str}\n{state_str}")

    colors = [STATE_COLORS.get(s, "#9E9E9E") for s in recent["state"]]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=list(range(len(recent))),
            y=[1] * len(recent),
            marker_color=colors,
            text=labels,
            textposition="auto",
            textfont={"size": 10},
            hovertext=[f"{row['date'].strftime('%Y-%m-%d')}: {row['state']}<br>趋势: {row['trend']}<br>RS动量: {row['rs_momentum_percentile']:.1f}%"
                        if hasattr(row['date'], 'strftime')
                        else f"{row['date']}: {row['state']}" for _, row in recent.iterrows()],
            hoverinfo="text",
        )
    )

    fig.update_layout(
        title="最近20天状态变化",
        height=200,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        xaxis={"showticklabels": False},
        yaxis={"showticklabels": False, "range": [0, 2]},
        showlegend=False,
    )

    st.plotly_chart(fig, use_container_width=True)


def _get_suggestion(state: str, prev_state: str) -> str:
    """根据状态变化获取操作建议"""
    rules = TransitionRules()
    action, logic = rules.get_transition_action(prev_state, state)
    return f"{action}: {logic}"


def render():
    """渲染板块详情页面 — 三级联动：九宫格状态/切换 → 行业 → 板块详情"""
    st.title("板块详情")
    st.caption("三级联动：九宫格状态 / 状态切换 → 行业 → 板块详情")

    # ================================================================
    # 第 1 级：选择筛选维度（状态 or 切换）
    # ================================================================
    filter_mode = st.radio(
        "筛选维度",
        ["🎯 按九宫格状态筛选", "🔄 按状态切换筛选"],
        horizontal=True,
    )

    state_df = load_all_sector_states()
    if state_df is None or state_df.empty:
        st.warning("暂无板块数据")
        return

    # ================================================================
    # 第 2 级：按状态/切换筛选 → 选行业
    # ================================================================
    if filter_mode.startswith("🎯"):
        _, matching_df = render_state_picker(state_df)
    else:
        _, matching_df = render_transition_picker(state_df)

    if matching_df is None or matching_df.empty:
        st.info("👆 请先选择筛选条件")
        return

    st.markdown("---")
    sector_code, sector_label = render_sector_picker(
        matching_df, label="选择行业查看详情", key="sector_detail_picker"
    )

    if not sector_code:
        return

    sector_name = get_sector_name(sector_code)

    # 加载详情数据
    with st.spinner(f"加载 {sector_name} 数据..."):
        kline_df = load_sector_kline(sector_code)
        rs_df = load_sector_rs(sector_code)
        state_series = load_sector_state_series(sector_code)

    # ================================================================
    # 当前状态卡片
    # ================================================================
    st.subheader("当前状态")

    if state_df is not None:
        sector_info = state_df[state_df["sector_code"] == sector_code]
        if not sector_info.empty:
            info = sector_info.iloc[0]

            # 获取评分
            scoring = get_scoring()
            score_info = scoring.calc_score(sector_code)
            score_val = score_info["score"] if score_info else None

            # 获取前一状态和建议
            prev_state = "⑤中性震荡"
            suggestion = None
            if state_series is not None and len(state_series) >= 2:
                prev_state = state_series.iloc[-2]["state"]
                try:
                    suggestion = _get_suggestion(info["state"], prev_state)
                except Exception:
                    pass

            # 状态卡片
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                color = STATE_COLORS.get(info["state"], "#9E9E9E")
                st.markdown(
                    f"<h1 style='color:{color};margin:0;'>{STATE_EMOJI.get(info['state'], '')} {info['state']}</h1>",
                    unsafe_allow_html=True,
                )
            with col2:
                st.metric("综合评分", f"{score_val:.1f}" if score_val else "N/A")
                st.metric("价格趋势", info["trend"])
            with col3:
                st.metric("RS分位", f"{info['rs_percentile']:.1f}%" if info.get("rs_percentile") is not None else "N/A")
                st.metric("RS动量", f"{info['rs_momentum_percentile']:.1f}%" if info.get("rs_momentum_percentile") is not None else "N/A")
            with col4:
                st.metric("前一日状态", prev_state)
                if suggestion:
                    st.info(suggestion)

    # ================================================================
    # K线图
    # ================================================================
    st.subheader("K线图")
    _render_kline_chart(kline_df, sector_name)

    # ================================================================
    # RS走势图
    # ================================================================
    st.subheader("RS走势")
    _render_rs_chart(rs_df)

    # ================================================================
    # RS动量图
    # ================================================================
    st.subheader("RS动量")
    _render_rs_momentum_chart(rs_df)

    # ================================================================
    # 状态历史
    # ================================================================
    st.subheader("状态历史")
    _render_state_history(state_series)

    # 状态序列表格
    if state_series is not None and not state_series.empty:
        with st.expander("查看完整状态序列"):
            display_series = state_series.tail(60).copy()
            display_series["date"] = display_series["date"].apply(
                lambda d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
            )
            display_series = display_series.sort_values("date", ascending=False)
            st.dataframe(display_series, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    render()

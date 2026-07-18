"""
板块轮动监控页面（核心页面）
===========================
展示九宫格热力图、板块强弱排行、重点关注区、板块卡片。
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
from config.settings import PARQUET_DIR
from dashboard.components.state_card import (
    STATE_COLORS, STATE_EMOJI,
    get_state_signal, get_state_signal_color, get_signal_legend,
)
from dashboard.components.drill_pickers import (
    load_all_sector_states as _load_drill_states,
    render_state_picker,
    render_transition_picker,
    render_sector_picker,
)
from dashboard.pages.stock_drill import render as render_stock_drill

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
    return sm, scoring


@st.cache_data(ttl=86400)  # 每日凌晨更新一次，24h TTL
def load_state_df():
    """加载板块状态（独立缓存）"""
    sm, _ = get_models()
    return sm.calc_all_sectors_state()


@st.cache_data(ttl=86400)
def load_score_df():
    """加载板块评分排行（独立缓存）"""
    _, scoring = get_models()
    return scoring.calc_all_scores()


@st.cache_data(ttl=3600)
def load_badge_map():
    """扫描 trend parquet 末行，返回 {code: trend_badge}。

    trend_badge 含义：
        负 = MA5 已下穿 MA20 的连续天数（死叉破位，绿底）
        正 = MA5 已上穿 MA20 的连续天数（金叉破位，红底）
        0  = 无角标（中性粘合 / 完整多头 / 完整空头）
    """
    trend_dir = os.path.join(str(PARQUET_DIR), "indicators", "trend")
    out = {}
    if not os.path.exists(trend_dir):
        return out
    for f in os.listdir(trend_dir):
        if not f.endswith(".parquet"):
            continue
        code = f.replace(".parquet", "").replace("_", ".", 1)
        if not code.endswith(".SI"):
            code = code.replace("_SI", ".SI")
        try:
            df = pd.read_parquet(os.path.join(trend_dir, f))
            if "trend_badge" in df.columns and not df.empty:
                v = df["trend_badge"].iloc[-1]
                out[code] = int(v) if pd.notna(v) else 0
        except Exception:
            pass
    return out


@st.cache_data(ttl=86400)
def load_sector_kline(sector_code: str):
    """加载板块K线（标准化列名 + 均线），取最近250日由图表函数处理"""
    ps, _ = get_stores()
    df = ps.load_index_hist(sector_code)
    if df is None:
        return None

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

    if "close" in df.columns:
        df["MA5"] = df["close"].rolling(5).mean()
        df["MA20"] = df["close"].rolling(20).mean()
        df["MA60"] = df["close"].rolling(60).mean()

    return df


def _trend_text(trend: str, badge) -> str:
    """列表单元格用的趋势文本（含角标天数）"""
    if trend == "上涨":
        return "↑ 上涨"
    if trend == "下跌":
        return "↓ 下跌"
    # 横盘
    if badge is None:
        return "→ 横盘"
    if badge < 0:
        return f"→ 横盘 {badge}"
    if badge > 0:
        return f"→ 横盘 +{badge}"
    return "→ 横盘"


def _render_state_badge(trend: str, badge, state: str):
    """右侧详情的状态标签，横盘时渲染真实角标（绿底负 / 红底正）"""
    badge_html = ""
    if trend == "横盘" and badge not in (None, 0):
        if badge < 0:
            badge_html = (
                f'<span style="margin-left:8px;padding:1px 8px;border-radius:10px;font-size:13px;font-weight:700;'
                f'color:#16a34a;background:rgba(22,163,74,.14);border:1px solid rgba(22,163,74,.45);" '
                f'title="下穿20日线 {abs(badge)} 天（空方破位）">{badge}</span>'
            )
        else:
            badge_html = (
                f'<span style="margin-left:8px;padding:1px 8px;border-radius:10px;font-size:13px;font-weight:700;'
                f'color:#e23c3c;background:rgba(226,60,60,.14);border:1px solid rgba(226,60,60,.45);" '
                f'title="上穿20日线 {badge} 天（多方破位）">+{badge}</span>'
            )
    color = STATE_COLORS.get(state, "#9E9E9E")
    emoji = STATE_EMOJI.get(state, "")
    st.markdown(
        f'<div style="font-size:22px;font-weight:700;color:{color};">{emoji} {state}{badge_html}</div>',
        unsafe_allow_html=True,
    )


def _render_kline_chart(kline_df: pd.DataFrame, sector_name: str):
    """250日K线图（红涨绿跌 + MA5/20/60 + 成交量）"""
    if kline_df is None or kline_df.empty:
        st.warning("暂无K线数据")
        return

    plot_df = kline_df.tail(250).copy()

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.7, 0.3],
    )

    if all(c in plot_df.columns for c in ["open", "high", "low", "close"]):
        fig.add_trace(
            go.Candlestick(
                x=plot_df["date"], open=plot_df["open"], high=plot_df["high"],
                low=plot_df["low"], close=plot_df["close"],
                name="K线", increasing_line_color="#F44336", decreasing_line_color="#4CAF50",
            ),
            row=1, col=1,
        )

    for ma_name, ma_color in [("MA5", "#FF9800"), ("MA20", "#2196F3"), ("MA60", "#9C27B0")]:
        if ma_name in plot_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=plot_df["date"], y=plot_df[ma_name], mode="lines",
                    name=ma_name, line={"color": ma_color, "width": 1},
                ),
                row=1, col=1,
            )

    if "volume" in plot_df.columns:
        colors = []
        for i in range(len(plot_df)):
            if i > 0 and "close" in plot_df.columns:
                colors.append("#F44336" if plot_df["close"].iloc[i] >= plot_df["close"].iloc[i - 1] else "#4CAF50")
            else:
                colors.append("#9E9E9E")
        fig.add_trace(
            go.Bar(x=plot_df["date"], y=plot_df["volume"], name="成交量",
                    marker_color=colors, opacity=0.5),
            row=2, col=1,
        )

    fig.update_layout(
        title=f"{sector_name} 日K线（近250日）",
        height=520,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1)
    st.plotly_chart(fig, width="stretch")


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
        border: 1px solid #e0e0e0; width: 33%; height: 150px;
    }
    .nine-grid .state-name { font-weight: bold; font-size: 14px; margin-bottom: 4px; }
    .nine-grid .count { font-size: 22px; font-weight: bold; margin-bottom: 4px; }
    .nine-grid .sectors { font-size: 11px; color: #666; line-height: 1.4; }
    .nine-grid .signal-pill {
        display: inline-block; margin: 4px 0 6px; padding: 1px 10px;
        border-radius: 10px; font-size: 12px; font-weight: 700; color: #fff;
    }
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
            signal = get_state_signal(state_label)
            signal_color = get_state_signal_color(state_label)

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
                <div class="signal-pill" style="background-color:{signal_color};">{signal}</div>
                <div class="count" style="color:{color};">{data['count']}</div>
                <div class="sectors">{sectors_html or '-'}</div>
            </td>
            """

        html += "</tr>"

    html += "</table>"

    # 信号图例
    legend_items = []
    for item in get_signal_legend():
        states = "、".join(s[1:] for s in item["states"])  # 去圆圈数字前缀
        legend_items.append(
            f'<span style="display:inline-block;margin:6px 14px 6px 0;font-size:13px;color:#333;">'
            f'<span style="display:inline-block;padding:1px 9px;border-radius:10px;color:#fff;'
            f'font-weight:700;background-color:{item["color"]};">{item["signal"]}</span>'
            f' <span style="color:#666;">{item["desc"]}（{states}）</span></span>'
        )
    html += '<div style="margin-top:10px;line-height:1.8;">' + "".join(legend_items) + "</div>"

    st.html(html)


@st.cache_data(ttl=86400)
def load_sector_state_series(sector_code: str):
    """加载板块历史状态序列（独立缓存）"""
    sm, _ = get_models()
    return sm.calc_state_series(sector_code)


@st.cache_data(ttl=86400)
def load_sector_rs(sector_code: str):
    """加载板块RS数据"""
    rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "rs")
    safe_code = sector_code.replace(".", "_")
    rs_path = os.path.join(rs_dir, f"{safe_code}.parquet")

    if not os.path.exists(rs_path):
        return None

    df = pd.read_parquet(rs_path)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _render_rs_chart(rs_df: pd.DataFrame):
    """渲染RS走势图"""
    if rs_df is None or rs_df.empty:
        st.warning("暂无RS数据")
        return
    if "rs_percentile" not in rs_df.columns:
        st.warning("暂无RS分位数据")
        return

    plot_df = rs_df.tail(250).copy()

    fig = go.Figure()
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
    st.plotly_chart(fig, width="stretch")


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
    st.plotly_chart(fig, width="stretch")


def _render_state_history(state_series: pd.DataFrame):
    """渲染状态历史（最近20天）"""
    if state_series is None or state_series.empty:
        st.warning("暂无状态历史数据")
        return

    recent = state_series.tail(20).copy()

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
            hovertext=[
                f"{row['date'].strftime('%Y-%m-%d')}: {row['state']}<br>趋势: {row['trend']}<br>RS动量: {row['rs_momentum_percentile']:.1f}%"
                if hasattr(row["date"], "strftime")
                else f"{row['date']}: {row['state']}"
                for _, row in recent.iterrows()
            ],
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
    st.plotly_chart(fig, width="stretch")


def _get_suggestion(state: str, prev_state: str) -> str:
    """根据状态变化获取操作建议"""
    rules = TransitionRules()
    action, logic = rules.get_transition_action(prev_state, state)
    return f"{action}: {logic}"


def render():
    """渲染板块轮动监控页面"""
    st.title("板块轮动监控")
    st.markdown("九宫格热力图、板块详情、趋势验证、个股下钻")

    with st.spinner("加载轮动数据..."):
        state_df = load_state_df()
        score_df = load_score_df()

    if state_df is None or state_df.empty:
        st.warning("暂无数据，请先运行数据更新流程")
        return

    # ================================================================
    # Tab切换：热力图 | 板块详情 | 趋势验证 | 个股下钻
    # ================================================================
    tab1, tab2, tab3, tab4 = st.tabs(["九宫格热力图", "板块详情", "趋势验证", "个股下钻"])

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
    # Tab 2: 板块详情（原独立页面迁入，替换板块卡片）
    # ================================================================
    with tab2:
        st.subheader("板块详情")
        st.caption("三级联动：九宫格状态 / 状态切换 → 行业 → 板块详情")

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

        sector_name = get_sector_name(selected_code)

        # 加载详情数据
        with st.spinner(f"加载 {sector_name} 数据..."):
            kline_df = load_sector_kline(selected_code)
            rs_df = load_sector_rs(selected_code)
            state_series = load_sector_state_series(selected_code)

        # ================================================================
        # 当前状态卡片
        # ================================================================
        st.subheader("当前状态")

        sector_info = state_df[state_df["sector_code"] == selected_code]
        if not sector_info.empty:
            info = sector_info.iloc[0]

            # 获取评分（复用排行数据，避免重复计算）
            score_val = None
            if score_df is not None:
                score_row = score_df[score_df["sector_code"] == selected_code]
                if not score_row.empty:
                    score_val = score_row.iloc[0]["score"]

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
                st.metric("综合评分", f"{score_val:.1f}" if score_val is not None else "N/A")
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

    # ================================================================
    # Tab 3: 趋势验证（板块趋势对照 + 250日K线）
    # ================================================================
    with tab3:
        st.subheader("板块趋势验证（价格趋势 vs K线）")
        st.caption("对照全板块「绝对价格趋势（含横盘穿越天数角标）、综合评分、RS分位、动量分位、横截面排名、九宫格状态」与 250 日 K 线，人工验证状态判定合理性。")

        st.info(
            "📐 趋势分界线：上穿60日线 = 上涨（红）｜ 击穿60日线 = 下跌（绿）｜ "
            "横盘带角标：死叉破位显示**绿色负数**（已下穿20日线天数）、金叉破位显示**红色正数**（已上穿20日线天数）"
        )

        with st.spinner("加载板块状态..."):
            badge_map = load_badge_map()

        if state_df is None or state_df.empty:
            st.warning("暂无数据，请先运行数据更新流程")
        else:
            data_date = str(state_df["date"].iloc[0])[:10] if "date" in state_df.columns else "未知"
            st.caption(f"📅 数据截面：{data_date}　共 {len(state_df)} 个板块")

            # 综合评分映射（取自板块强弱排行的 score_df）
            score_map = {}
            if score_df is not None and not score_df.empty:
                score_map = {
                    str(c): s for c, s in zip(score_df["sector_code"], score_df["score"])
                }

            # 组装显示表
            rows = []
            for _, r in state_df.iterrows():
                code = r["sector_code"]
                badge = badge_map.get(code, 0)
                rows.append({
                    "板块名称": r["sector_name"],
                    "趋势": _trend_text(r["trend"], badge),
                    "RS分位(%)": round(float(r["rs_percentile"]), 1) if r["rs_percentile"] is not None else None,
                    "动量分位(%)": round(float(r["rs_momentum_percentile"]), 1) if r["rs_momentum_percentile"] is not None else None,
                    "横截面(%)": round(float(r["rs_momentum_cross_pct"]), 1) if r.get("rs_momentum_cross_pct") is not None else None,
                    "综合评分": round(float(score_map[str(code)]), 1) if str(code) in score_map and score_map[str(code)] is not None else None,
                    "九宫格状态": f"{STATE_EMOJI.get(r['state'], '')} {r['state']}",
                    "板块代码": code,
                })
            df = pd.DataFrame(rows)

            # 状态排序：③②①⑥⑨⑤④⑧⑦
            STATE_ORD = {"③": 0, "②": 1, "①": 2, "⑥": 3, "⑨": 4, "⑤": 5, "④": 6, "⑧": 7, "⑦": 8}
            df["_o"] = df["九宫格状态"].str[1].map(STATE_ORD).fillna(9)
            df = df.sort_values("_o").drop(columns="_o").reset_index(drop=True)

            st.subheader("全板块趋势对照（点击左侧行查看右侧K线）")

            colL, colR = st.columns([0.95, 1.05])

            with colL:
                st.dataframe(
                    df,
                    key="trend_table",
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="single-row",
                    height=660,
                    width="stretch",
                    column_config={
                        "板块名称": st.column_config.TextColumn("板块名称", width="medium"),
                        "趋势": st.column_config.TextColumn("趋势", width="small"),
                        "RS分位(%)": st.column_config.NumberColumn("RS分位(%)", format="%.1f", width="small"),
                        "动量分位(%)": st.column_config.NumberColumn("动量分位(%)", format="%.1f", width="small"),
                        "横截面(%)": st.column_config.NumberColumn("横截面(%)", format="%.1f", width="small"),
                        "综合评分": st.column_config.NumberColumn("综合评分", format="%.1f", width="small"),
                        "九宫格状态": st.column_config.TextColumn("九宫格状态", width="medium"),
                        "板块代码": st.column_config.TextColumn("代码", width="small"),
                    },
                )

                # 读取选中行（首次未点选时默认第一行）
                sel = st.session_state.get("trend_table", {})
                sel_rows = sel.get("selection", {}).get("rows", []) if sel else []
                if sel_rows:
                    chosen = df.iloc[sel_rows[0]]
                else:
                    chosen = df.iloc[0]
                chosen_code = chosen["板块代码"]
                chosen_name = chosen["板块名称"]
                srow = state_df[state_df["sector_code"] == chosen_code].iloc[0]
                chosen_trend = srow["trend"]
                chosen_badge = badge_map.get(chosen_code, 0)
                chosen_state = srow["state"]

            with colR:
                st.markdown(f"**{chosen_name}（{chosen_code}）**")
                _render_state_badge(chosen_trend, chosen_badge, chosen_state)

                # 交易信号（买卖建议）
                sig = get_state_signal(chosen_state)
                sig_color = get_state_signal_color(chosen_state)
                st.markdown(
                    f'<div style="margin:2px 0 8px;">交易信号：'
                    f'<span style="display:inline-block;padding:1px 12px;border-radius:10px;'
                    f'color:#fff;font-weight:700;font-size:14px;background-color:{sig_color};">{sig}</span></div>',
                    unsafe_allow_html=True,
                )

                # 趋势语义说明
                if chosen_trend == "横盘" and isinstance(chosen_badge, (int, float)) and chosen_badge < 0:
                    st.caption(f"横盘·死叉破位：MA5 已下穿 MA20 第 {abs(chosen_badge)} 天（尚未击穿 MA60），空方动能释放中，等待企稳或破位。")
                elif chosen_trend == "横盘" and isinstance(chosen_badge, (int, float)) and chosen_badge > 0:
                    st.caption(f"横盘·金叉破位：MA5 已上穿 MA20 第 {chosen_badge} 天（尚未上穿 MA60），多方试探，等待确认。")
                elif chosen_trend == "横盘":
                    st.caption("横盘·中性粘合：均线交织，方向不明。")
                elif chosen_trend == "上涨":
                    st.caption("上涨：MA5 > MA20 > MA60，完整多头排列。")
                else:
                    st.caption("下跌：MA5 < MA20 < MA60，完整空头排列。")

                with st.spinner("加载K线..."):
                    kline = load_sector_kline(chosen_code)
                _render_kline_chart(kline, chosen_name)

    # ================================================================
    # Tab 4: 个股下钻（原独立页面迁入，作为一个页签）
    # ================================================================
    with tab4:
        render_stock_drill()


if __name__ == "__main__":
    render()

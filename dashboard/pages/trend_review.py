# -*- coding: utf-8 -*-
"""
趋势验证页面
===========
罗列全板块的「绝对价格趋势（含横盘穿越天数角标）、RS分位、动量分位、横截面排名、九宫格状态」，
并与最近 250 天 K 线对照，用于人工验证状态判定的合理性。

趋势分界线定义（绝对价格趋势）：
- 上涨 = MA5 > MA20 > MA60（上穿60，完整多头排列）
- 下跌 = MA5 < MA20 < MA60（击穿60，完整空头排列）
- 横盘（带角标）= 过渡态：
    · 死叉（MA5<MA20 但未击穿60）→ 角标 负数、绿底（已下穿20日线 N 天）
    · 金叉（MA5>MA20 但未上穿60）→ 角标 正数、红底（已上穿20日线 N 天）
    · 中性粘合 → 无角标
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
from config.settings import PARQUET_DIR
from dashboard.components.state_card import STATE_COLORS, STATE_EMOJI


# ================================================================
# 缓存：存储 / 模型 / 数据
# ================================================================
@st.cache_resource
def get_stores():
    return ParquetStore(), SQLiteStore()


@st.cache_resource
def get_state_machine():
    ps, ss = get_stores()
    return StateMachine(ps, ss)


@st.cache_data(ttl=86400)
def load_state_df():
    """全板块最新日状态（含 trend / state / rs 分位 / 横截面）"""
    sm = get_state_machine()
    return sm.calc_all_sectors_state()


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


# ================================================================
# 辅助：文本 / 角标 / 图表
# ================================================================
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


def _render_heatmap(state_df: pd.DataFrame):
    """九宫格热力图（自定义HTML表格）"""
    if state_df is None or state_df.empty:
        st.warning("暂无数据")
        return

    grid = [
        (0, 0, "①领涨减速", "领涨减速"), (0, 1, "②稳健上行", "稳健上行"), (0, 2, "③加速冲顶", "加速冲顶"),
        (1, 0, "④强转弱", "强转弱"), (1, 1, "⑤中性震荡", "中性震荡"), (1, 2, "⑥弱转强", "弱转强"),
        (2, 0, "⑦持续杀跌", "持续杀跌"), (2, 1, "⑧下跌中继", "下跌中继"), (2, 2, "⑨底背离", "底背离"),
    ]

    grid_data = {}
    for state_label in [g[2] for g in grid]:
        subset = state_df[state_df["state"] == state_label]
        grid_data[state_label] = {
            "count": len(subset),
            "sectors": list(subset["sector_name"].values) if len(subset) > 0 else [],
        }

    GRID_BG = {
        "⑥弱转强": "#E8F5E9", "⑨底背离": "#E8F5E9", "③加速冲顶": "#FFFDE7",
        "①领涨减速": "#FFF3E0", "④强转弱": "#FFF3E0", "⑦持续杀跌": "#FFEBEE",
        "⑧下跌中继": "#FFEBEE", "②稳健上行": "#F1F8E9", "⑤中性震荡": "#F5F5F5",
    }

    html = """
    <style>
    .nine-grid { width:100%; border-collapse:collapse; table-layout:fixed; }
    .nine-grid td { padding:12px; text-align:center; vertical-align:top; border:1px solid #e0e0e0; width:33%; height:130px; }
    .nine-grid .sn { font-weight:bold; font-size:14px; margin-bottom:4px; }
    .nine-grid .cnt { font-size:22px; font-weight:bold; margin-bottom:4px; }
    .nine-grid .sc { font-size:11px; color:#666; line-height:1.4; }
    </style>
    <table class="nine-grid">
    """
    for row in range(3):
        html += "<tr>"
        for col in range(3):
            cell = [g for g in grid if g[0] == row and g[1] == col][0]
            sl = cell[2]
            bg = GRID_BG.get(sl, "#F5F5F5")
            emoji = STATE_EMOJI.get(sl, "")
            color = STATE_COLORS.get(sl, "#9E9E9E")
            sc = "<br>".join(grid_data[sl]["sectors"][:8])
            if len(grid_data[sl]["sectors"]) > 8:
                sc += f"<br>...等{len(grid_data[sl]['sectors'])}个"
            html += f"""
            <td style="background-color:{bg};">
                <div class="sn" style="color:{color};">{emoji} {sl}</div>
                <div class="cnt" style="color:{color};">{grid_data[sl]['count']}</div>
                <div class="sc">{sc or '-'}</div>
            </td>
            """
        html += "</tr>"
    html += "</table>"
    st.html(html)


def _make_state_bar(state_counts: dict) -> go.Figure:
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


# ================================================================
# 主渲染
# ================================================================
def render():
    """渲染趋势验证页面"""
    st.title("趋势验证")
    st.caption("罗列全板块价格趋势（含横盘穿越天数角标）、RS分位、动量分位、横截面排名与九宫格状态，对照250日K线人工验证。")

    st.info(
        "📐 趋势分界线：上穿60日线 = 上涨（红）｜ 击穿60日线 = 下跌（绿）｜ "
        "横盘带角标：死叉破位显示**绿色负数**（已下穿20日线天数）、金叉破位显示**红色正数**（已上穿20日线天数）"
    )

    with st.spinner("加载板块状态..."):
        state_df = load_state_df()
        badge_map = load_badge_map()

    if state_df is None or state_df.empty:
        st.warning("暂无数据，请先运行数据更新流程")
        return

    data_date = str(state_df["date"].iloc[0])[:10] if "date" in state_df.columns else "未知"
    st.caption(f"📅 数据截面：{data_date}　共 {len(state_df)} 个板块")

    tab1, tab2 = st.tabs(["板块趋势对照", "九宫格分布"])

    # ================================================================
    # Tab 2: 九宫格分布（总览）
    # ================================================================
    with tab2:
        st.subheader("九宫格板块分布")
        _render_heatmap(state_df)

        state_counts = state_df["state"].value_counts().to_dict()
        st.plotly_chart(_make_state_bar(state_counts), width="stretch")

    # ================================================================
    # Tab 1: 板块趋势对照（主）
    # ================================================================
    with tab1:
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


if __name__ == "__main__":
    render()

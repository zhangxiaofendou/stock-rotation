"""
九宫格网格组件
===============
3×3网格可视化，用Plotly散点图展示板块在九宫格中的位置。
X轴：相对强弱趋势（减弱/走平/增强）
Y轴：绝对价格趋势（上涨/横盘/下跌）
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

# 状态颜色映射（与state_card保持一致）
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

STATE_EMOJI = {
    "①领涨减速": "①",
    "②稳健上行": "②",
    "③加速冲顶": "③",
    "④强转弱":   "④",
    "⑤中性震荡": "⑤",
    "⑥弱转强":   "⑥",
    "⑦持续杀跌": "⑦",
    "⑧下跌中继": "⑧",
    "⑨底背离":   "⑨",
}

# 九宫格标签
GRID_CELLS = [
    # (x, y, x_label, y_label, state, description)
    (0, 2, "减弱", "上涨", "①领涨减速", "领涨减速"),
    (1, 2, "走平", "上涨", "②稳健上行", "稳健上行"),
    (2, 2, "增强", "上涨", "③加速冲顶", "加速冲顶"),
    (0, 1, "减弱", "横盘", "④强转弱", "强转弱"),
    (1, 1, "走平", "横盘", "⑤中性震荡", "中性震荡"),
    (2, 1, "增强", "横盘", "⑥弱转强", "弱转强"),
    (0, 0, "减弱", "下跌", "⑦持续杀跌", "持续杀跌"),
    (1, 0, "走平", "下跌", "⑧下跌中继", "下跌中继"),
    (2, 0, "增强", "下跌", "⑨底背离", "底背离"),
]


def _rs_dir(rs_momentum_percentile):
    """RS动量方向 -> 网格X坐标"""
    if rs_momentum_percentile is None or pd.isna(rs_momentum_percentile):
        return 1  # 走平
    if rs_momentum_percentile > 70:
        return 2  # 增强
    elif rs_momentum_percentile < 30:
        return 0  # 减弱
    else:
        return 1  # 走平


def _trend_y(trend):
    """价格趋势 -> 网格Y坐标"""
    mapping = {"上涨": 2, "横盘": 1, "下跌": 0}
    return mapping.get(trend, 1)


def render_state_grid(state_df: pd.DataFrame):
    """
    渲染九宫格网格

    参数:
        state_df: DataFrame, 包含 sector_code, sector_name, state, trend,
                  rs_percentile, rs_momentum_percentile
    """
    if state_df is None or state_df.empty:
        st.warning("暂无板块状态数据")
        return

    # 为每个板块计算网格坐标
    plot_df = state_df.copy()
    plot_df["grid_x"] = plot_df["rs_momentum_percentile"].apply(_rs_dir)
    plot_df["grid_y"] = plot_df["trend"].apply(_trend_y)

    # 添加微小随机偏移避免完全重叠
    import random
    random.seed(42)
    offsets = []
    seen = {}
    for _, row in plot_df.iterrows():
        key = (row["grid_x"], row["grid_y"])
        idx = seen.get(key, 0)
        seen[key] = idx + 1
        # 根据序号偏移
        if idx == 0:
            offsets.append((0, 0))
        else:
            offsets.append((random.uniform(-0.25, 0.25), random.uniform(-0.25, 0.25)))
    plot_df["ox"] = [o[0] for o in offsets]
    plot_df["oy"] = [o[1] for o in offsets]

    fig = go.Figure()

    # 绘制网格背景矩形
    for x, y, x_label, y_label, state, desc in GRID_CELLS:
        color = STATE_COLORS.get(state, "#9E9E9E")
        fig.add_shape(
            type="rect",
            x0=x - 0.5, x1=x + 0.5,
            y0=y - 0.5, y1=y + 0.5,
            line={"color": "lightgray", "width": 0.5},
            fillcolor=f"rgba{(*_hex_to_rgba(color, 0.08),)}",
            layer="below",
        )
        # 格子标签
        fig.add_annotation(
            x=x, y=y + 0.35,
            text=f"<b>{state}</b><br><span style='font-size:10px'>{desc}</span>",
            showarrow=False,
            font={"size": 11, "color": color},
            align="center",
        )

    # 按状态分组绘制散点
    for state_val in plot_df["state"].unique():
        subset = plot_df[plot_df["state"] == state_val]
        color = STATE_COLORS.get(state_val, "#9E9E9E")
        emoji = STATE_EMOJI.get(state_val, "")

        fig.add_trace(
            go.Scatter(
                x=subset["grid_x"] + subset["ox"],
                y=subset["grid_y"] + subset["oy"],
                mode="markers+text",
                text=subset["sector_name"].apply(lambda n: n[:4]),  # 缩写
                textposition="top center",
                textfont={"size": 8, "color": color},
                marker={
                    "size": 10,
                    "color": color,
                    "opacity": 0.8,
                    "line": {"width": 1, "color": "white"},
                },
                name=state_val,
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>"
                    "代码: %{customdata[1]}<br>"
                    "状态: %{customdata[2]}<br>"
                    "RS分位: %{customdata[3]:.1f}%<br>"
                    "RS动量: %{customdata[4]:.1f}%<br>"
                    "<extra></extra>"
                ),
                customdata=subset[
                    ["sector_name", "sector_code", "state", "rs_percentile", "rs_momentum_percentile"]
                ].values,
            )
        )

    # 布局
    fig.update_layout(
        xaxis={
            "tickmode": "array",
            "tickvals": [0, 1, 2],
            "ticktext": ["减弱", "走平", "增强"],
            "title": "RS动量方向",
            "range": [-0.8, 2.8],
            "zeroline": False,
            "gridcolor": "lightgray",
        },
        yaxis={
            "tickmode": "array",
            "tickvals": [0, 1, 2],
            "ticktext": ["下跌", "横盘", "上涨"],
            "title": "价格趋势",
            "range": [-0.8, 2.8],
            "zeroline": False,
            "gridcolor": "lightgray",
        },
        height=600,
        margin={"l": 60, "r": 20, "t": 40, "b": 60},
        legend={"orientation": "h", "yanchor": "bottom", "y": -0.2, "xanchor": "center", "x": 0.5},
        hovermode="closest",
    )

    # 统计每个格子的板块数
    for x, y, x_label, y_label, state, desc in GRID_CELLS:
        count = len(plot_df[(plot_df["grid_x"] == x) & (plot_df["grid_y"] == y)])
        color = STATE_COLORS.get(state, "#9E9E9E")
        fig.add_annotation(
            x=x, y=y - 0.35,
            text=f"<span style='color:{color};font-size:11px'>{count}个板块</span>",
            showarrow=False,
            font={"size": 10},
        )

    st.plotly_chart(fig, width="stretch")


def _hex_to_rgba(hex_color: str, alpha: float = 1.0) -> tuple:
    """Hex颜色转RGBA元组"""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return (r, g, b, alpha)

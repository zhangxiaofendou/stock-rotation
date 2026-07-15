"""
三层联动选择器（共享组件）
==========================
九宫格状态/切换 → 行业 → 个股，所有页面统一的板块筛选入口。

使用方式:
    from dashboard.components.drill_pickers import (
        load_all_sector_states,
        detect_state_transitions,
        render_state_picker,
        render_transition_picker,
        render_sector_picker,
        render_state_grid_visual,
        STATE_COLORS,
        GRID_LAYOUT,
    )
"""

import streamlit as st
import pandas as pd
import numpy as np
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from model.state_machine import StateMachine
from config.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 缓存资源
# ============================================================
@st.cache_resource
def get_state_machine():
    return StateMachine()


# ============================================================
# 九宫格常量
# ============================================================
GRID_LAYOUT = [
    ["①领涨减速", "②稳健上行", "③加速冲顶"],
    ["④强转弱", "⑤中性震荡", "⑥弱转强"],
    ["⑦持续杀跌", "⑧下跌中继", "⑨底背离"],
]

STATE_COLORS = {
    "①领涨减速": ("#FFF3E0", "#E65100"),
    "②稳健上行": ("#E8F5E9", "#2E7D32"),
    "③加速冲顶": ("#FCE4EC", "#C62828"),
    "④强转弱":   ("#FFF8E1", "#F9A825"),
    "⑤中性震荡": ("#ECEFF1", "#546E7A"),
    "⑥弱转强":   ("#E3F2FD", "#1565C0"),
    "⑦持续杀跌": ("#FFEBEE", "#B71C1C"),
    "⑧下跌中继": ("#F3E5F5", "#6A1B9A"),
    "⑨底背离":   ("#E0F2F1", "#00695C"),
}


# ============================================================
# 数据加载
# ============================================================
_STATE_SNAPSHOT = None


def load_all_sector_states():
    """加载所有板块的九宫格状态（全局缓存，只加载一次）"""
    global _STATE_SNAPSHOT
    if _STATE_SNAPSHOT is not None:
        return _STATE_SNAPSHOT
    sm = get_state_machine()
    try:
        df = sm.calc_all_sectors_state()
        if df is not None and not df.empty:
            _STATE_SNAPSHOT = df
            return df
    except Exception as e:
        logger.warning(f"加载板块状态失败: {e}")
    return pd.DataFrame()


def detect_state_transitions(all_states_df, days_back=5):
    """检测最近N天内的状态切换

    返回:
        pd.DataFrame: sector_code, sector_name, from_state, to_state,
                      transition_date, state_change
    """
    sm = get_state_machine()
    transitions = []

    if all_states_df is None or all_states_df.empty:
        return pd.DataFrame()

    for _, row in all_states_df.iterrows():
        code = row["sector_code"]
        try:
            state_series = sm.calc_state_series(code)
            if state_series is None or state_series.empty or len(state_series) < 2:
                continue

            recent = state_series.tail(days_back + 2)
            states = recent["state"].tolist()
            dates = recent["date"].tolist()

            for i in range(1, len(states)):
                if states[i] != states[i - 1]:
                    transitions.append({
                        "sector_code": code,
                        "sector_name": row.get("sector_name", code),
                        "from_state": states[i - 1],
                        "to_state": states[i],
                        "state_change": f"{states[i-1]} → {states[i]}",
                        "date": dates[i],
                    })
        except Exception:
            continue

    if not transitions:
        return pd.DataFrame()

    df = pd.DataFrame(transitions)
    df = df.sort_values("date", ascending=False).reset_index(drop=True)
    return df


# ============================================================
# 可视化组件
# ============================================================
def render_state_grid_visual(all_states_df, highlight_state=None):
    """渲染 3×3 九宫格可视化（含各状态板块数统计）"""
    grid_states = GRID_LAYOUT

    state_counts = {}
    if all_states_df is not None and not all_states_df.empty:
        counts = all_states_df["state"].value_counts()
        for s, c in counts.items():
            state_counts[s] = c

    rows_html = ""
    for row in grid_states:
        cells = ""
        for state in row:
            bg, text_color = STATE_COLORS.get(state, ("#ECEFF1", "#546E7A"))
            count = state_counts.get(state, 0)

            if state == highlight_state:
                cells += (
                    f'<td style="background:{bg};color:{text_color};padding:16px 12px;'
                    f'text-align:center;font-size:14px;font-weight:bold;'
                    f'border:3px solid #FF6F00;border-radius:6px;">'
                    f'📍 {state}<br><small>{count} 个板块</small>'
                    f'</td>'
                )
            else:
                opacity = "0.5" if count == 0 else "0.8"
                cells += (
                    f'<td style="background:{bg};color:{text_color};padding:12px 10px;'
                    f'text-align:center;font-size:13px;opacity:{opacity};border-radius:4px;">'
                    f'{state}<br><small>{count} 个板块</small>'
                    f'</td>'
                )
        rows_html += f"<tr>{cells}</tr>"

    table_html = (
        f'<table style="width:100%;border-collapse:separate;border-spacing:6px;">'
        f'{rows_html}'
        f'</table>'
    )
    st.markdown(table_html, unsafe_allow_html=True)


# ============================================================
# 三层联动选择器
# ============================================================
def render_state_picker(all_states_df, key="state_picker"):
    """第一层：按九宫格状态筛选 → 返回 (选定状态, 匹配板块DataFrame)"""
    st.markdown("#### 🎯 按九宫格状态筛选")

    render_state_grid_visual(all_states_df)

    grid_states = [s for row in GRID_LAYOUT for s in row]

    col1, col2 = st.columns([3, 2])
    with col1:
        selected_state = st.selectbox(
            "选择九宫格状态",
            options=[""] + grid_states,
            format_func=lambda x: "请选择状态..." if x == "" else x,
            key=key,
        )

    if not selected_state:
        with col2:
            st.metric("全部板块", f"{len(all_states_df)} 个")
        return None, None

    matching_df = all_states_df[all_states_df["state"] == selected_state].copy()

    with col2:
        st.metric("符合条件的板块", f"{len(matching_df)} 个")
        buy_states = {"⑨底背离", "⑥弱转强"}
        sell_states = {"①领涨减速", "④强转弱", "⑦持续杀跌"}
        if selected_state in buy_states:
            st.caption("🟢 买入信号状态")
        elif selected_state in sell_states:
            st.caption("🔴 卖出信号状态")
        else:
            st.caption("🟡 中性/持有状态")

    if matching_df.empty:
        st.warning(f"当前没有板块处于「{selected_state}」状态")

    return selected_state, matching_df


def render_transition_picker(all_states_df, key="transition_picker"):
    """第一层：按状态切换筛选 → 返回 (选定切换类型, 匹配板块DataFrame)"""
    st.markdown("#### 🔄 按状态切换筛选")

    trans_df = detect_state_transitions(all_states_df, days_back=5)

    if trans_df.empty:
        st.info("最近5天没有板块发生状态切换")
        st.metric("全部板块", f"{len(all_states_df)} 个")
        return None, None

    buy_targets = {"⑨底背离", "⑥弱转强"}
    sell_targets = {"①领涨减速", "④强转弱", "⑦持续杀跌"}

    transition_types = trans_df.groupby("state_change").agg(
        count=("sector_code", "count"),
        sectors=("sector_name", lambda x: list(x)),
        latest_date=("date", "max"),
    ).reset_index()
    transition_types = transition_types.sort_values("count", ascending=False)

    st.markdown("##### 最近5天状态切换统计")

    for _, row in transition_types.iterrows():
        state_chg = row["state_change"]
        count = row["count"]
        sectors = row["sectors"]
        latest = str(row["latest_date"])[:10]

        to_s = state_chg.split(" → ")[-1] if " → " in state_chg else ""
        badge = ""
        if to_s in buy_targets:
            badge = " 🟢买入"
        elif to_s in sell_targets:
            badge = " 🔴卖出"

        with st.expander(f"**{state_chg}**{badge} — {count} 个板块 | 最近: {latest}", expanded=False):
            for s in sectors[:20]:
                sc = all_states_df[all_states_df["sector_name"] == s]
                sc_state = sc.iloc[0]["state"] if not sc.empty else ""
                st.caption(f"• {s} [当前: {sc_state}]")
            if count > 20:
                st.caption(f"... 还有 {count - 20} 个板块")

    st.markdown("---")

    transition_options = [""] + list(transition_types["state_change"])
    selected_transition = st.selectbox(
        "选择具体的状态切换",
        options=transition_options,
        format_func=lambda x: "请选择切换类型..." if x == "" else x,
        key=key,
    )

    if not selected_transition:
        st.info("👆 请先选择一个状态切换类型")
        return None, None

    matching_trans = trans_df[trans_df["state_change"] == selected_transition]
    matching_codes = matching_trans["sector_code"].unique()

    matching_df = all_states_df[all_states_df["sector_code"].isin(matching_codes)].copy()
    matching_df = matching_df.merge(
        matching_trans[["sector_code", "state_change", "date"]].drop_duplicates("sector_code"),
        on="sector_code", how="left",
    )

    st.metric("符合条件的板块", f"{len(matching_df)} 个")

    if matching_df.empty:
        st.warning(f"没有板块发生「{selected_transition}」切换")

    return selected_transition, matching_df


def render_sector_picker(matching_df, label="选择行业查看个股", key="sector_picker"):
    """第二层：行业选择器 — 仅显示匹配的板块 → 返回 (code, label_text)"""
    if matching_df is None or matching_df.empty:
        return None, None

    st.markdown(f"#### 📊 {label}")

    sector_options = {"": "请选择行业"}
    for _, row in matching_df.iterrows():
        code = row["sector_code"]
        name = row.get("sector_name", code)
        state = row.get("state", "")
        state_change = row.get("state_change", None)
        rs = row.get("rs_momentum_percentile", None)
        rs_str = f" | RS: {rs:.0f}%" if rs is not None and not pd.isna(rs) else ""

        summary = f"{name} ({code}) [{state}]{rs_str}"
        if state_change is not None and not pd.isna(state_change):
            summary += f" | 切换: {state_change}"
        sector_options[code] = summary

    selected_code = st.selectbox(
        "选择行业",
        options=list(sector_options.keys()),
        format_func=lambda x: sector_options[x],
        key=key,
    )

    if not selected_code:
        return None, None

    return selected_code, sector_options[selected_code]

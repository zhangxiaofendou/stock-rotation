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

from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from model.state_machine import StateMachine
from config.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 缓存资源
# ============================================================
@st.cache_resource
def get_state_machine():
    """创建带数据存储的状态机，供状态切换检测读取历史 RS/趋势序列。"""
    return StateMachine(ParquetStore(), SQLiteStore())


# ============================================================
# 九宫格常量
# ============================================================
GRID_LAYOUT = [
    ["①领涨减速", "②稳健上行", "③加速冲顶"],
    ["④强转弱", "⑤中性震荡", "⑥弱转强"],
    ["⑦持续杀跌", "⑧下跌中继", "⑨底背离"],
]

# 趋势分组：根据状态切换的目标状态判定切换后的方向
TREND_OF_STATE = {
    "①领涨减速": "上涨",
    "②稳健上行": "上涨",
    "③加速冲顶": "上涨",
    "④强转弱":   "下跌",
    "⑤中性震荡": "横盘",
    "⑥弱转强":   "上涨",
    "⑦持续杀跌": "下跌",
    "⑧下跌中继": "下跌",
    "⑨底背离":   "上涨",
}
TREND_ORDER = {"下跌": 0, "上涨": 1, "横盘": 2}
TREND_EMOJI = {"下跌": "📉", "上涨": "📈", "横盘": "➡️"}

# 动作排序：按目标状态的交易信号分组
ACTION_ORDER = {"卖出": 0, "买入": 1, "持有": 2, "观望": 3}


# 状态颜色：背景色 + 文字色（用于九宫格可视化）
STATE_COLORS = {
    "①领涨减速": ("#FFF3E0", "#E65100"),
    "②稳健上行": ("#E8F5E9", "#2E7D32"),
    "③加速冲顶": ("#FFFDE7", "#F57F17"),
    "④强转弱":   ("#FFF3E0", "#E65100"),
    "⑤中性震荡": ("#ECEFF1", "#546E7A"),
    "⑥弱转强":   ("#E3F2FD", "#1565C0"),
    "⑦持续杀跌": ("#FFEBEE", "#B71C1C"),
    "⑧下跌中继": ("#F3E5F5", "#6A1B9A"),
    "⑨底背离":   ("#E0F2F1", "#00695C"),
}


def _get_state_meta(state: str) -> tuple:
    """返回状态的趋势方向与交易信号"""
    signal = StateMachine.STATE_SIGNAL_MAP.get(state, StateMachine.SIGNAL_WATCH)
    trend = TREND_OF_STATE.get(state, "横盘")
    return trend, signal


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
    """检测最近 N 个交易日内的状态切换。

    `days_back` 按状态序列末尾的交易日计数，自动覆盖周末和节假日；
    因而「最近5天」展示的是最近 5 个交易日的切换，而非自然日。

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
        except Exception as e:
            logger.warning(f"检测板块 {code} 状态切换失败: {e}")
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

    days_back = 5
    trans_df = detect_state_transitions(all_states_df, days_back=days_back)

    if trans_df.empty:
        st.info(f"最近 {days_back} 个交易日没有板块发生状态切换")
        st.metric("全部板块", f"{len(all_states_df)} 个")
        return None, None

    buy_targets = {"⑨底背离", "⑥弱转强"}
    sell_targets = {"①领涨减速", "④强转弱", "⑦持续杀跌"}
    signal_colors = {
        StateMachine.SIGNAL_SELL: "#e23c3c",
        StateMachine.SIGNAL_BUY:  "#16a34a",
        StateMachine.SIGNAL_HOLD: "#f59e0b",
        StateMachine.SIGNAL_WATCH: "#9e9e9e",
    }

    def _extract_to_state(state_change: str) -> str:
        return state_change.split(" → ")[-1] if " → " in state_change else ""

    transition_types = trans_df.groupby("state_change").agg(
        count=("sector_code", "count"),
        sectors=("sector_name", lambda x: list(x)),
        latest_date=("date", "max"),
    ).reset_index()
    transition_types["to_state"] = transition_types["state_change"].apply(_extract_to_state)
    transition_types["trend"] = transition_types["to_state"].map(lambda s: TREND_OF_STATE.get(s, "横盘"))
    transition_types["action"] = transition_types["to_state"].map(lambda s: StateMachine.STATE_SIGNAL_MAP.get(s, StateMachine.SIGNAL_WATCH))
    transition_types["trend_order"] = transition_types["trend"].map(TREND_ORDER)
    transition_types["action_order"] = transition_types["action"].map(ACTION_ORDER)
    transition_types = transition_types.sort_values(
        ["latest_date", "trend_order", "action_order", "count"],
        ascending=[False, True, True, False],
    ).reset_index(drop=True)

    total_transitions = int(transition_types["count"].sum())
    st.markdown(f"##### 最近 {days_back} 个交易日状态切换统计 · 共 {total_transitions} 次")

    # 按日期 → 趋势 → 动作 层级展示
    from itertools import groupby

    for latest_date, date_group in groupby(transition_types.to_dict("records"), key=lambda r: r["latest_date"]):
        date_str = str(latest_date)[:10]
        st.markdown(f"### 📅 {date_str}")

        for trend, trend_group in groupby(date_group, key=lambda r: r["trend"]):
            trend_group = list(trend_group)
            trend_total = sum(int(r["count"]) for r in trend_group)
            st.markdown(
                f"**{TREND_EMOJI.get(trend, '')} {trend}** "
                f"<span style='color:#666; font-size:13px;'>({trend_total} 个板块)</span>",
                unsafe_allow_html=True,
            )

            for row in trend_group:
                state_chg = row["state_change"]
                count = int(row["count"])
                sectors = row["sectors"]
                action = row["action"]
                action_color = signal_colors.get(action, "#9e9e9e")
                action_badge = f"<span style='color:{action_color}; font-weight:700;'>{action}</span>"

                with st.expander(f"{state_chg} · {action_badge} · {count} 个板块", expanded=False):
                    # 使用紧凑的列布局减少空白
                    for i, s in enumerate(sectors[:20]):
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

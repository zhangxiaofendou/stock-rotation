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


def detect_state_transitions(all_states_df, days_back=5, latest_only=False):
    """检测板块状态切换。

    默认扫描最近 `days_back` 个交易日的历史切换，供个股下钻等历史回顾使用。
    `latest_only=True` 时仅比较最近两个交易日，返回「上一交易日状态 → 当前状态」；
    这是板块轮动监控的状态切换筛选语义，确保目标状态与当前状态一致。

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

            if latest_only:
                # 当前状态切换：只比较上一交易日与最新交易日。
                # 目标状态必然等于状态快照中的当前状态，避免历史变更混入。
                recent = state_series.tail(2)
            else:
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

    # 此处只展示「上一交易日 → 当前交易日」的实时状态切换，
    # 历史多日切换不混入，避免目标状态与当前状态不一致。
    trans_df = detect_state_transitions(all_states_df, latest_only=True)

    if trans_df.empty:
        st.info("最新交易日没有板块发生状态切换")
        st.metric("全部板块", f"{len(all_states_df)} 个")
        return None, None

    action_emoji = {
        StateMachine.SIGNAL_SELL: "🔴",
        StateMachine.SIGNAL_BUY: "🟢",
        StateMachine.SIGNAL_HOLD: "🟡",
        StateMachine.SIGNAL_WATCH: "⚪",
    }

    def _extract_state(state_change: str, position: int) -> str:
        """从“前一状态 → 当前状态”中取指定一端的状态。"""
        parts = state_change.split(" → ")
        return parts[position] if len(parts) == 2 else ""

    transition_types = trans_df.groupby("state_change").agg(
        count=("sector_code", "count"),
        sectors=("sector_name", lambda x: list(x)),
        latest_date=("date", "max"),
    ).reset_index()
    transition_types["from_state"] = transition_types["state_change"].apply(
        lambda x: _extract_state(x, 0)
    )
    transition_types["to_state"] = transition_types["state_change"].apply(
        lambda x: _extract_state(x, 1)
    )
    transition_types["trend"] = transition_types["to_state"].map(
        lambda s: TREND_OF_STATE.get(s, "横盘")
    )
    transition_types["from_action"] = transition_types["from_state"].map(
        lambda s: StateMachine.STATE_SIGNAL_MAP.get(s, StateMachine.SIGNAL_WATCH)
    )
    transition_types["to_action"] = transition_types["to_state"].map(
        lambda s: StateMachine.STATE_SIGNAL_MAP.get(s, StateMachine.SIGNAL_WATCH)
    )
    transition_types["trend_order"] = transition_types["trend"].map(TREND_ORDER)
    transition_types["action_order"] = transition_types["to_action"].map(ACTION_ORDER)
    transition_types = transition_types.sort_values(
        ["latest_date", "trend_order", "action_order", "count"],
        ascending=[False, True, True, False],
    ).reset_index(drop=True)

    total_transitions = int(transition_types["count"].sum())
    st.markdown(f"##### 上一交易日 → 当前交易日状态切换 · 共 {total_transitions} 个板块")
    st.caption("直接点击任意状态切换，即可筛选下方行业；再次点击其他切换可切换筛选条件。")

    selection_key = f"{key}_selected"
    valid_transitions = set(transition_types["state_change"])
    selected_transition = st.session_state.get(selection_key, "")
    if selected_transition not in valid_transitions:
        selected_transition = ""
        st.session_state.pop(selection_key, None)

    # 按日期 → 趋势 → 动作 层级展示；每条列表项本身就是筛选按钮。
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

            for row_index, row in enumerate(trend_group):
                state_chg = row["state_change"]
                count = int(row["count"])
                from_action = row["from_action"]
                to_action = row["to_action"]
                signal_change = (
                    f"{action_emoji.get(from_action, '')} {from_action}"
                    f" → {action_emoji.get(to_action, '')} {to_action}"
                )
                is_selected = state_chg == selected_transition
                button_label = (
                    f"✓ 当前已选 · {state_chg} · 信号：{signal_change} · {count} 个板块"
                    if is_selected
                    else f"{state_chg} · 信号：{signal_change} · {count} 个板块"
                )

                if st.button(
                    button_label,
                    key=f"{key}_option_{date_str}_{trend}_{row_index}",
                    type="primary" if is_selected else "secondary",
                    width="stretch",
                ):
                    st.session_state[selection_key] = state_chg
                    selected_transition = state_chg
                    st.rerun()

        st.markdown("---")

    if not selected_transition:
        st.info("👆 请直接点击上方任意状态切换类型")
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

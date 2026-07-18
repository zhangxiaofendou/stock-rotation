"""
镜像对监控页面
==============
展示镜像对列表、资金迁移网络图、信号验证。
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import numpy as np
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from model.state_machine import StateMachine
from model.mirror_pair import MirrorPair
from config.sector_map import get_sector_name, SECTOR_GROUPS
from dashboard.components.state_card import STATE_COLORS, STATE_EMOJI
from dashboard.components.drill_pickers import (
    load_all_sector_states as _load_drill_states,
    render_state_picker,
    render_transition_picker,
    render_sector_picker,
)


@st.cache_resource
def get_stores():
    return ParquetStore(), SQLiteStore()


@st.cache_resource
def get_models():
    ps, ss = get_stores()
    sm = StateMachine(ps, ss)
    mirror = MirrorPair(ss, sm)
    return sm, mirror


@st.cache_data(ttl=86400)
def load_mirror_data():
    """加载镜像对数据（独立缓存）"""
    _, mirror = get_models()
    mirror_pairs = mirror.find_mirror_pairs()
    return mirror_pairs


@st.cache_data(ttl=86400)
def load_state_for_mirror():
    """加载板块状态（独立缓存）"""
    sm, _ = get_models()
    return sm.calc_all_sectors_state()


def _render_sankey(mirror_pairs: list):
    """渲染资金流向Sankey图"""
    if not mirror_pairs:
        st.info("暂无镜像对数据")
        return

    # 构建节点和链接
    # 源: 弱板块(④/⑦) -> 目标: 强板块(⑥/③)
    node_labels = []
    node_colors = []
    node_map = {}  # label -> index

    links_source = []
    links_target = []
    links_value = []
    links_color = []

    for mp in mirror_pairs:
        weak_label = f"{mp['weak_name']}\n({mp['weak_state']})"
        strong_label = f"{mp['strong_name']}\n({mp['strong_state']})"

        for label in [weak_label, strong_label]:
            if label not in node_map:
                node_map[label] = len(node_labels)
                node_labels.append(label)
                # 弱板块红色系，强板块绿色系
                if "④" in label or "⑦" in label:
                    node_colors.append("#F44336")
                else:
                    node_colors.append("#4CAF50")

        links_source.append(node_map[weak_label])
        links_target.append(node_map[strong_label])
        links_value.append(max(mp.get("confidence", 0.5) * 10, 1))
        links_color.append("rgba(200,200,200,0.4)")

    fig = go.Figure(
        go.Sankey(
            node={
                "pad": 15,
                "thickness": 20,
                "line": {"color": "gray", "width": 0.5},
                "label": node_labels,
                "color": node_colors,
            },
            link={
                "source": links_source,
                "target": links_target,
                "value": links_value,
                "color": links_color,
            },
        )
    )

    fig.update_layout(
        title=dict(
            text="资金迁移路径（弱→强）",
            font={"size": 14, "color": "black"},
        ),
        height=400 + len(node_labels) * 20,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        font={"size": 12, "color": "black"},
        paper_bgcolor="white",
    )

    st.plotly_chart(fig, use_container_width=True)


def _render_mirror_table(mirror_pairs: list):
    """渲染镜像对列表"""
    if not mirror_pairs:
        st.info("暂无镜像对数据")
        return

    table_data = []
    for mp in mirror_pairs:
        table_data.append({
            "弱板块": f"{STATE_EMOJI.get(mp['weak_state'], '')} {mp['weak_name']}",
            "弱状态": mp["weak_state"],
            "强板块": f"{STATE_EMOJI.get(mp['strong_state'], '')} {mp['strong_name']}",
            "强状态": mp["strong_state"],
            "关联组": mp["group"],
            "配对类型": mp["pair_type"],
            "置信度": f"{mp['confidence']:.1%}",
        })

    df = pd.DataFrame(table_data)

    # 置信度排序
    df = df.sort_values("置信度", ascending=False)

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "置信度": st.column_config.NumberColumn(
                format="%.1f%%",
            ),
        },
    )


def _render_signal_validation(state_df: pd.DataFrame):
    """渲染信号验证 — 三级联动：状态/切换 → 行业 → 信号验证"""
    st.subheader("交叉验证信号")
    st.caption("三级联动：九宫格状态 / 状态切换 → 行业 → 信号验证")

    _, mirror = get_models()

    drill_states = _load_drill_states()
    if drill_states is None or drill_states.empty:
        st.warning("暂无板块数据")
        return

    # --- 第 1 级：选择筛选维度 ---
    filter_mode = st.radio(
        "筛选维度",
        ["🎯 按九宫格状态筛选", "🔄 按状态切换筛选"],
        horizontal=True,
        key="mirror_signal_filter",
    )

    # --- 第 2 级：按状态/切换筛选 → 选行业 ---
    if filter_mode.startswith("🎯"):
        selected_state, matching_df = render_state_picker(drill_states, key="mirror_signal_state")
    else:
        selected_state, matching_df = render_transition_picker(drill_states, key="mirror_signal_transition")

    if matching_df is None or matching_df.empty:
        st.info("👆 请先选择筛选条件")
        return

    st.markdown("---")
    # 信号验证只对极端状态有意义，提示用户
    extreme_states = {"⑥弱转强", "④强转弱", "③加速冲顶", "⑦持续杀跌"}
    if filter_mode.startswith("🎯") and selected_state and selected_state not in extreme_states:
        st.info(f"💡 当前选择的状态「{selected_state}」不是极端信号状态。信号交叉验证对 ⑥弱转强、④强转弱、③加速冲顶、⑦持续杀跌 最有价值。")

    selected_code, sector_label = render_sector_picker(
        matching_df, label="选择行业进行信号验证", key="mirror_signal_sector"
    )

    if not selected_code:
        return

    # 获取板块信息
    sector_info = drill_states[drill_states["sector_code"] == selected_code]
    if sector_info.empty:
        st.warning(f"未找到板块 {selected_code} 的数据")
        return
    sector_info = sector_info.iloc[0]

    with st.spinner("验证中..."):
        is_valid, mirror_code, confidence = mirror.validate_signal(
            selected_code,
            sector_info["state"],
        )

    if is_valid:
        if mirror_code:
            mirror_name = get_sector_name(mirror_code)
            st.success(
                f"验证通过: {sector_info['sector_name']}({sector_info['state']}) "
                f"↔ {mirror_name}({get_opposite_state(sector_info['state'])}) "
                f"置信度: {confidence:.1%}"
            )
        else:
            st.success(
                f"验证通过: {sector_info['sector_name']}({sector_info['state']}) "
                f"- 非极端状态，无需交叉验证"
            )
    else:
        st.error(
            f"验证未通过: {sector_info['sector_name']}({sector_info['state']}) - "
            f"未在关联组内找到镜像板块"
        )


def get_opposite_state(state: str) -> str:
    """获取对立状态"""
    opposite = {
        "⑥弱转强": "④强转弱",
        "④强转弱": "⑥弱转强",
        "③加速冲顶": "⑦持续杀跌",
        "⑦持续杀跌": "③加速冲顶",
    }
    return opposite.get(state, "")


def render():
    """渲染镜像对监控页面"""
    st.title("镜像对监控")
    st.markdown("监控板块间资金迁移的镜像对关系")

    with st.spinner("加载镜像对数据..."):
        mirror_pairs = load_mirror_data()
        state_df = load_state_for_mirror()

    # 统计概览
    st.subheader("概览")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("镜像对总数", len(mirror_pairs))
    with col2:
        # 强弱转换对
        sw_pairs = [mp for mp in mirror_pairs if mp["pair_type"] == "强弱转换镜像对"]
        st.metric("④↔⑥强弱转换", len(sw_pairs))
    with col3:
        # 极端状态对
        ex_pairs = [mp for mp in mirror_pairs if mp["pair_type"] == "极端状态镜像对"]
        st.metric("③↔⑦极端状态", len(ex_pairs))

    # ================================================================
    # 镜像对列表
    # ================================================================
    st.subheader("镜像对列表")
    _render_mirror_table(mirror_pairs)

    # ================================================================
    # 资金迁移网络图
    # ================================================================
    st.markdown(
        '<span style="color:#000000;font-weight:normal;font-size:1.25rem;">资金迁移路径</span>',
        unsafe_allow_html=True,
    )
    st.caption("展示资金从弱势板块(④/⑦)流向强势板块(⑥/③)的路径")
    _render_sankey(mirror_pairs)

    # ================================================================
    # 按关联组展示
    # ================================================================
    st.subheader("按关联组查看")

    groups = {}
    for mp in mirror_pairs:
        g = mp["group"]
        if g not in groups:
            groups[g] = []
        groups[g].append(mp)

    if groups:
        for group_name in sorted(groups.keys()):
            with st.expander(f"{group_name} ({len(groups[group_name])}对)", expanded=len(groups) <= 3):
                group_pairs = groups[group_name]
                for mp in group_pairs:
                    c1, c2, c3 = st.columns([2, 1, 2])
                    with c1:
                        st.markdown(
                            f"{STATE_EMOJI.get(mp['weak_state'], '')} **{mp['weak_name']}** "
                            f"({mp['weak_state']})"
                        )
                    with c2:
                        st.markdown(f"→ {mp['pair_type']} →")
                        st.caption(f"置信度: {mp['confidence']:.1%}")
                    with c3:
                        st.markdown(
                            f"{STATE_EMOJI.get(mp['strong_state'], '')} **{mp['strong_name']}** "
                            f"({mp['strong_state']})"
                        )
                    st.divider()

    # ================================================================
    # 信号验证
    # ================================================================
    _render_signal_validation(state_df)


if __name__ == "__main__":
    render()

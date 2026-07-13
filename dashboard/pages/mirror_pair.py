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


@st.cache_resource
def get_stores():
    return ParquetStore(), SQLiteStore()


@st.cache_resource
def get_models():
    ps, ss = get_stores()
    sm = StateMachine(ps, ss)
    mirror = MirrorPair(ss, sm)
    return sm, mirror


@st.cache_data(ttl=3600)
def load_mirror_data():
    """加载镜像对数据（独立缓存）"""
    _, mirror = get_models()
    mirror_pairs = mirror.find_mirror_pairs()
    return mirror_pairs


@st.cache_data(ttl=3600)
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
        title="资金迁移路径（弱→强）",
        height=400 + len(node_labels) * 20,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        font={"size": 10},
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
            "置信度": st.column_config.ProgressColumn(
                format="%.1f%%",
                min_value=0,
                max_value=100,
            ),
        },
    )


def _render_signal_validation(state_df: pd.DataFrame):
    """渲染信号验证"""
    st.subheader("交叉验证信号")

    _, mirror = get_models()

    # 选择板块
    if state_df is not None and not state_df.empty:
        sector_options = {}
        for _, row in state_df.iterrows():
            # 只显示镜像对相关的状态
            if row["state"] in ["⑥弱转强", "④强转弱", "③加速冲顶", "⑦持续杀跌"]:
                emoji = STATE_EMOJI.get(row["state"], "")
                sector_options[f"{emoji} {row['sector_name']} ({row['sector_code']}) - {row['state']}"] = row

        if not sector_options:
            st.info("当前无处于极端状态的板块")
            return

        selected_label = st.selectbox(
            "选择要验证的板块",
            list(sector_options.keys()),
        )
        selected = sector_options[selected_label]

        with st.spinner("验证中..."):
            is_valid, mirror_code, confidence = mirror.validate_signal(
                selected["sector_code"],
                selected["state"],
            )

        if is_valid:
            if mirror_code:
                mirror_name = get_sector_name(mirror_code)
                st.success(
                    f"验证通过: {selected['sector_name']}({selected['state']}) "
                    f"↔ {mirror_name}({get_opposite_state(selected['state'])}) "
                    f"置信度: {confidence:.1%}"
                )
            else:
                st.success(f"验证通过: {selected['sector_name']}({selected['state']}) - 非极端状态，无需交叉验证")
        else:
            st.error(
                f"验证未通过: {selected['sector_name']}({selected['state']}) - "
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
    st.subheader("资金迁移路径")
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

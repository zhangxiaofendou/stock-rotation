"""
镜像对监控页面
==============
展示镜像对列表、资金迁移网络图。
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from model.state_machine import StateMachine
from model.mirror_pair import MirrorPair
from dashboard.components.state_card import STATE_EMOJI


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
            textfont={"size": 16, "color": "black", "family": "Arial, sans-serif"},
            node={
                "pad": 18,
                "thickness": 30,
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
            font={"size": 16, "color": "black", "family": "Arial, sans-serif"},
        ),
        height=500 + len(node_labels) * 25,
        margin={"l": 10, "r": 10, "t": 50, "b": 10},
        font={"size": 16, "color": "black", "family": "Arial, sans-serif"},
        paper_bgcolor="white",
    )

    st.plotly_chart(fig, use_container_width=True, key="mirror_sankey")


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


def render_group_capital_path(mirror_pairs: list):
    """
    板块组资金迁移路径：按板块组聚合镜像对，用 Sankey 展示各组内
    资金从弱势子板块(④/⑦)向强势子板块(⑥/③)迁移的强度。
    复用市场温度计的板块组(关联组)口径，红=资金流出、绿=资金流入。
    """
    if not mirror_pairs:
        st.info("暂无镜像对数据，无法生成板块组资金迁移路径")
        return

    # 按板块组聚合资金流强度（置信度合计，下限 0.1 保证连线可见）
    group_flow = {}
    for mp in mirror_pairs:
        g = mp["group"]
        group_flow[g] = group_flow.get(g, 0) + max(mp.get("confidence", 0.5), 0.1)

    groups = list(group_flow.keys())
    if not groups:
        st.info("当前无板块组资金流数据")
        return

    # 节点：左侧 = 板块组(流出/弱势)，右侧 = 板块组(流入/强势)
    node_labels = []
    node_colors = []
    node_map = {}
    for g in groups:
        out_label = f"{g}\n（流出）"
        in_label = f"{g}\n（流入）"
        node_map[("out", g)] = len(node_labels)
        node_labels.append(out_label)
        node_colors.append("#F44336")  # 红：资金流出
        node_map[("in", g)] = len(node_labels)
        node_labels.append(in_label)
        node_colors.append("#4CAF50")  # 绿：资金流入

    links_source, links_target, links_value = [], [], []
    for g in groups:
        links_source.append(node_map[("out", g)])
        links_target.append(node_map[("in", g)])
        links_value.append(group_flow[g])

    fig = go.Figure(
        go.Sankey(
            textfont={"size": 16, "color": "black", "family": "Arial, sans-serif"},
            node={
                "pad": 18,
                "thickness": 30,
                "line": {"color": "gray", "width": 0.5},
                "label": node_labels,
                "color": node_colors,
            },
            link={
                "source": links_source,
                "target": links_target,
                "value": links_value,
                "color": ["rgba(255,152,0,0.35)"] * len(links_source),
            },
        )
    )
    fig.update_layout(
        title=dict(
            text="板块组资金迁移路径（组内：弱→强）",
            font={"size": 16, "color": "black", "family": "Arial, sans-serif"},
        ),
        height=400 + len(groups) * 60,
        margin={"l": 10, "r": 10, "t": 50, "b": 10},
        font={"size": 16, "color": "black", "family": "Arial, sans-serif"},
        paper_bgcolor="white",
    )
    st.plotly_chart(fig, use_container_width=True, key="group_capital_sankey")


def render(show_header: bool = True):
    """渲染镜像对监控页面（show_header=False 时用于嵌入市场温度计页签）"""
    if show_header:
        st.title("镜像对监控")
        st.markdown("监控板块间资金迁移的镜像对关系")

    with st.spinner("加载镜像对数据..."):
        mirror_pairs = load_mirror_data()

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


if __name__ == "__main__":
    render()

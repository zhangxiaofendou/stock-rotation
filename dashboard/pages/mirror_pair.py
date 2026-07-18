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
from config.sector_map import SECTOR_GROUPS
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

    st.plotly_chart(fig, width="stretch", key="mirror_sankey")


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
            "关联组": mp["group"],
            "强板块": f"{STATE_EMOJI.get(mp['strong_state'], '')} {mp['strong_name']}",
            "强状态": mp["strong_state"],
            "配对类型": mp["pair_type"],
            "置信度": f"{mp['confidence']:.1%}",
        })

    df = pd.DataFrame(table_data)

    # 置信度排序
    df = df.sort_values("置信度", ascending=False)

    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "置信度": st.column_config.NumberColumn(
                format="%.1f%%",
            ),
        },
    )


# 板块状态 → 资金流向权重（正=资金流入/强势，负=资金流出/弱势，0=中性）
_STATE_FLOW_WEIGHT = {
    "①领涨减速": -0.6,
    "②稳健上行": +0.6,
    "③加速冲顶": +1.0,
    "④强转弱":   -1.0,
    "⑤中性震荡":  0.0,
    "⑥弱转强":   +1.0,
    "⑦持续杀跌": -1.0,
    "⑧下跌中继": -0.6,
    "⑨底背离":   +0.7,
}


def render_group_capital_path(state_df: pd.DataFrame):
    """
    板块组之间的资金迁移路径（跨组，含具体行业）：
    四层 Sankey —— 流出行业 → 净流出板块组 → 净流入板块组 → 流入行业。
    净资金流 = Σ 组内各板块状态的资金权重（见 _STATE_FLOW_WEIGHT）。
    每个板块组只展开资金流最强的 Top N 行业，其余行业合并为「其他N行业」节点
    （保证流量守恒、又不至于拥挤）。
    """
    TOP_N = 6  # 每组最多展开的领先行业数

    if state_df is None or state_df.empty:
        st.info("暂无板块状态数据，无法生成板块组资金迁移路径")
        return

    # 1) 计算每个板块组、每个行业的净资金流
    group_net = {}
    group_industries = {}  # group -> [{"name","state","net"}, ...]
    for gname, ginfo in SECTOR_GROUPS.items():
        codes = set(ginfo["level2_codes"])
        gdf = state_df[state_df["sector_code"].isin(codes)]
        if len(gdf) == 0:
            continue
        net = gdf["state"].map(_STATE_FLOW_WEIGHT).fillna(0.0).sum()
        group_net[gname] = net
        for _, row in gdf.iterrows():
            w = _STATE_FLOW_WEIGHT.get(row["state"], 0.0)
            group_industries.setdefault(gname, []).append({
                "name": row.get("sector_name", row["sector_code"]),
                "state": row["state"],
                "net": w,
            })

    if not group_net:
        st.info("当前无板块组数据")
        return

    # 2) 拆为净流出组（弱）与净流入组（强）
    sources = {g: -v for g, v in group_net.items() if v < 0}  # 流出量(正)
    sinks = {g: v for g, v in group_net.items() if v > 0}       # 流入量(正)

    if not sources or not sinks:
        st.info("当前没有同时出现『净流出板块组』与『净流入板块组』，无法绘制组间迁移路径")
        return

    total_out = sum(sources.values())  # = total_in

    # 3) 构建四层 Sankey 节点
    node_labels, node_colors, node_map = [], [], {}
    def add_node(label, color):
        if label not in node_map:
            node_map[label] = len(node_labels)
            node_labels.append(label)
            node_colors.append(color)
        return node_map[label]

    out_groups = list(sources.keys())
    in_groups = list(sinks.keys())

    # 流出行业（浅红）/ 流出组其余行业（浅粉）
    for g in out_groups:
        inds = sorted([i for i in group_industries.get(g, []) if i["net"] < 0], key=lambda x: x["net"])
        for i in inds[:TOP_N]:
            add_node(f"{i['name']}\n({i['state']})", "#EF9A9A")
        if len(inds) > TOP_N:
            add_node(f"{g}·其他{len(inds) - TOP_N}行业\n（净流出）", "#F8BBD0")
    # 净流出板块组（深红）
    for g in out_groups:
        add_node(f"{g}\n（净流出）", "#F44336")
    # 净流入板块组（深绿）
    for g in in_groups:
        add_node(f"{g}\n（净流入）", "#4CAF50")
    # 流入行业（浅绿）/ 流入组其余行业（浅绿灰）
    for g in in_groups:
        inds = sorted([i for i in group_industries.get(g, []) if i["net"] > 0], key=lambda x: -x["net"])
        for i in inds[:TOP_N]:
            add_node(f"{i['name']}\n({i['state']})", "#A5D6A7")
        if len(inds) > TOP_N:
            add_node(f"{g}·其他{len(inds) - TOP_N}行业\n（净流入）", "#C8E6C9")

    # 4) 构建连线
    links_source, links_target, links_value, links_color = [], [], [], []
    # 流出行业 → 净流出板块组
    for g in out_groups:
        inds = sorted([i for i in group_industries.get(g, []) if i["net"] < 0], key=lambda x: x["net"])
        for i in inds[:TOP_N]:
            links_source.append(node_map[f"{i['name']}\n({i['state']})"])
            links_target.append(node_map[f"{g}\n（净流出）"])
            links_value.append(-i["net"])
            links_color.append("rgba(244,67,54,0.35)")
        if len(inds) > TOP_N:
            rv = sum(-i["net"] for i in inds[TOP_N:])
            links_source.append(node_map[f"{g}·其他{len(inds) - TOP_N}行业\n（净流出）"])
            links_target.append(node_map[f"{g}\n（净流出）"])
            links_value.append(rv)
            links_color.append("rgba(244,67,54,0.25)")
    # 净流出板块组 → 净流入板块组（按流入占比分配，保证守恒）
    for g_out, out_v in sources.items():
        for g_in, in_v in sinks.items():
            w = out_v * (in_v / total_out)
            if w <= 0:
                continue
            links_source.append(node_map[f"{g_out}\n（净流出）"])
            links_target.append(node_map[f"{g_in}\n（净流入）"])
            links_value.append(w)
            links_color.append("rgba(255,152,0,0.3)")
    # 净流入板块组 → 流入行业
    for g in in_groups:
        inds = sorted([i for i in group_industries.get(g, []) if i["net"] > 0], key=lambda x: -x["net"])
        for i in inds[:TOP_N]:
            links_source.append(node_map[f"{g}\n（净流入）"])
            links_target.append(node_map[f"{i['name']}\n({i['state']})"])
            links_value.append(i["net"])
            links_color.append("rgba(76,175,80,0.35)")
        if len(inds) > TOP_N:
            rv = sum(i["net"] for i in inds[TOP_N:])
            links_source.append(node_map[f"{g}\n（净流入）"])
            links_target.append(node_map[f"{g}·其他{len(inds) - TOP_N}行业\n（净流入）"])
            links_value.append(rv)
            links_color.append("rgba(76,175,80,0.25)")

    # 5) 绘制
    n_ind_nodes = sum(
        (min(len([i for i in group_industries.get(g, []) if i["net"] < 0]), TOP_N)
         + (1 if len([i for i in group_industries.get(g, []) if i["net"] < 0]) > TOP_N else 0))
        for g in out_groups
    ) + sum(
        (min(len([i for i in group_industries.get(g, []) if i["net"] > 0]), TOP_N)
         + (1 if len([i for i in group_industries.get(g, []) if i["net"] > 0]) > TOP_N else 0))
        for g in in_groups
    )

    fig = go.Figure(
        go.Sankey(
            textfont={"size": 14, "color": "black", "family": "Arial, sans-serif"},
            node={
                "pad": 18,
                "thickness": 24,
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
            text="板块组之间资金迁移路径（行业 → 组 → 组 → 行业）",
            font={"size": 16, "color": "black", "family": "Arial, sans-serif"},
        ),
        height=max(420, n_ind_nodes * 16 + 160),
        margin={"l": 10, "r": 10, "t": 50, "b": 10},
        font={"size": 14, "color": "black", "family": "Arial, sans-serif"},
        paper_bgcolor="white",
    )
    st.plotly_chart(fig, width="stretch", key="group_capital_sankey")

    st.caption(
        "四层结构：左/浅红=流出行业，左中/深红=净流出板块组，右中/深绿=净流入板块组，右/浅绿=流入行业；"
        "「其他N行业」= 该板块组内资金流其余行业的合计。连线粗细代表迁移强度。"
    )


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

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


@st.cache_data(ttl=86400)
def load_state_for_mirror():
    """加载板块状态（用于板块组信号平衡统计，独立缓存）"""
    sm, _ = get_models()
    return sm.calc_all_sectors_state()


def _calc_group_signal_stats(state_df: pd.DataFrame) -> dict:
    """
    按板块组(关联组)计算强弱信号平衡，复用市场温度计的口径：
    - 买入信号：⑥弱转强、⑨底背离
    - 卖出信号：①领涨减速、④强转弱、⑦持续杀跌
    返回: {group_name: {"total", "buy_count", "sell_count"}}
    """
    result = {}
    if state_df is None or state_df.empty:
        return result
    for group_name, group_info in SECTOR_GROUPS.items():
        group_codes = set(group_info["level2_codes"])
        group_df = state_df[state_df["sector_code"].isin(group_codes)]
        if len(group_df) > 0:
            buy_states = group_df[group_df["state"].isin(["⑥弱转强", "⑨底背离"])]
            sell_states = group_df[group_df["state"].isin(["①领涨减速", "④强转弱", "⑦持续杀跌"])]
            result[group_name] = {
                "total": len(group_df),
                "buy_count": len(buy_states),
                "sell_count": len(sell_states),
            }
    return result


def _render_group_monitor(mirror_pairs: list):
    """
    板块组镜像对监控：把市场温度计的「板块组强弱」概念引入镜像对模块。
    先给每个板块组一张强弱信号平衡表，再按板块组折叠展开各自的镜像对。
    """
    st.subheader("板块组镜像对监控")
    st.caption("按板块组(关联组)维度监控资金镜像关系：组内强弱信号此消彼长，红为流出、绿为流入")

    state_df = load_state_for_mirror()
    group_stats = _calc_group_signal_stats(state_df)

    # 组内镜像对归集
    group_pairs = {}
    for mp in mirror_pairs:
        group_pairs.setdefault(mp["group"], []).append(mp)

    if not group_stats:
        st.warning("暂无板块组信号数据")
        return

    # 概览表：每个板块组的强弱信号平衡 + 组内镜像对数
    table_rows = []
    for g, s in group_stats.items():
        n_pairs = len(group_pairs.get(g, []))
        table_rows.append({
            "板块组": g,
            "板块数": s["total"],
            "买入信号": s["buy_count"],
            "卖出信号": s["sell_count"],
            "买入占比": f"{s['buy_count'] / max(s['total'], 1):.1%}",
            "卖出占比": f"{s['sell_count'] / max(s['total'], 1):.1%}",
            "组内镜像对": n_pairs,
        })
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

    # 折叠展开：每个板块组内的镜像对
    st.markdown("---")
    for g in sorted(group_stats.keys(), key=lambda x: -len(group_pairs.get(x, []))):
        s = group_stats[g]
        pairs = group_pairs.get(g, [])
        with st.expander(
            f"{g} · 板块{s['total']}个 · 买{s['buy_count']}/卖{s['sell_count']} · 镜像对{len(pairs)}对",
            expanded=False,
        ):
            if pairs:
                for mp in pairs:
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
            else:
                st.caption("该板块组当前无镜像对（组内未同时出现镜像状态组合）")


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

    # ================================================================
    # 板块组镜像对监控
    # ================================================================
    _render_group_monitor(mirror_pairs)


if __name__ == "__main__":
    render()

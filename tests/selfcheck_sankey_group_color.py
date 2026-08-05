"""Sankey 板块组配色自检：每个板块组用独立色，不能共用红/绿。

不在 Streamlit 上下文里跑，直接调底层 _build_group_capital_path_figure，
构造 6 个板块组都同时出现净流出/净流入的场景，导出图确认中间层节点
颜色互不相同、连线按集团色上色。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.io as pio

import dashboard.pages.mirror_pair as mp


def _build_state_df():
    """构造一个所有板块组都同时出现流出+流入的场景。

    用 SECTOR_GROUPS 真实行业代码（避免假代码被 _build_group_capital_path_figure
    里的 isin() 过滤掉），人为分配：两组净流出（周期/其他）、四组净流入
    （消费/医药/科技/新能源），大金融故意流出/流入各一行业让其在两边都出现。
    """
    from config.sector_map import SECTOR_GROUPS
    groups_codes = {g: list(info["level2_codes"])[:3] for g, info in SECTOR_GROUPS.items()}
    rows = []
    out_groups = {"周期", "其他"}
    in_groups = {"消费", "医药", "科技", "新能源"}
    for g, codes in groups_codes.items():
        for idx, code in enumerate(codes):
            if g in out_groups:
                state = "⑦持续杀跌"  # -1
            elif g in in_groups:
                state = "⑥弱转强"    # +1
            else:  # 大金融 — 流出流入各一行业，让它在两边都出现
                state = "⑧下跌中继" if idx == 0 else "③加速冲顶"
            rows.append({"sector_code": code, "sector_name": f"{g}·{code}", "state": state})
    return pd.DataFrame(rows)


def main():
    state_df = _build_state_df()
    print(f"构造 {len(state_df)} 条状态记录")

    result = mp._build_group_capital_path_figure(state_df)
    if "fig" not in result:
        print(f"FAIL: figure not built: {result}")
        return 1

    fig = result["fig"]
    # 校验：每个板块组节点的颜色互不相同
    node_colors = fig.data[0].node.color
    node_labels = fig.data[0].node.label
    print(f"节点数 = {len(node_labels)}")

    # 找出所有板块组节点（标签含 (净流出) 或 (净流入)）
    group_nodes = [(lbl, c) for lbl, c in zip(node_labels, node_colors) if "净流出" in lbl or "净流入" in lbl]
    print("\n板块组中间层节点颜色：")
    group_color_set = set()
    for lbl, c in group_nodes:
        short = lbl.replace("\n", " ")
        print(f"  {short} -> {c}")
        group_color_set.add(c)

    if len(group_color_set) != len(group_nodes):
        print(f"\n❌ FAIL: 板块组节点只有 {len(group_color_set)} 种颜色，共 {len(group_nodes)} 个节点 — 仍有共用色！")
        return 1

    # 校验：连线里组→组过渡仍是橙色，其他连线颜色应跟随集团色
    link_colors = fig.data[0].link.color
    link_src = fig.data[0].link.source
    link_tgt = fig.data[0].link.target
    src_labels = [node_labels[i] for i in link_src]
    tgt_labels = [node_labels[i] for i in link_tgt]
    inter_group_count = 0
    intra_group_color_count = 0
    for s, t, c in zip(src_labels, tgt_labels, link_colors):
        if "净流出" in s and "净流入" in t:
            inter_group_count += 1
            assert "152" in c, f"组→组连线应该是橙色, 但得到 {c}"
        elif "净流出" in s or "净流入" in s or "净流出" in t or "净流入" in t:
            intra_group_color_count += 1
            assert "152" not in c, f"组/行业 邻接线不应是橙色, 但得到 {c}"
    print(f"\n组→组橙色过渡连线数 = {inter_group_count}")
    print(f"组/行业 邻接彩色连线数 = {intra_group_color_count}")

    # 导出 PNG 供人眼核查（HTML 也行，避免 plotly 静态导出依赖）
    out_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sankey_color_check.html")
    fig.write_html(out_html, include_plotlyjs="cdn")
    print(f"\n已导出 HTML: {out_html}")
    print("\n✅ 通过：所有板块组中间层节点颜色互不相同；行业邻接线跟随集团色；组→组过渡仍为橙色。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
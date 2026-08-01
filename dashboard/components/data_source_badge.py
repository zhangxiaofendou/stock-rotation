"""数据来源标注组件（看板统一口径）。

把界面上每个数据块的来源用统一的小徽标标注出来，并在侧栏提供图例，
让用户一眼看清某组数据来自 同花顺 / 东方财富 / 腾讯 / 本地计算。

来源口径（与 data/sources 实现一致）：
  - ths     同花顺(10jqka)：行业板块 K 线/涨跌/轮动原数据、个股 K 线（主源）
  - em      东方财富(EastMoney)：指数/个股 K 线主源（腾讯兜底）、基准指数、
            真实行业资金流（经 AkShare 取东财）、北向、融资融券、涨停、ETF、交易日历
  - tencent 腾讯(gtimg)：盘中实时行情条（指数/ETF 实时快照）
  - derived 本地计算/AI 合成：RS、趋势、九宫格状态机、分化度、信号绩效、
            综合评分、回测、持仓、研报共识样本
"""

from typing import Iterable

import streamlit as st

# 来源元数据：标签 + 颜色 + 说明（hover 提示）
SOURCE_META = {
    "ths": {
        "short": "THS",
        "label": "同花顺",
        "color": "#1677ff",
        "desc": "同花顺(10jqka)：行业板块 K 线/涨跌/轮动原数据、个股 K 线（主数据源）",
    },
    "em": {
        "short": "EM",
        "label": "东方财富",
        "color": "#722ed1",
        "desc": "东方财富(EastMoney)：指数/个股 K 线主源（腾讯兜底）、基准指数、真实行业资金流（经 AkShare 取东财）、北向/融资融券/涨停/ETF/交易日历",
    },
    "tencent": {
        "short": "TX",
        "label": "腾讯",
        "color": "#13c2c2",
        "desc": "腾讯(gtimg)：盘中实时行情条（指数/ETF 实时快照）",
    },
    "derived": {
        "short": "本地",
        "label": "本地计算",
        "color": "#8c8c8c",
        "desc": "本地计算 / AI 合成：RS、价格趋势、九宫格状态机、分化度、信号绩效、综合评分、策略回测、持仓、研报共识样本",
    },
}


def _hex_to_rgba(hex_color: str, alpha: float = 0.12) -> str:
    """把 #RRGGBB 转成 rgba(...) 用作浅色底。"""
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},{alpha})"
    return "rgba(0,0,0,0.06)"


def src_badge_html(*keys: str, prefix: str = "🏷 数据来源") -> str:
    """生成一组来源徽标的 HTML 字符串（供 unsafe_allow_html 渲染）。

    keys 可为 "ths" / "em" / "tencent" / "derived" 的任意组合。
    """
    valid = [k for k in keys if k in SOURCE_META]
    if not valid:
        return ""
    chips = []
    for k in valid:
        m = SOURCE_META[k]
        bg = _hex_to_rgba(m["color"], 0.12)
        chips.append(
            f'<span title="{m["desc"]}" style="display:inline-block;margin:1px 5px 1px 0;'
            f'padding:1px 8px;border-radius:9px;font-size:11px;font-weight:600;'
            f'background:{bg};color:{m["color"]};border:1px solid {m["color"]};'
            f'white-space:nowrap;">{m["short"]}·{m["label"]}</span>'
        )
    joined = "".join(chips)
    return (
        f'<div style="margin:2px 0 6px;line-height:1.6;">'
        f'<span style="font-size:11px;color:#888;margin-right:4px;">{prefix}：</span>'
        f'{joined}</div>'
    )


def render_src_badge(*keys: str, prefix: str = "🏷 数据来源"):
    """直接在 Streamlit 渲染来源徽标（在 section header 之后调用）。"""
    html = src_badge_html(*keys, prefix=prefix)
    if html:
        st.markdown(html, unsafe_allow_html=True)


def render_legend():
    """侧栏「数据来源图例」：列出全部来源及含义，供界面徽标解码。"""
    with st.sidebar.expander("🏷️ 数据来源图例", expanded=False):
        for k, m in SOURCE_META.items():
            bg = _hex_to_rgba(m["color"], 0.12)
            st.markdown(
                f'<span style="display:inline-block;margin:1px 5px 1px 0;padding:1px 8px;'
                f'border-radius:9px;font-size:11px;font-weight:600;background:{bg};'
                f'color:{m["color"]};border:1px solid {m["color"]};'
                f'white-space:nowrap;">{m["short"]}·{m["label"]}</span>',
                unsafe_allow_html=True,
            )
            st.caption(m["desc"])
        st.markdown("---")
        st.caption("界面中每个数据块标题下方标注其来源徽标；悬停徽标可看详细说明。")


# 便捷别名，便于页面调用
SRC = {
    "ths": "ths",
    "em": "em",
    "tencent": "tencent",
    "derived": "derived",
}

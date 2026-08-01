"""
导航状态持久化工具
==================
用 URL query 参数实现「刷新保留、关页重置」的受控 tab / 页面选择器。

设计要点：
  - 浏览器刷新（F5）：URL 不变，query 参数仍在 → 恢复到上次选中。
  - 关闭网页重新打开：新开的是干净 URL（无参数）→ 回到默认首项。
  - 不依赖 st.session_state（F5 会产生新会话、session_state 会丢失），
    因此以 query 参数为唯一真相来源。
"""

import streamlit as st


def persistent_tabs(key: str, tabs: list):
    """受控 tab 选择器（外观为水平单选，等价于可记忆的标签页）。

    参数:
        key:    query 参数名（同时作为 session 缓存键），须各页面唯一
        tabs:   tab 标签列表（字符串）

    返回:
        当前选中的 tab 标签
    """
    radio_key = f"{key}__radio"

    # 1) 从 URL query 参数读上次选中（F5 后仍在；新开为空 → 首个）
    qp = st.query_params.get(key, [None])[0]
    if qp not in tabs:
        qp = tabs[0]

    # 2) 会话内缓存（同一次会话保持稳定，避免重复查 query）
    if key not in st.session_state:
        st.session_state[key] = qp

    # 3) 渲染分段控件（外观即标签页，刷新保留）
    choice = st.segmented_control(
        "页签",
        tabs,
        default=st.session_state[key],
        key=radio_key,
        label_visibility="collapsed",
    )

    # 4) 选中变化则写回 URL（触发一次 rerun，之后 choice == 缓存值即不再写）
    if choice != st.session_state[key]:
        st.session_state[key] = choice
        st.query_params[key] = choice

    # 5) 防御：部分 Streamlit 版本在首渲时 segmented_control 可能返回 None，
    #    此时回退到已确定的会话默认值，避免调用方 if/elif 全不命中导致内容空白。
    if choice is None:
        choice = st.session_state.get(key) or tabs[0]

    return choice


def persistent_radio(key: str, options: list, label: str = "导航菜单"):
    """受控单选（用于顶层页面导航），同样具备刷新保留能力。

    参数:
        key:      query 参数名
        options:  选项列表
        label:    侧边栏显示文案

    返回:
        当前选中的选项
    """
    qp = st.query_params.get(key, [None])[0]
    if qp not in options:
        qp = options[0]

    if key not in st.session_state:
        st.session_state[key] = qp

    choice = st.radio(
        label,
        options,
        index=options.index(st.session_state[key]),
        key=f"{key}__radio",
    )

    if choice != st.session_state[key]:
        st.session_state[key] = choice
        st.query_params[key] = choice

    return choice

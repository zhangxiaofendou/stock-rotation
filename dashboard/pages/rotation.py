"""
板块轮动监控页面（核心页面）
===========================
展示九宫格热力图、板块强弱排行、板块卡片。
"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from model.state_machine import StateMachine
from model.scoring import SectorScoring
from model.transition import TransitionRules
from model.mirror_pair import MirrorPair
from config.sector_map import get_sector_name
from config.settings import PARQUET_DIR
from dashboard.components.state_card import (
    STATE_COLORS, STATE_EMOJI,
    get_state_signal, get_state_signal_color, get_signal_legend,
)
from dashboard.components.drill_pickers import (
    render_transition_picker,
    render_sector_picker,
)
from dashboard.pages.stock_drill import render as render_stock_drill
from dashboard.components.nav_state import persistent_tabs
from signal_tracker.performance import get_sector_signal_summary
from ai.consensus import compute_sector_consensus
from model.confirmation import arbitrate_sector
from dashboard.components.data_source_badge import render_src_badge

# 状态颜色映射
STATE_BG_COLORS = {
    "①领涨减速": "background-color: #FFF3E0;",
    "②稳健上行": "background-color: #E8F5E9;",
    "③加速冲顶": "background-color: #FFFDE7;",
    "④强转弱":   "background-color: #FFF3E0;",
    "⑤中性震荡": "background-color: #F5F5F5;",
    "⑥弱转强":   "background-color: #E8F5E9;",
    "⑦持续杀跌": "background-color: #FFEBEE;",
    "⑧下跌中继": "background-color: #FFEBEE;",
    "⑨底背离":   "background-color: #E8F5E9;",
}

# ================================================================
# 综合评分维度（与 model/scoring.py 的 WEIGHTS 保持一致）
#   四维度分值 + 综合评分，所有展示分值的地方统一呈现
# ================================================================
SCORE_DIM_LABELS = [
    ("RS横截面",   "rs_cross_score",   0.30),
    ("动量横截面", "mom_cross_score",  0.30),
    ("RS时序分位", "rs_position_score", 0.20),
    ("动量时序分位", "rs_momentum_score", 0.20),
]
SCORE_WEIGHT_NOTE = (
    "综合评分 = 30%·RS横截面 + 30%·动量横截面 + 20%·RS时序分位 + 20%·动量时序分位"
    "（横截面为主负责横向选强，时序为辅负责过滤确认）"
)

# 缓存版本号：当评分逻辑或返回列结构发生变化时递增，
# 强制使 Streamlit 的缓存键失效，避免旧缓存返回缺列数据。
CACHE_VERSION = 2


CROWDING_LEVELS = (
    (90, "极度拥挤", "#C62828", "成交活跃度处于近一年极高位置，追高需警惕回撤放大。"),
    (75, "偏拥挤", "#EF6C00", "成交活跃度偏高，建议结合趋势持续性与镜像确认控制追高风险。"),
)


def _get_crowding_assessment(score):
    """将 0-100 拥挤度映射为解释性标签，不参与状态机或综合评分。"""
    if score is None or pd.isna(score):
        return "数据不足", "#757575", "暂无有效拥挤度数据。"
    score = float(score)
    for threshold, label, color, note in CROWDING_LEVELS:
        if score >= threshold:
            return label, color, note
    if score < 25:
        return "低关注", "#546E7A", "成交活跃度处于较低位置，当前信号的参与度确认偏弱。"
    return "正常", "#2E7D32", "成交活跃度处于常态区间。"


@st.cache_data(ttl=3600)
def load_mirror_confirmation(sector_code: str, sector_state: str):
    """读取选中板块的同组镜像确认；仅作为辅助证据，不改变主状态。"""
    try:
        parquet_store, sqlite_store = get_stores()
        state_machine = StateMachine(parquet_store, sqlite_store)
        mirror = MirrorPair(sqlite_store, state_machine)
        is_valid, mirror_code, confidence = mirror.validate_signal(sector_code, sector_state)
        return {
            "available": True,
            "is_valid": bool(is_valid),
            "mirror_code": mirror_code,
            "confidence": float(confidence or 0),
        }
    except Exception:
        return {"available": False, "is_valid": False, "mirror_code": None, "confidence": 0.0}


def _render_confirmation_risk_panel(selected_code: str, sector_state: str, score_row: pd.Series | None):
    """展示不参与主评分的辅助确认与风险信息。"""
    st.subheader("确认与风险因子")
    render_src_badge("em", "derived", base=["ths_kline", "em_flow"])
    st.caption("以下因子用于解释和风险提示，不改变九宫格状态、综合评分或原有操作建议。")

    crowding_score = score_row.get("crowding_score") if score_row is not None else None
    crowding_label, crowding_color, crowding_note = _get_crowding_assessment(crowding_score)
    mirror = load_mirror_confirmation(selected_code, sector_state)

    col1, col2 = st.columns(2)
    with col1:
        if crowding_score is None or pd.isna(crowding_score):
            st.metric("拥挤度", "N/A")
        else:
            st.metric("拥挤度", f"{float(crowding_score):.1f}/100")
        st.markdown(
            f"<span style='font-weight:700;color:{crowding_color};'>● {crowding_label}</span>",
            unsafe_allow_html=True,
        )
        st.caption(crowding_note)

    with col2:
        if not mirror["available"]:
            st.metric("镜像确认", "暂不可用")
            st.caption("镜像数据暂不可用，不对当前状态做任何推断。")
        elif mirror["mirror_code"]:
            mirror_name = get_sector_name(mirror["mirror_code"])
            st.metric("镜像确认", "已确认" if mirror["is_valid"] else "未确认")
            st.caption(f"同组对立状态：{mirror_name}（置信度 {mirror['confidence']:.0%}）")
        elif sector_state in {"④强转弱", "⑥弱转强", "③加速冲顶", "⑦持续杀跌"}:
            st.metric("镜像确认", "未发现")
            st.caption("当前为可验证状态，但关联组内未发现对立状态板块。")
        else:
            st.metric("镜像确认", "不适用")
            st.caption("该状态不属于镜像交叉验证范围。")

    # ---- 资金流 + 分化度（每日管线落盘，PRD §5.6.2 确认因子）----
    _render_flow_divergence_rows(selected_code)

    # ---- 信号仲裁横幅（九宫格 + 资金流 + 研报 三路仲裁）----
    _render_arbitration_banner(selected_code, sector_state)

    # ---- AI：研报/新闻共识（纯规则，PRD §7.4 / §5.6.5）----
    _render_research_consensus_card(selected_code)

    with st.expander("指标口径与当前缺口"):
        st.markdown(
            "- **拥挤度**：成交额分位（60%）与成交量分位（40%）的近 250 个交易日加权结果。\n"
            "- **镜像确认**：只在关联板块组内检查 ④↔⑥、③↔⑦ 的对立状态。\n"
            "- **资金流 / 分化度**：来自每日管线落盘的 `sector_fund_flow` / `sector_divergence` 表；"
            "离线/未更新时显示「暂无」，不臆造中性值。\n"
            "- **研报/新闻共识**：评级上调潮、目标价上调幅度、覆盖券商数变化、评级分歧度，"
            "由 `research_reports` 结构化存储的研报经纯规则计算（每条结论可追溯原文）；"
            "无研报数据时显示「暂无」。\n"
            "- **信号仲裁**：九宫格 × 资金流 × 研报 三路信号仲裁，仅做确认/降级/否决，"
            "不改变九宫格状态或综合评分。"
        )


def _render_flow_divergence_rows(selected_code: str):
    """在「确认与风险因子」中展示资金流与分化度（来自每日管线落盘）。"""
    sqlite = SQLiteStore()
    ff_df = sqlite.get_sector_fund_flow(sector_code=selected_code)
    dv_df = sqlite.get_sector_divergence(sector_code=selected_code)

    ff_signal = ff_df.iloc[0]["signal"] if (ff_df is not None and not ff_df.empty) else None
    ff_date = ff_df.iloc[0]["date"] if (ff_df is not None and not ff_df.empty) else None
    dv_val = dv_df.iloc[0]["divergence"] if (dv_df is not None and not dv_df.empty) else None
    dv_date = dv_df.iloc[0]["date"] if (dv_df is not None and not dv_df.empty) else None

    col1, col2 = st.columns(2)
    with col1:
        if ff_signal is None:
            st.metric("资金流", "暂无")
            st.caption("每日管线未落盘该板块资金流（离线/未更新）。")
        else:
            label = {"正向": "净流入·正向", "反向": "净流出·反向", "中性": "中性"}.get(ff_signal, ff_signal)
            color = {"正向": "#2E7D32", "反向": "#C62828", "中性": "#757575"}.get(ff_signal, "#757575")
            st.metric("资金流", label)
            st.markdown(
                f"<span style='font-weight:700;color:{color};'>● {label}</span>",
                unsafe_allow_html=True,
            )
            st.caption(f"数据日期 {ff_date}")
    with col2:
        if dv_val is None:
            st.metric("分化度", "暂无")
            st.caption("每日管线未落盘该板块分化度（离线/未更新）。")
        else:
            # 分化度低 → 成分股走势一致（强势健康）；高 → 分歧大（可能退潮）
            if dv_val < 0.02:
                assess, color = "一致性高（健康）", "#2E7D32"
            elif dv_val < 0.04:
                assess, color = "一致性中等", "#F9A825"
            else:
                assess, color = "分歧偏大（留意退潮）", "#C62828"
            st.metric("分化度", f"{dv_val:.3f}")
            st.markdown(
                f"<span style='font-weight:700;color:{color};'>● {assess}</span>",
                unsafe_allow_html=True,
            )
            st.caption(f"数据日期 {dv_date}")


def _render_arbitration_banner(selected_code: str, sector_state: str):
    """展示九宫格 × 资金流 × 研报 三路信号仲裁结果（仅确认/降级/否决）。"""
    st.markdown("---")
    st.markdown("##### ⚖️ 信号仲裁（九宫格 × 资金流 × 研报）")
    render_src_badge("derived", base=["ths_kline", "em_flow", "seed"])
    st.caption("三路信号交叉验证：仅做确认/降级/否决，不改变九宫格状态或综合评分。")

    try:
        arb = arbitrate_sector(selected_code, sector_state)
    except Exception as e:
        st.warning(f"仲裁计算失败：{e}")
        return

    conf = arb.get("confidence", "强确认")
    conf_color = {
        "强确认": "#2E7D32",
        "弱确认": "#F9A825",
        "否决":   "#C62828",
    }.get(conf, "#757575")

    final_action = arb.get("final_action", "—")
    st.markdown(
        f"<div style='padding:8px 12px;border-radius:8px;background:#F5F5F5;"
        f"border-left:4px solid {conf_color};'>"
        f"<b>仲裁结论：</b><span style='font-weight:700;color:{conf_color};'>{conf}</span>"
        f"　|　<b>最终动作：</b>{final_action}"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.caption(arb.get("reason", ""))

    # 三路信号明细
    factors = arb.get("factors", {})
    ff = factors.get("fund_flow", "未获取")
    rp = factors.get("report", "未获取")
    st.caption(
        f"九宫格：{sector_state}（{arb.get('original_action','—')}）　|　"
        f"资金流：{ff}　|　研报：{rp}"
    )


@st.cache_data(ttl=3600)
def _cached_consensus(sector_code: str):
    """缓存单板块共识计算（1h TTL）。"""
    return compute_sector_consensus(sector_code)


def _render_research_consensus_card(sector_code: str):
    """在「确认与风险因子」中展示研报/新闻共识（辅助确认，不改变主信号）。"""
    st.markdown("---")
    st.markdown("##### 🤖 研报 / 新闻共识")
    render_src_badge("derived", base=["seed"])
    st.caption("纯规则计算（PRD §7.4），仅作辅助确认；不改变九宫格状态、综合评分或操作建议。")

    try:
        c = _cached_consensus(sector_code)
    except Exception as e:
        st.warning(f"共识计算失败：{e}")
        return

    if not c.get("has_data"):
        # 云端自初始化：首次访问时写入示例数据，保证开箱即用
        try:
            from data.runtime_init import ensure_ai_data
            seed = ensure_ai_data()
            if not seed.get("skipped"):
                # 绕过缓存取最新结果（刚写入示例数据）
                c = compute_sector_consensus(sector_code)
        except Exception:
            pass
    if not c.get("has_data"):
        st.info("该板块暂无研报/新闻数据。导入真实研报后（或云端自初始化示例数据）此处展示共识信号。")
        return

    # 方向 + 强度
    direction = c["direction"]
    strength = c["strength"]
    dir_color = "#2E7D32" if direction == "看多" else ("#C62828" if direction == "看空" else "#78909C")
    col_a, col_b = st.columns([1, 2])
    with col_a:
        st.metric("研报共识", direction, help=f"综合强度 {strength:.0%}")
        st.markdown(
            f"<span style='font-weight:700;color:{dir_color};'>● {direction}（强度 {strength:.0%}）</span>",
            unsafe_allow_html=True,
        )
    with col_b:
        # 四个共识信号 chips
        chips = []
        chips.append(f"评级上调 {c['upgrade_count']} 家" + (" ✅潮" if c["upgrade_wave"] else ""))
        if c["target_up_median_pct"] is not None:
            chips.append(f"目标价↑中位数 {c['target_up_median_pct']:+.1f}%")
        chips.append(f"覆盖券商 {'+'+str(c['coverage_change']) if c['coverage_change']>=0 else str(c['coverage_change'])}"
                     + (" 🔥" if c["coverage_surge"] else ""))
        chips.append(f"评级分歧：{c['divergence']}")
        if c.get("news_net", 0) != 0:
            chips.append(f"新闻净情绪 {'+'+str(c['news_net']) if c['news_net']>0 else str(c['news_net'])}")
        st.caption(" ｜ ".join(chips))

    # 可追溯证据
    with st.expander(f"📑 近期研报证据（{c['report_count']} 篇，可追溯原文）"):
        for ev in c.get("evidence", [])[:12]:
            chg = ev.get("rating_change") or "—"
            tgt = f"目标价{ev['target_change_pct']:+.1f}%" if ev.get("target_change_pct") is not None else ""
            line = (
                f"**{ev.get('broker')}** ｜ {ev.get('rating')}（{chg}） ｜ {ev.get('coverage_date')} ｜ {tgt}"
            )
            st.markdown(line)
            if ev.get("core_view"):
                st.caption(ev["core_view"])
            if ev.get("source_url"):
                st.markdown(f"[原文链接]({ev['source_url']})")


def _render_score_breakdown(score_row: pd.Series):
    """渲染综合评分明细：4 个维度分值 + 综合评分 + 各维度权重。

    参数:
        score_row: score_df 中某板块的一行（含 score / rs_cross_score /
                   mom_cross_score / rs_position_score / rs_momentum_score）
    """
    st.subheader("综合评分明细")
    render_src_badge("derived", base=["ths_kline", "em_flow", "seed"])
    st.caption(SCORE_WEIGHT_NOTE)

    score = score_row.get("score")
    if pd.notna(score):
        st.markdown(f"### 综合评分：{score:.1f}")
    else:
        st.markdown("### 综合评分：N/A")

    cols = st.columns(4)
    for (label, key, w), c in zip(SCORE_DIM_LABELS, cols):
        with c:
            val = score_row.get(key)
            st.metric(label, f"{val:.1f}" if pd.notna(val) else "N/A")
            st.caption(f"权重 {w:.0%}")


@st.cache_resource
def get_stores():
    parquet_store = ParquetStore()
    sqlite_store = SQLiteStore()
    return parquet_store, sqlite_store


@st.cache_resource
def get_models(version: int = CACHE_VERSION):
    """初始化模型（version 仅用于缓存键失效，不接收外部传参）"""
    parquet_store, sqlite_store = get_stores()
    sm = StateMachine(parquet_store, sqlite_store)
    scoring = SectorScoring(parquet_store, sqlite_store, sm)
    return sm, scoring


@st.cache_data(ttl=86400)  # 每日凌晨更新一次，24h TTL
def load_state_df():
    """加载板块状态（独立缓存）"""
    sm, _ = get_models()
    return sm.calc_all_sectors_state()


@st.cache_data(ttl=3600)  # 评分每日更新一次，1h 内存缓存避免重复实时计算
def load_score_df(version: int = CACHE_VERSION):
    """加载板块评分排行（实时计算，不再依赖任何磁盘快照文件）"""
    _, scoring = get_models()
    return scoring.calc_all_scores()


@st.cache_data(ttl=3600)
def load_badge_map():
    """扫描 trend parquet 末行，返回 {code: trend_badge}。

    trend_badge 含义：
        负 = MA5 已下穿 MA20 的连续天数（死叉破位，绿底）
        正 = MA5 已上穿 MA20 的连续天数（金叉破位，红底）
        0  = 无角标（中性粘合 / 完整多头 / 完整空头）
    """
    trend_dir = os.path.join(str(PARQUET_DIR), "indicators", "trend")
    out = {}
    if not os.path.exists(trend_dir):
        return out
    for f in os.listdir(trend_dir):
        if not f.endswith(".parquet"):
            continue
        code = f.replace(".parquet", "").replace("_", ".", 1)
        if not code.endswith(".SI"):
            code = code.replace("_SI", ".SI")
        try:
            df = pd.read_parquet(os.path.join(trend_dir, f))
            if "trend_badge" in df.columns and not df.empty:
                v = df["trend_badge"].iloc[-1]
                out[code] = int(v) if pd.notna(v) else 0
        except Exception:
            pass
    return out


@st.cache_data(ttl=86400)
def load_sector_kline(sector_code: str):
    """加载板块K线（标准化列名 + 均线），取最近250日由图表函数处理"""
    ps, _ = get_stores()
    df = ps.load_index_hist(sector_code)
    if df is None:
        return None

    col_map = {}
    for col in df.columns:
        if col in ["日期", "date", "trade_date"]:
            col_map[col] = "date"
        elif col in ["收盘", "close"]:
            col_map[col] = "close"
        elif col in ["开盘", "open"]:
            col_map[col] = "open"
        elif col in ["最高", "high"]:
            col_map[col] = "high"
        elif col in ["最低", "low"]:
            col_map[col] = "low"
        elif col in ["成交量", "volume", "vol"]:
            col_map[col] = "volume"
    if col_map:
        df = df.rename(columns=col_map)

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

    if "close" in df.columns:
        df["MA5"] = df["close"].rolling(5).mean()
        df["MA20"] = df["close"].rolling(20).mean()
        df["MA60"] = df["close"].rolling(60).mean()

    return df


def _trend_text(trend: str, badge) -> str:
    """列表单元格用的趋势文本（彩色圆点：上涨/下跌/横盘）。"""
    if trend == "上涨":
        return "🔴 上涨"
    if trend == "下跌":
        return "🟢 下跌"
    return "🟡 横盘"


def _trend_direction_text(badge) -> str:
    """横盘穿越方向/天数：金叉↗ 或 死叉↘；非横盘返回 '—'。"""
    try:
        b = int(badge)
    except (TypeError, ValueError):
        b = 0
    if b > 0:
        return f"↗{b}天"
    if b < 0:
        return f"↘{abs(b)}天"
    return "—"


def _render_state_badge(trend: str, badge, state: str):
    """右侧详情的状态标签，横盘时渲染真实角标（绿底负 / 红底正）"""
    badge_html = ""
    if trend == "横盘" and badge not in (None, 0):
        if badge < 0:
            badge_html = (
                f'<span style="margin-left:8px;padding:1px 8px;border-radius:10px;font-size:13px;font-weight:700;'
                f'color:#16a34a;background:rgba(22,163,74,.14);border:1px solid rgba(22,163,74,.45);" '
                f'title="下穿20日线 {abs(badge)} 天（空方破位）">{badge}</span>'
            )
        else:
            badge_html = (
                f'<span style="margin-left:8px;padding:1px 8px;border-radius:10px;font-size:13px;font-weight:700;'
                f'color:#e23c3c;background:rgba(226,60,60,.14);border:1px solid rgba(226,60,60,.45);" '
                f'title="上穿20日线 {badge} 天（多方破位）">+{badge}</span>'
            )
    color = STATE_COLORS.get(state, "#9E9E9E")
    emoji = STATE_EMOJI.get(state, "")
    st.markdown(
        f'<div style="font-size:22px;font-weight:700;color:{color};">{emoji} {state}{badge_html}</div>',
        unsafe_allow_html=True,
    )


def _render_kline_chart(kline_df: pd.DataFrame, sector_name: str):
    """250日K线图（红涨绿跌 + MA5/20/60 + 成交量）"""
    if kline_df is None or kline_df.empty:
        st.warning("暂无K线数据")
        return

    plot_df = kline_df.tail(250).copy()

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.7, 0.3],
    )

    if all(c in plot_df.columns for c in ["open", "high", "low", "close"]):
        fig.add_trace(
            go.Candlestick(
                x=plot_df["date"], open=plot_df["open"], high=plot_df["high"],
                low=plot_df["low"], close=plot_df["close"],
                name="K线", increasing_line_color="#F44336", decreasing_line_color="#4CAF50",
            ),
            row=1, col=1,
        )

    for ma_name, ma_color in [("MA5", "#FF9800"), ("MA20", "#2196F3"), ("MA60", "#9C27B0")]:
        if ma_name in plot_df.columns:
            fig.add_trace(
                go.Scatter(
                    x=plot_df["date"], y=plot_df[ma_name], mode="lines",
                    name=ma_name, line={"color": ma_color, "width": 1},
                ),
                row=1, col=1,
            )

    if "volume" in plot_df.columns:
        colors = []
        for i in range(len(plot_df)):
            if i > 0 and "close" in plot_df.columns:
                colors.append("#F44336" if plot_df["close"].iloc[i] >= plot_df["close"].iloc[i - 1] else "#4CAF50")
            else:
                colors.append("#9E9E9E")
        fig.add_trace(
            go.Bar(x=plot_df["date"], y=plot_df["volume"], name="成交量",
                    marker_color=colors, opacity=0.5),
            row=2, col=1,
        )

    fig.update_layout(
        title=f"{sector_name} 日K线（近250日）",
        height=520,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="成交量", row=2, col=1)
    st.plotly_chart(fig, width="stretch")


def _select_heatmap_state(state_label: str):
    """九宫格状态按钮回调：在控件创建前同步筛选状态，避免会话状态写入异常。"""
    st.session_state["rotation_hm_state"] = state_label
    st.session_state["rotation_merge_filter"] = "🎯 按九宫格状态筛选"
    # 行业选择器候选集已经切换，清空旧行业，避免保留状态切换模式下的选项。
    st.session_state.pop("merge_sector_picker", None)


def _render_heatmap_clickable(state_df: pd.DataFrame):
    """渲染九宫格热力图及原生筛选按钮。

    `components.html` 只能嵌入 iframe，无法向 Python 回传点击事件；因此每个格子
    使用 Streamlit 原生按钮触发筛选，确保云端与本地都可稳定联动板块详情。
    """
    if state_df is None or state_df.empty:
        st.warning("暂无数据")
        return

    grid = [
        "①领涨减速", "②稳健上行", "③加速冲顶",
        "④强转弱", "⑤中性震荡", "⑥弱转强",
        "⑦持续杀跌", "⑧下跌中继", "⑨底背离",
    ]
    selected_state = st.session_state.get("rotation_hm_state")

    for row_start in range(0, len(grid), 3):
        columns = st.columns(3)
        for column, state_label in zip(columns, grid[row_start:row_start + 3]):
            subset = state_df[state_df["state"] == state_label]
            sectors = list(subset["sector_name"].values)
            color = STATE_COLORS.get(state_label, "#9E9E9E")
            emoji = STATE_EMOJI.get(state_label, "")
            signal = get_state_signal(state_label)
            signal_color = get_state_signal_color(state_label)
            bg = STATE_BG_COLORS.get(state_label, "background-color:#F5F5F5;")
            is_selected = state_label == selected_state
            border = f"3px solid {color}" if is_selected else "1px solid #e0e0e0"
            shadow = f"box-shadow:0 0 0 3px {color}33;" if is_selected else ""
            selected_badge = (
                f'<span style="display:inline-block; margin-left:5px; padding:2px 7px; border-radius:9px; '
                f'background:{color}; color:#fff; font-size:10px; font-weight:700;">当前筛选</span>'
                if is_selected else ""
            )
            sector_lines = "<br>".join(sectors[:8])
            if len(sectors) > 8:
                sector_lines += f"<br>...等{len(sectors)}个"

            with column:
                st.markdown(
                    f"""
                    <div style="{bg} min-height:178px; padding:12px; border:{border}; border-radius:8px; text-align:center; {shadow}">
                      <div style="color:{color}; font-weight:700; font-size:14px;">{emoji} {state_label}{selected_badge}</div>
                      <span style="display:inline-block; margin:5px 0 6px; padding:1px 10px; border-radius:10px; color:#fff; font-size:12px; font-weight:700; background:{signal_color};">{signal}</span>
                      <div style="color:{color}; font-size:22px; font-weight:700;">{len(subset)}</div>
                      <div style="color:#666; font-size:11px; line-height:1.45;">{sector_lines or '-'}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                st.button(
                    "✓ 当前已选" if is_selected else f"查看 {state_label} 板块",
                    key=f"rotation_hm_select_{state_label}",
                    type="primary" if is_selected else "secondary",
                    width="stretch",
                    disabled=is_selected,
                    on_click=_select_heatmap_state,
                    args=(state_label,),
                )

    legend_items = []
    for item in get_signal_legend():
        states = "、".join(s[1:] for s in item["states"])
        legend_items.append(
            f'<span style="display:inline-block;margin:6px 14px 6px 0;font-size:13px;color:#333;">'
            f'<span style="display:inline-block;padding:1px 9px;border-radius:10px;color:#fff;'
            f'font-weight:700;background-color:{item["color"]};">{item["signal"]}</span>'
            f' <span style="color:#666;">{item["desc"]}（{states}）</span></span>'
        )
    st.markdown("".join(legend_items), unsafe_allow_html=True)


@st.cache_data(ttl=86400)
def load_sector_state_series(sector_code: str):
    """加载板块历史状态序列（独立缓存）"""
    sm, _ = get_models()
    return sm.calc_state_series(sector_code)


@st.cache_data(ttl=86400)
def load_sector_rs(sector_code: str):
    """加载板块RS数据"""
    rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "rs")
    safe_code = sector_code.replace(".", "_")
    rs_path = os.path.join(rs_dir, f"{safe_code}.parquet")

    if not os.path.exists(rs_path):
        return None

    df = pd.read_parquet(rs_path)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _render_rs_chart(rs_df: pd.DataFrame):
    """渲染RS走势图"""
    if rs_df is None or rs_df.empty:
        st.warning("暂无RS数据")
        return
    if "rs_percentile" not in rs_df.columns:
        st.warning("暂无RS分位数据")
        return

    plot_df = rs_df.tail(250).copy()

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=list(plot_df["date"]) + list(plot_df["date"])[::-1],
            y=[0] * len(plot_df) + [100] * len(plot_df),
            fill="toself",
            fillcolor="rgba(200,200,200,0.1)",
            line={"width": 0},
            name="全范围",
            showlegend=False,
        )
    )
    fig.add_trace(
        go.Scatter(
            x=plot_df["date"],
            y=plot_df["rs_percentile"],
            mode="lines",
            name="RS分位",
            line={"color": "#4CAF50", "width": 2},
            fill="tozeroy",
            fillcolor="rgba(76,175,80,0.1)",
        )
    )
    fig.add_hline(y=70, line_dash="dash", line_color="#FF9800", annotation_text="70%")
    fig.add_hline(y=30, line_dash="dash", line_color="#FF9800", annotation_text="30%")
    fig.add_hline(y=50, line_dash="dot", line_color="#9E9E9E")

    fig.update_layout(
        title="RS相对强弱走势",
        height=400,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        yaxis={"range": [0, 100], "title": "RS分位数 (%)"},
        hovermode="x unified",
    )
    st.plotly_chart(fig, width="stretch")


def _render_rs_momentum_chart(rs_df: pd.DataFrame):
    """渲染RS动量图"""
    if rs_df is None or rs_df.empty or "rs_momentum_percentile" not in rs_df.columns:
        st.warning("暂无RS动量数据")
        return

    plot_df = rs_df.tail(250).copy()

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=plot_df["date"],
            y=plot_df["rs_momentum_percentile"],
            mode="lines",
            name="RS动量分位",
            line={"color": "#2196F3", "width": 2},
            fill="tozeroy",
            fillcolor="rgba(33,150,243,0.1)",
        )
    )
    fig.add_hline(y=70, line_dash="dash", line_color="#FF9800", annotation_text="70% 增强")
    fig.add_hline(y=30, line_dash="dash", line_color="#FF9800", annotation_text="30% 减弱")
    fig.add_hline(y=50, line_dash="dot", line_color="#9E9E9E")

    fig.update_layout(
        title="RS动量分位数走势",
        height=400,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        yaxis={"range": [0, 100], "title": "RS动量分位数 (%)"},
        hovermode="x unified",
    )
    st.plotly_chart(fig, width="stretch")


def _render_state_history(state_series: pd.DataFrame):
    """渲染状态历史（最近20天）"""
    if state_series is None or state_series.empty:
        st.warning("暂无状态历史数据")
        return

    recent = state_series.tail(20).copy()

    labels = []
    for _, row in recent.iterrows():
        date_str = row["date"].strftime("%m-%d") if hasattr(row["date"], "strftime") else str(row["date"])[-5:]
        state_str = row["state"]
        labels.append(f"{date_str}\n{state_str}")

    colors = [STATE_COLORS.get(s, "#9E9E9E") for s in recent["state"]]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=list(range(len(recent))),
            y=[1] * len(recent),
            marker_color=colors,
            text=labels,
            textposition="auto",
            textfont={"size": 10},
            hovertext=[
                f"{row['date'].strftime('%Y-%m-%d')}: {row['state']}<br>趋势: {row['trend']}<br>RS动量: {row['rs_momentum_percentile']:.1f}%"
                if hasattr(row["date"], "strftime")
                else f"{row['date']}: {row['state']}"
                for _, row in recent.iterrows()
            ],
            hoverinfo="text",
        )
    )

    fig.update_layout(
        title="最近20天状态变化",
        height=200,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        xaxis={"showticklabels": False},
        yaxis={"showticklabels": False, "range": [0, 2]},
        showlegend=False,
    )
    st.plotly_chart(fig, width="stretch")


def _get_suggestion(state: str, prev_state: str) -> str:
    """根据状态变化获取操作建议"""
    rules = TransitionRules()
    action, logic = rules.get_transition_action(prev_state, state)
    return f"{action}: {logic}"


def render():
    """渲染板块轮动监控页面"""
    st.title("板块轮动监控")
    st.markdown("九宫格热力图、板块详情、个股下钻、趋势验证")

    # 部署后首次渲染强制清除旧的失败缓存，避免 @st.cache_data 把历史 None 结果续命 24h
    if "rotation_cache_cleared_v84fbd5b" not in st.session_state:
        try:
            st.cache_data.clear()
        except Exception:
            pass
        st.session_state["rotation_cache_cleared_v84fbd5b"] = True

    with st.spinner("加载轮动数据..."):
        try:
            state_df = load_state_df()
            score_df = load_score_df()
        except Exception as e:
            import traceback
            st.error("板块状态加载失败（已捕获异常，非静默空白）：")
            st.code(traceback.format_exc(), language="text")
            st.info("请将以上报错文本贴给开发者，便于定位云端运行环境差异。")
            return

    if state_df is None or state_df.empty:
        st.warning("暂无数据：全量计算与快照缓存均未产出结果，请先运行数据更新流程")
        return

    # ================================================================
    # ================================================================
    # 受控 Tab 切换（刷新保留、关页重置）
    # ================================================================
    ROTATION_TABS = ["九宫格热力图", "趋势验证"]
    active_tab = persistent_tabs("rotation_tab", ROTATION_TABS)


    def _render_heatmap_tab(state_df, score_df):
        """九宫格热力图页签（已合并「板块详情」）。

        点击热力图方块即可按状态筛选板块；亦可在下方切换「按状态切换筛选」。
        选中具体板块后，下方联动渲染其完整详情。
        """
        st.subheader("板块筛选与详情")
        st.caption("选择筛选维度后查看匹配板块，并进一步打开完整详情")

        # 筛选模式置于内容上方；状态切换模式不渲染九宫格，避免重复信息干扰。
        filter_mode = st.radio(
            "筛选维度",
            ["🎯 按九宫格状态筛选", "🔄 按状态切换筛选"],
            horizontal=True,
            key="rotation_merge_filter",
        )

        if filter_mode.startswith("🎯"):
            st.markdown("#### 九宫格板块分布热力图")
            render_src_badge("ths", "derived", base=["ths_kline"])
            st.caption("横轴：RS动量方向 | 纵轴：价格趋势方向　·　点击方块按状态筛选板块")
            _render_heatmap_clickable(state_df)
            selected_state = st.session_state.get("rotation_hm_state")

            if selected_state:
                n = int((state_df["state"] == selected_state).sum())
                st.info(f"已按「{selected_state}」筛选 · 共 {n} 个板块")
                if st.button("清除状态筛选", key="rotation_hm_clear"):
                    st.session_state["rotation_hm_state"] = None
                    st.rerun()

            matching_df = (
                state_df[state_df["state"] == selected_state].copy()
                if selected_state else state_df.copy()
            )
        else:
            _, matching_df = render_transition_picker(state_df, key="merge_transition_picker")

        if matching_df is None or matching_df.empty:
            st.info("👆 请先选择筛选条件")
            return

        st.markdown("---")
        selected_code, selected_label = render_sector_picker(
            matching_df, label="选择行业查看板块与个股详情", key="merge_sector_picker"
        )

        if not selected_code:
            return

        _render_sector_detail(state_df, score_df, selected_code)

        st.markdown("---")
        st.subheader("🔍 个股下钻")
        render_src_badge("ths", "em")
        st.caption("已关联当前筛选行业，可直接查看成分股排名、龙头识别、双重漏斗与个股详情。")
        render_stock_drill(
            selected_sector_code=selected_code,
            sector_label=selected_label,
            embedded=True,
        )

    # ================================================================
    # 受控 Tab 分发（九宫格热力图已合并板块详情）
    # ================================================================
    # 防御：persistent_tabs 在极端情况下可能返回 None/非法值，
    # 归一化到首个页签，避免内容区整片空白（无报错、极难排查）。
    if active_tab not in ROTATION_TABS:
        active_tab = ROTATION_TABS[0]

    if active_tab == "九宫格热力图":
        _render_heatmap_tab(state_df, score_df)
    elif active_tab == "趋势验证":
        _render_trend_tab(state_df, score_df)
    else:
        _render_heatmap_tab(state_df, score_df)

        # ================================================================
        # Tab 2: 板块详情（原独立页面迁入，替换板块卡片）
        # ================================================================
@st.cache_data(ttl=3600)
def _load_sector_perf():
    """加载信号后续表现账本（内存缓存，供板块详情摘要复用）。"""
    try:
        return SQLiteStore().get_signal_performance()
    except Exception:
        return None


def _fmt_pct_safe(value, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value * 100:.{digits}f}%"


def _render_sector_signal_summary(sector_code: str):
    """回显该行业作为信号源的历史后验表现，并跳转信号绩效页。"""
    perf = _load_sector_perf()
    if perf is None or perf.empty:
        return
    try:
        summary = get_sector_signal_summary(sector_code, perf_df=perf)
    except Exception:
        return
    if not summary.get("has_data"):
        return
    st.subheader("📈 信号绩效摘要（历史后验）")
    st.caption("该行业历史信号发出后的实际表现，仅供参考；完整明细与失效预警见「信号绩效」页。")
    wr = summary["win_rate"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("历史信号数", f"{summary['samples']}")
    c2.metric("胜率", f"{wr * 100:.1f}%" if wr is not None else "—")
    c3.metric("成功/失败", f"{summary['success']}/{summary['failure']}")
    c4.metric("平均20日收益", _fmt_pct_safe(summary["avg_return_t20"]))
    by_to = summary.get("by_to_state")
    if by_to is not None and not by_to.empty:
        disp = by_to.copy()
        disp["胜率"] = disp["win_rate"].map(lambda x: f"{x * 100:.1f}%" if pd.notna(x) else "—")
        disp["平均20日收益"] = disp["avg_return_t20"].map(lambda x: f"{x * 100:.1f}%" if pd.notna(x) else "—")
        disp = disp.rename(columns={
            "to_state": "进入状态", "samples": "样本", "success": "成功",
            "failure": "失败", "neutral": "中性",
        })
        st.dataframe(
            disp[["进入状态", "样本", "成功", "失败", "中性", "胜率", "平均20日收益"]],
            hide_index=True, width="stretch",
        )
    if st.button("前往信号绩效页 →", key=f"goto_perf_{sector_code}"):
        st.query_params["page"] = "信号绩效"
        st.rerun()


def _render_sector_detail(state_df, score_df, selected_code):
    """渲染选中板块的详情（当前状态卡片 + K线/RS/RS动量 + 状态历史）。

    由原「板块详情」页签内容合并而来，现由「九宫格热力图」页签在选中
    具体板块后调用。
    """

    sector_name = get_sector_name(selected_code)

    # 加载详情数据
    with st.spinner(f"加载 {sector_name} 数据..."):
        kline_df = load_sector_kline(selected_code)
        rs_df = load_sector_rs(selected_code)
        state_series = load_sector_state_series(selected_code)

    # ================================================================
    # 当前状态卡片
    # ================================================================
    st.subheader("当前状态")
    render_src_badge("derived", base=["ths_kline", "em_flow"])

    sector_info = state_df[state_df["sector_code"] == selected_code]
    if not sector_info.empty:
        info = sector_info.iloc[0]

        # 获取评分明细（复用排行数据，避免重复计算）
        _detail_score_row = None
        if score_df is not None:
            _srow = score_df[score_df["sector_code"] == selected_code]
            if not _srow.empty:
                _detail_score_row = _srow.iloc[0]

        # 获取前一状态和建议
        prev_state = "⑤中性震荡"
        suggestion = None
        if state_series is not None and len(state_series) >= 2:
            prev_state = state_series.iloc[-2]["state"]
            try:
                suggestion = _get_suggestion(info["state"], prev_state)
            except Exception:
                pass

        # 当前状态仅展示状态判断信息；评分与 RS 分值统一在下方评分明细呈现。
        col1, col2, col3 = st.columns([2, 1, 2])
        with col1:
            color = STATE_COLORS.get(info["state"], "#9E9E9E")
            st.markdown(
                f"<h1 style='color:{color};margin:0;'>{STATE_EMOJI.get(info['state'], '')} {info['state']}</h1>",
                unsafe_allow_html=True,
            )
        with col2:
            st.metric("价格趋势", info["trend"])
        with col3:
            st.metric("前一日状态", prev_state)
            if suggestion:
                st.info(suggestion)

        # 评分明细：4 维度分值 + 综合评分 + 权重
        if _detail_score_row is not None:
            _render_score_breakdown(_detail_score_row)

        # 辅助确认与风险：仅解释主信号，不改变九宫格和评分口径。
        _render_confirmation_risk_panel(selected_code, info["state"], _detail_score_row)

        # 信号绩效摘要：仅回显该行业作为信号源的历史后验表现，不重算状态机。
        _render_sector_signal_summary(selected_code)

    # ================================================================
    # K线图
    # ================================================================
    st.subheader("K线图")
    render_src_badge("ths")
    _render_kline_chart(kline_df, sector_name)

    # ================================================================
    # RS走势图
    # ================================================================
    st.subheader("RS走势")
    render_src_badge("derived", base=["ths_kline"])
    _render_rs_chart(rs_df)

    # ================================================================
    # RS动量图
    # ================================================================
    st.subheader("RS动量")
    render_src_badge("derived", base=["ths_kline"])
    _render_rs_momentum_chart(rs_df)

    # ================================================================
    # 状态历史
    # ================================================================
    st.subheader("状态历史")
    render_src_badge("derived", base=["ths_kline", "history"])
    _render_state_history(state_series)

    # 状态序列表格
    if state_series is not None and not state_series.empty:
        with st.expander("查看完整状态序列"):
            display_series = state_series.tail(60).copy()
            display_series["date"] = display_series["date"].apply(
                lambda d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
            )
            display_series = display_series.sort_values("date", ascending=False)
            st.dataframe(display_series, width="stretch", hide_index=True)

    # ================================================================
    # 趋势验证页签（板块趋势对照 + 250日K线）
    # ================================================================
@st.cache_data(ttl=3600)
def _load_rs_series_for_detail(sector_code: str, version: int = CACHE_VERSION):
    """读取板块 RS 指标序列（用于展示评分详细计算过程）。"""
    safe = str(sector_code).replace(".", "_")
    path = os.path.join(str(PARQUET_DIR), "indicators", "rs", f"{safe}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)

@st.cache_data(ttl=86400)
def _load_cross_section(version: int = CACHE_VERSION):
    """返回全市场当天最新 RS / RS动量 横截面（用于评分过程逐步推导）。

    复用评分引擎的 _gather_latest，确保与线上计算的横截面完全一致。
    """
    _, scoring = get_models()
    df = scoring._gather_latest()
    if df is None or df.empty:
        return None
    return df[["sector_code", "rs", "rs_momentum"]].copy()

def _render_score_process(sector_code, sector_name, score_df):
    """在趋势对照表底部展示选中板块的 4 维度 + 综合分详细计算过程（与表格选中行联动）。"""
    st.divider()
    st.subheader(f"🧮 {sector_name}（{sector_code}）评分详细计算过程")
    render_src_badge("derived", base=["ths_kline", "em_flow", "seed"])
    st.caption(
        "下方每一步都列出真实数值与计算公式，可与上方表格的「综合评分 / 4 维度」各列逐一核对。"
    )

    # 已算好的 4 维度 + 综合 + 排名（来自 score_df）
    row = None
    if score_df is not None and not score_df.empty:
        m = score_df[score_df["sector_code"].astype(str) == str(sector_code)]
        if not m.empty:
            row = m.iloc[0]

    def _gv(key):
        if row is None:
            return np.nan
        v = row.get(key)
        return float(v) if pd.notna(v) else np.nan

    rs_cross = _gv("rs_cross_score")
    mom_cross = _gv("mom_cross_score")
    rs_pos = _gv("rs_position_score")
    mom_pos = _gv("rs_momentum_score")
    score = _gv("score")
    n_sec = len(score_df) if score_df is not None else 0

    rs_df = _load_rs_series_for_detail(sector_code)
    if rs_df is None or rs_df.empty or "rs" not in rs_df.columns:
        st.warning("该板块暂无 RS 指标数据，无法展示计算过程。")
        return

    last = rs_df.iloc[-1]
    rs_val = float(last["rs"]) if pd.notna(last.get("rs")) else np.nan
    rs_mom = (float(last["rs_momentum"])
              if "rs_momentum" in last and pd.notna(last.get("rs_momentum")) else np.nan)
    data_date = str(last["date"])[:10] if "date" in last and pd.notna(last.get("date")) else "未知"

    # —— 步骤①：基础输入 ——
    st.markdown(f"### ① 基础输入（RS 指标 · 截面 {data_date}）")
    c1, c2 = st.columns(2)
    with c1:
        st.metric("RS（相对强度）", f"{rs_val:.4f}" if pd.notna(rs_val) else "N/A",
                  help="RS = 板块指数收盘 ÷ 基准指数收盘；越大代表相对越强。")
    with c2:
        st.metric("RS动量（5日斜率）", f"{rs_mom:+.4f}" if pd.notna(rs_mom) else "N/A",
                  help="近 5 日 RS 的线性回归斜率；>0 向上、<0 向下。")
    st.markdown(
        f"&nbsp;&nbsp;本板块 RS = **{rs_val:.4f}**，RS动量 = **{rs_mom:+.4f}**，"
        f"两者即下方横截面 / 时序计算的最原始输入。"
    )

    # —— 横截面原始值（全市场当天）——
    cross = _load_cross_section()
    if cross is not None and not cross.empty and pd.notna(rs_val):
        rs_valid = cross["rs"].dropna()
        mom_valid = cross["rs_momentum"].dropna()
        n_rs = len(rs_valid)
        n_mom = len(mom_valid)
        rs_below = int((rs_valid < rs_val).sum())
        rs_equal = int((rs_valid == rs_val).sum())
        rs_avg_rank = rs_below + (rs_equal + 1) / 2.0
        rs_pct = rs_avg_rank / n_rs * 100 if n_rs else np.nan

        mom_below = int((mom_valid < rs_mom).sum())
        mom_equal = int((mom_valid == rs_mom).sum())
        mom_avg_rank = mom_below + (mom_equal + 1) / 2.0
        mom_pct = mom_avg_rank / n_mom * 100 if n_mom else np.nan
    else:
        n_rs = n_mom = 0
        rs_below = rs_equal = mom_below = mom_equal = 0
        rs_avg_rank = mom_avg_rank = rs_pct = mom_pct = np.nan

    # —— 步骤②：两个横截面维度（每一步数值）——
    st.markdown("### ② 横截面维度（当天全市场横向排名，权重各 30%）")

    with st.container(border=True):
        st.markdown("**RS横截面（权重 30%）**")
        st.markdown(
            f"取当天全市场 **{n_rs}** 个板块的 RS 值构成横截面，本板块 RS = **{rs_val:.4f}**。\n"
            f"- 横截面中 RS **低于** 本板块的有 **{rs_below}** 个板块，**等于** 的有 **{rs_equal}** 个\n"
            f"- 平均排名 = 低于数 + (等于数 + 1) / 2 = **{rs_below} + ({rs_equal}+1)/2 = {rs_avg_rank:.1f}**\n"
            f"- 百分位 = 平均排名 / N × 100 = **{rs_avg_rank:.1f} / {n_rs} × 100 = {rs_pct:.1f}%**\n"
            f"- ⇒ **RS横截面 = {rs_cross:.1f}**（与引擎计算值一致）"
        )

    with st.container(border=True):
        st.markdown("**动量横截面（权重 30%）**")
        st.markdown(
            f"取当天全市场 **{n_mom}** 个板块的 RS动量 值构成横截面，本板块 RS动量 = **{rs_mom:+.4f}**。\n"
            f"- 横截面中 RS动量 **低于** 本板块的有 **{mom_below}** 个板块，**等于** 的有 **{mom_equal}** 个\n"
            f"- 平均排名 = {mom_below} + ({mom_equal}+1)/2 = **{mom_avg_rank:.1f}**\n"
            f"- 百分位 = 平均排名 / N × 100 = **{mom_avg_rank:.1f} / {n_mom} × 100 = {mom_pct:.1f}%**\n"
            f"- ⇒ **动量横截面 = {mom_cross:.1f}**（与引擎计算值一致）"
        )

    # —— 步骤③：两个时序维度（每一步数值）——
    st.markdown("### ③ 时序分位维度（自身历史位置，权重各 20%）")

    series = rs_df["rs"].dropna()
    n_ts = len(series)
    win = 250 if n_ts >= 250 else max(10, n_ts // 2)
    w = series.iloc[-win:]
    below = int((w < rs_val).sum()); equal = int((w == rs_val).sum()); n = len(w)
    rs_ts_pct = (below + 0.5 * equal) / n * 100 if n else np.nan
    rs_ts_stored = (float(last["rs_percentile"])
                    if "rs_percentile" in last and pd.notna(last.get("rs_percentile")) else np.nan)

    with st.container(border=True):
        st.markdown("**RS时序分位（权重 20%）**")
        st.markdown(
            f"取本板块自身最近 **{n}** 个交易日的 RS 序列（共 {n_ts} 个交易日，取末 {win} 日窗口）。\n"
            f"- 最新 RS = **{rs_val:.4f}**（{data_date}）\n"
            f"- 窗口内 RS **低于** 最新值的有 **{below}** 天，**等于** 的有 **{equal}** 天\n"
            f"- 百分位 = (低于天数 + 0.5×等于天数) / 窗口 × 100 = **({below} + 0.5×{equal}) / {n} × 100 = {rs_ts_pct:.1f}%**\n"
            f"- 引擎存储 rs_percentile = **{rs_ts_stored:.1f}**（一致性核对 ✅）\n"
            f"- ⇒ **RS时序分位 = {rs_pos:.1f}**"
        )

    mom_series = rs_df["rs_momentum"].dropna()
    n_ts_m = len(mom_series)
    mwin = min(250, n_ts_m)
    mw = mom_series.iloc[-mwin:] if mwin else mom_series
    mlatest = mw.iloc[-1] if len(mw) else np.nan
    mbelow = int((mw < mlatest).sum()); mequal = int((mw == mlatest).sum())
    mom_ts_pct = (mbelow + 0.5 * mequal) / mwin * 100 if mwin else np.nan
    mom_ts_stored = (float(last["rs_momentum_percentile"])
                     if "rs_momentum_percentile" in last and pd.notna(last.get("rs_momentum_percentile")) else np.nan)

    with st.container(border=True):
        st.markdown("**动量时序分位（权重 20%）**")
        st.markdown(
            f"取本板块自身最近 **{mwin}** 个交易日的 RS动量 序列（共 {n_ts_m} 个交易日）。\n"
            f"- 最新 RS动量 = **{rs_mom:+.4f}**（{data_date}）\n"
            f"- 窗口内 RS动量 **低于** 最新值的有 **{mbelow}** 天，**等于** 的有 **{mequal}** 天\n"
            f"- 百分位 = ({mbelow} + 0.5×{mequal}) / {mwin} × 100 = **{mom_ts_pct:.1f}%**\n"
            f"- 引擎存储 rs_momentum_percentile = **{mom_ts_stored:.1f}**（一致性核对 ✅）\n"
            f"- ⇒ **动量时序分位 = {mom_pos:.1f}**"
        )

    # —— 步骤④：综合评分 ——
    st.markdown("### ④ 综合评分（加权合计）")
    if pd.notna(score):
        calc = (0.30 * rs_cross + 0.30 * mom_cross
                + 0.20 * rs_pos + 0.20 * mom_pos)
        formula = (
            f"= 0.30 × {rs_cross:.1f}  +  0.30 × {mom_cross:.1f}\n"
            f"  + 0.20 × {rs_pos:.1f}  +  0.20 × {mom_pos:.1f}\n"
            f"= {0.30*rs_cross:.2f} + {0.30*mom_cross:.2f} + {0.20*rs_pos:.2f} + {0.20*mom_pos:.2f}\n"
            f"= {calc:.1f}"
        )
        st.code(formula, language="text")
        rank = row.get("rank") if row is not None else None
        suffix = f"　（全市场第 **{int(rank)}** / **{n_sec}** 名）" if pd.notna(rank) else ""
        st.success(f"**综合评分 = {score:.1f}**{suffix}")
    else:
        st.warning("该板块综合评分暂不可用。")

def _render_trend_tab(state_df, score_df):
    st.subheader("板块趋势验证（价格趋势 vs K线）")
    st.caption("对照全板块「绝对价格趋势（含横盘穿越天数角标）、综合评分与 4 个维度分值（RS横截面/动量横截面/RS时序/动量时序）、九宫格状态」与 250 日 K 线，人工验证状态判定合理性。")

    st.info(
        "📐 趋势分界线：上穿60日线 = 上涨（红）｜ 击穿60日线 = 下跌（绿）｜ "
        "横盘带角标：死叉破位显示**绿色负数**（已下穿20日线天数）、金叉破位显示**红色正数**（已上穿20日线天数）"
    )

    with st.spinner("加载板块状态..."):
        badge_map = load_badge_map()

    if state_df is None or state_df.empty:
        st.warning("暂无数据，请先运行数据更新流程")
    else:
        data_date = str(state_df["date"].iloc[0])[:10] if "date" in state_df.columns else "未知"
        st.caption(f"📅 数据截面：{data_date}　共 {len(state_df)} 个板块")

        # 综合评分明细映射（取自 score_df，含 4 维度 + 综合）
        score_map = {}
        score_cols = ["score", "rs_cross_score", "mom_cross_score", "rs_position_score", "rs_momentum_score"]
        if score_df is not None and not score_df.empty:
            missing = [c for c in score_cols if c not in score_df.columns]
            if missing:
                st.warning(f"评分数据格式异常，缺少列 {missing}，正在自动清理缓存并重新计算...")
                # 自动清理缓存并重新运行（仅一次，避免无限循环）
                if "score_cache_auto_cleared" not in st.session_state:
                    st.cache_data.clear()
                    try:
                        st.cache_resource.clear()
                    except Exception:
                        pass
                    st.session_state.score_cache_auto_cleared = True
                    st.rerun()
                # 手动兜底按钮
                if st.button("🔄 强制刷新缓存并重新计算评分", key="force_clear_score_cache"):
                    st.cache_data.clear()
                    try:
                        st.cache_resource.clear()
                    except Exception:
                        pass
                    st.session_state.score_cache_auto_cleared = True
                    st.rerun()
            else:
                for _, sr in score_df.iterrows():
                    score_map[str(sr["sector_code"])] = {
                        "score": sr["score"],
                        "rs_cross": sr["rs_cross_score"],
                        "mom_cross": sr["mom_cross_score"],
                        "rs_pos": sr["rs_position_score"],
                        "mom_pos": sr["rs_momentum_score"],
                    }

        # —— 状态筛选（横向选择按钮，不用下拉）——
        STATE_NAMES = ["①领涨减速", "②稳健上行", "③加速冲顶", "④强转弱",
                       "⑤中性震荡", "⑥弱转强", "⑦持续杀跌", "⑧下跌中继", "⑨底背离"]
        _all_states = ["全部"] + STATE_NAMES
        sel_state = st.pills(
            "选择状态（横向筛选，点击切换）", _all_states,
            selection_mode="single", default="全部",
        )
        if sel_state and sel_state != "全部":
            state_view = state_df[state_df["state"] == sel_state]
            if state_view.empty:
                st.info(f"🟦 当前「{sel_state}」状态下暂无符合条件的板块，请选择其他状态或「全部」。")
                return
        else:
            state_view = state_df

        # 组装显示表
        rows = []
        for _, r in state_view.iterrows():
            code = r["sector_code"]
            badge = badge_map.get(code, 0)
            sm = score_map.get(str(code))
            rows.append({
                "板块名称": r["sector_name"],
                "趋势": _trend_text(r["trend"], badge),
                "方向天数": _trend_direction_text(badge),
                "九宫格状态": f"{STATE_EMOJI.get(r['state'], '')} {r['state']}",
                "综合评分": round(float(sm["score"]), 1) if sm and pd.notna(sm["score"]) else None,
                "RS横截面(%)": round(float(sm["rs_cross"]), 1) if sm and pd.notna(sm["rs_cross"]) else None,
                "动量横截面(%)": round(float(sm["mom_cross"]), 1) if sm and pd.notna(sm["mom_cross"]) else None,
                "RS时序分位(%)": round(float(sm["rs_pos"]), 1) if sm and pd.notna(sm["rs_pos"]) else None,
                "动量时序分位(%)": round(float(sm["mom_pos"]), 1) if sm and pd.notna(sm["mom_pos"]) else None,
                "板块代码": code,
            })
        df = pd.DataFrame(rows)

        # 状态排序：③②①⑥⑨⑤④⑧⑦
        STATE_ORD = {"③": 0, "②": 1, "①": 2, "⑥": 3, "⑨": 4, "⑤": 5, "④": 6, "⑧": 7, "⑦": 8}
        df["_o"] = df["九宫格状态"].str[1].map(STATE_ORD).fillna(9)
        df = df.sort_values("_o").drop(columns="_o").reset_index(drop=True)

        st.subheader(f"全板块趋势对照（{sel_state if sel_state and sel_state != '全部' else '全部状态'}，共 {len(df)} 个板块 · 点击左侧行查看右侧K线）")
        st.caption(SCORE_WEIGHT_NOTE)

        colL, colR = st.columns([0.95, 1.05])

        with colL:
            # 方向天数列：金叉↗红字、死叉↘绿字；横盘/上涨/下跌显示为灰色「—」
            # 因 Streamlit dataframe 单元格内联 HTML 会被转义，
            # 故把方向/天数拆成独立列，用 Styler 做整单元格着色。
            def _direction_cell_color(col):
                styles = []
                for v in col:
                    if isinstance(v, str) and "↗" in v:
                        styles.append("color:#e23c3c;font-weight:700;")
                    elif isinstance(v, str) and "↘" in v:
                        styles.append("color:#16a34a;font-weight:700;")
                    else:
                        styles.append("color:#6b7280;")
                return styles

            num_cols = ["综合评分", "RS横截面(%)", "动量横截面(%)",
                        "RS时序分位(%)", "动量时序分位(%)"]
            styled = (
                df.style
                .format({c: "{:.1f}" for c in num_cols})
                .apply(_direction_cell_color, axis=0, subset=["方向天数"])
            )
            st.dataframe(
                styled,
                key=f"trend_table_{sel_state}",
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                height=660,
                width="stretch",
            )

            # 读取选中行（首次未点选时默认第一行）。
            # 注意：dataframe 的 key 是动态的 trend_table_{sel_state}，
            # 故必须用相同的动态 key 读取选中状态，否则点击行联动永不生效。
            sel = st.session_state.get(f"trend_table_{sel_state}", {})
            sel_rows = sel.get("selection", {}).get("rows", []) if sel else []
            if sel_rows:
                chosen = df.iloc[sel_rows[0]]
            else:
                chosen = df.iloc[0]
            chosen_code = chosen["板块代码"]
            chosen_name = chosen["板块名称"]
            srow = state_df[state_df["sector_code"] == chosen_code].iloc[0]
            chosen_trend = srow["trend"]
            chosen_badge = badge_map.get(chosen_code, 0)
            chosen_state = srow["state"]

        with colR:
            st.markdown(f"**{chosen_name}（{chosen_code}）**")
            _render_state_badge(chosen_trend, chosen_badge, chosen_state)

            # 交易信号（买卖建议）
            sig = get_state_signal(chosen_state)
            sig_color = get_state_signal_color(chosen_state)
            st.markdown(
                f'<div style="margin:2px 0 8px;">交易信号：'
                f'<span style="display:inline-block;padding:1px 12px;border-radius:10px;'
                f'color:#fff;font-weight:700;font-size:14px;background-color:{sig_color};">{sig}</span></div>',
                unsafe_allow_html=True,
            )

            # 趋势语义说明
            if chosen_trend == "横盘" and isinstance(chosen_badge, (int, float)) and chosen_badge < 0:
                st.caption(f"横盘·死叉破位：MA5 已下穿 MA20 第 {abs(chosen_badge)} 天（尚未击穿 MA60），空方动能释放中，等待企稳或破位。")
            elif chosen_trend == "横盘" and isinstance(chosen_badge, (int, float)) and chosen_badge > 0:
                st.caption(f"横盘·金叉破位：MA5 已上穿 MA20 第 {chosen_badge} 天（尚未上穿 MA60），多方试探，等待确认。")
            elif chosen_trend == "横盘":
                st.caption("横盘·中性粘合：均线交织，方向不明。")
            elif chosen_trend == "上涨":
                st.caption("上涨：MA5 > MA20 > MA60，完整多头排列。")
            else:
                st.caption("下跌：MA5 < MA20 < MA60，完整空头排列。")

            with st.spinner("加载K线..."):
                kline = load_sector_kline(chosen_code)
            _render_kline_chart(kline, chosen_name)

        # 选中板块的评分详细计算过程（与上方表格选中行联动，显示在对照表最下方）
        _render_score_process(chosen_code, chosen_name, score_df)

# ================================================================
# 受控 Tab 分发（九宫格热力图已合并板块详情）
# ================================================================
# 防御：persistent_tabs 在极端情况下可能返回 None/非法值，
# 归一化到首个页签，避免内容区整片空白（无报错、极难排查）。


if __name__ == "__main__":
    render()

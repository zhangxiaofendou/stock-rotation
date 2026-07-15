"""
个股下钻页面
============
PRD 5.2 实现：板块成分股列表（按相对强弱排序）、龙头识别、
基本面快照（PE/PB/ROE/营收增速）、双重漏斗选股（基本面+资金面）。
"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from data.sources.akshare_source import AkShareSource
from data.storage.parquet_store import ParquetStore
from config.sector_map import SW_LEVEL2_MAP, get_sector_name
from config.logger import get_logger
from model.state_machine import StateMachine
from model.transition import TransitionRules

logger = get_logger(__name__)


# ============================================================
# 缓存资源
# ============================================================
@st.cache_resource
def get_source():
    return AkShareSource()


@st.cache_resource
def get_store():
    return ParquetStore()


@st.cache_resource
def get_state_machine():
    return StateMachine()


# ============================================================
# 状态相关数据加载
# ============================================================
_STATE_SNAPSHOT = None


def load_all_sector_states():
    """加载所有板块的九宫格状态（仅加载一次，全局缓存）"""
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


def detect_state_transitions(all_states_df, days_back=5):
    """检测最近N天内的状态切换

    返回:
        pd.DataFrame: sector_code, sector_name, from_state, to_state,
                      transition_date, days_ago
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

            # 取最后 days_back+1 天的数据来检测切换
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
        except Exception:
            continue

    if not transitions:
        return pd.DataFrame()

    df = pd.DataFrame(transitions)
    df = df.sort_values("date", ascending=False).reset_index(drop=True)
    return df


# ============================================================
# 数据加载（带缓存）
# ============================================================
@st.cache_data(ttl=1800)
def load_spot_all():
    """加载全市场A股实时快照数据"""
    source = get_source()
    try:
        df = source.ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            return df
    except Exception as e:
        logger.warning(f"加载全市场快照失败: {e}")
    return None


@st.cache_data(ttl=86400)
def load_component_stocks(sector_code: str):
    """加载板块成分股列表"""
    source = get_source()
    df = source.get_index_component(sector_code)
    if df is None or df.empty:
        return None
    # 统一列名
    if "stock_code" not in df.columns:
        for col in df.columns:
            col_lower = str(col).lower()
            if "code" in col_lower or "代码" in str(col):
                df = df.rename(columns={col: "stock_code"})
                break
    if "stock_name" not in df.columns:
        for col in df.columns:
            col_lower = str(col).lower()
            if "name" in col_lower or "名称" in str(col):
                df = df.rename(columns={col: "stock_name"})
                break
    # 清洗代码
    df["stock_code"] = df["stock_code"].astype(str).str.replace("'", "").str.strip()
    return df


@st.cache_data(ttl=1800)
def load_stock_fund_flow(stock_code: str):
    """加载个股资金流"""
    source = get_source()
    # 判断市场
    code = str(stock_code).zfill(6)
    if code.startswith(("6", "5")):
        market = "sh"
    else:
        market = "sz"
    try:
        df = source.get_stock_individual_fund_flow(stock=code, market=market)
        return df
    except Exception as e:
        logger.warning(f"加载个股资金流 {code} 失败: {e}")
        return None


@st.cache_data(ttl=86400)
def load_stock_hist_cached(stock_code: str, days: int = 30):
    """加载个股历史行情（优先本地缓存，其次 API）"""
    store = get_store()
    code = str(stock_code).zfill(6)

    # 先尝试本地缓存
    df = store.load_stock_hist(code)
    if df is not None and not df.empty:
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df.tail(days)

    # 本地没有，从 API 拉取
    source = get_source()
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
    try:
        df = source.get_stock_hist(symbol=code, start=start, end=end, adjust="qfq")
        if df is not None and not df.empty:
            if "日期" in df.columns:
                df = df.rename(columns={"日期": "date"})
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception as e:
        logger.warning(f"加载个股历史 {code} 失败: {e}")
        return None


@st.cache_data(ttl=86400)
def load_financial_summary(stock_code: str):
    """加载个股财务摘要（ROE、营收增速等）"""
    source = get_source()
    code = str(stock_code).zfill(6)
    try:
        # 使用东方财富业绩报表接口
        df = source.ak.stock_yjbb_em(date=(datetime.now() - timedelta(days=365)).strftime("%Y%m%d"))
        if df is not None and not df.empty:
            # 筛选该股票
            mask = df["股票代码"].astype(str).str.zfill(6) == code
            stock_df = df[mask]
            if not stock_df.empty:
                return stock_df.iloc[0].to_dict()
    except Exception as e:
        logger.warning(f"加载财务摘要 {code} 失败: {e}")
    return None


# ============================================================
# 数据处理函数
# ============================================================

def _determine_market(code: str) -> str:
    """根据股票代码判断市场"""
    code = str(code).zfill(6)
    if code.startswith("6"):
        return "sh"
    return "sz"


def _format_pe(pe_val) -> str:
    """格式化PE值"""
    if pe_val is None or pd.isna(pe_val) or pe_val <= 0:
        return "N/A"
    return f"{pe_val:.1f}"


def _format_pb(pb_val) -> str:
    """格式化PB值"""
    if pb_val is None or pd.isna(pb_val) or pb_val <= 0:
        return "N/A"
    return f"{pb_val:.2f}"


def _format_pct(val) -> str:
    """格式化百分比"""
    if val is None or pd.isna(val):
        return "N/A"
    return f"{val:+.2f}%"


def _format_money(val) -> str:
    """格式化金额（亿）"""
    if val is None or pd.isna(val):
        return "N/A"
    yi = val / 1e8
    if abs(yi) >= 1:
        return f"{yi:+.2f}亿"
    return f"{val/1e4:+.0f}万"


def build_component_table(component_df: pd.DataFrame, spot_df: pd.DataFrame) -> pd.DataFrame:
    """
    合并成分股列表和实时快照，构建完整成分股表格。
    返回按涨跌幅排序的 DataFrame。
    """
    if component_df is None or component_df.empty:
        return pd.DataFrame()

    if spot_df is None or spot_df.empty:
        # 没有快照数据，只返回成分股列表
        result = component_df[["stock_code", "stock_name"]].copy()
        result["涨跌幅"] = None
        return result

    # 统一代码格式
    spot_codes = spot_df["代码"].astype(str).str.zfill(6)
    component_codes = component_df["stock_code"].astype(str).str.zfill(6)

    # 筛选属于本板块的股票
    spot_in_sector = spot_df[spot_codes.isin(component_codes)].copy()

    if spot_in_sector.empty:
        return component_df[["stock_code", "stock_name"]].copy()

    # 提取关键列
    col_map = {
        "代码": "stock_code",
        "名称": "stock_name",
        "最新价": "latest_price",
        "涨跌幅": "change_pct",
        "涨跌额": "change_amount",
        "成交量": "volume",
        "成交额": "amount",
        "振幅": "amplitude",
        "换手率": "turnover",
        "量比": "volume_ratio",
        "市盈率-动态": "pe",
        "市净率": "pb",
        "总市值": "total_mv",
        "流通市值": "float_mv",
    }

    result_cols = {}
    for src, dst in col_map.items():
        if src in spot_in_sector.columns:
            result_cols[dst] = spot_in_sector[src]

    result = pd.DataFrame(result_cols)
    result["stock_code"] = result["stock_code"].astype(str).str.zfill(6)

    # 按涨跌幅排序
    if "change_pct" in result.columns:
        result = result.sort_values("change_pct", ascending=False, na_position="last").reset_index(drop=True)

    return result


def calculate_fundamental_score(row: dict) -> float:
    """
    计算基本面得分（0-100分）
    维度：PE合理、PB合理、ROE高、营收增速高
    """
    score = 50.0  # 基础分

    pe = row.get("pe")
    pb = row.get("pb")

    # PE：10-40 为合理区间，越低越好
    if pe is not None and not pd.isna(pe) and pe > 0:
        if pe <= 15:
            score += 15
        elif pe <= 25:
            score += 10
        elif pe <= 40:
            score += 5
        elif pe <= 60:
            score += 0
        else:
            score -= 5

    # PB：0.5-5 为合理区间
    if pb is not None and not pd.isna(pb) and pb > 0:
        if pb <= 1.5:
            score += 15
        elif pb <= 3:
            score += 10
        elif pb <= 5:
            score += 5
        else:
            score -= 5

    return max(0, min(100, score))


def calculate_capital_score(row: dict, rank_cols: list) -> float:
    """
    计算资金面得分（0-100分）
    维度：近5日主力净流入、20日动量排名、换手率适中度
    """
    score = 50.0

    # 涨跌幅（日涨幅作为动量参考）
    change_pct = row.get("change_pct")
    if change_pct is not None and not pd.isna(change_pct):
        if change_pct > 5:
            score += 20
        elif change_pct > 2:
            score += 12
        elif change_pct > 0:
            score += 5
        elif change_pct > -2:
            score -= 5
        else:
            score -= 10

    # 换手率适中度
    turnover = row.get("turnover")
    if turnover is not None and not pd.isna(turnover):
        if 3 <= turnover <= 10:
            score += 15  # 活跃但不过热
        elif 1 <= turnover < 3:
            score += 8
        elif turnover > 20:
            score -= 5  # 过热

    return max(0, min(100, score))


# ============================================================
# 渲染函数
# ============================================================

def _render_component_list(merged_df: pd.DataFrame, sector_name: str):
    """渲染成分股排名列表"""
    if merged_df.empty:
        st.warning("暂无该板块成分股数据")
        return

    st.subheader(f"📋 {sector_name} 成分股排名 ({len(merged_df)}只)")

    # 构建展示表
    display_cols = []
    col_config = {}

    if "stock_code" in merged_df.columns:
        display_cols.append("stock_code")
        col_config["stock_code"] = st.column_config.TextColumn("代码", width="small")

    if "stock_name" in merged_df.columns:
        display_cols.append("stock_name")
        col_config["stock_name"] = st.column_config.TextColumn("名称", width="medium")

    if "latest_price" in merged_df.columns:
        display_cols.append("latest_price")
        col_config["latest_price"] = st.column_config.NumberColumn("最新价", format="%.2f")

    if "change_pct" in merged_df.columns:
        # 创建涨跌幅色阶列
        display_cols.append("change_pct")
        col_config["change_pct"] = st.column_config.NumberColumn(
            "涨跌幅(%)", format="%+.2f",
        )

    if "turnover" in merged_df.columns:
        display_cols.append("turnover")
        col_config["turnover"] = st.column_config.NumberColumn("换手率(%)", format="%.2f")

    if "pe" in merged_df.columns:
        display_cols.append("pe")
        col_config["pe"] = st.column_config.NumberColumn("市盈率", format="%.1f")

    if "pb" in merged_df.columns:
        display_cols.append("pb")
        col_config["pb"] = st.column_config.NumberColumn("市净率", format="%.2f")

    if "total_mv" in merged_df.columns:
        display_cols.append("total_mv")
        col_config["total_mv"] = st.column_config.NumberColumn("总市值(亿)", format="%.0f")

    if "amount" in merged_df.columns:
        display_cols.append("amount")
        col_config["amount"] = st.column_config.NumberColumn("成交额(亿)", format="%.1f")

    # 显示数据表
    display_df = merged_df[display_cols].copy()

    # 高亮涨跌幅
    def highlight_change(val):
        if pd.isna(val):
            return ""
        color = "#F44336" if val > 0 else ("#4CAF50" if val < 0 else "")
        return f"color: {color}" if color else ""

    styled = display_df.style.applymap(highlight_change, subset=["change_pct"] if "change_pct" in display_df.columns else [])
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # 板块统计卡片
    if len(merged_df) > 0:
        st.markdown("---")
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            up_count = len(merged_df[merged_df.get("change_pct", pd.Series([0]*len(merged_df))) > 0]) if "change_pct" in merged_df.columns else 0
            down_count = len(merged_df[merged_df.get("change_pct", pd.Series([0]*len(merged_df))) < 0]) if "change_pct" in merged_df.columns else 0
            st.metric("上涨家数", up_count)
        with col2:
            st.metric("下跌家数", down_count)
        with col3:
            if "change_pct" in merged_df.columns:
                avg_change = merged_df["change_pct"].mean()
                st.metric("平均涨跌幅", f"{avg_change:+.2f}%")
        with col4:
            if "pe" in merged_df.columns:
                valid_pe = merged_df["pe"][merged_df["pe"] > 0]
                if not valid_pe.empty:
                    st.metric("PE中位数", f"{valid_pe.median():.1f}")


def _render_leaders(merged_df: pd.DataFrame, sector_name: str):
    """渲染龙头识别"""
    if merged_df.empty or "change_pct" not in merged_df.columns:
        st.warning("暂无龙头识别数据")
        return

    st.subheader(f"🏆 {sector_name} 龙头识别")

    # 按涨跌幅取 Top 5
    top5 = merged_df.head(5).copy()

    # 龙头股卡片行
    cols = st.columns(min(len(top5), 5))
    for i, (_, row) in enumerate(top5.iterrows()):
        with cols[i]:
            name = row.get("stock_name", "N/A")
            code = row.get("stock_code", "")
            change = row.get("change_pct", 0)
            price = row.get("latest_price", 0)
            pe = row.get("pe", None)
            turnover = row.get("turnover", None)

            change_color = "#F44336" if change > 0 else "#4CAF50"
            st.markdown(
                f"""
                <div style="border:1px solid #e0e0e0;border-radius:8px;padding:12px;text-align:center;">
                    <div style="font-size:14px;font-weight:bold;margin-bottom:4px;">{name}</div>
                    <div style="font-size:11px;color:#888;margin-bottom:8px;">{code}</div>
                    <div style="font-size:20px;font-weight:bold;color:{change_color};">{change:+.2f}%</div>
                    <div style="font-size:12px;color:#888;">¥{price:.2f}</div>
                    <div style="font-size:11px;margin-top:4px;">
                        PE: {_format_pe(pe)} | 换手: {_format_pct(turnover)}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    # 龙头股K线小图
    st.markdown("---")
    st.markdown("#### 龙头股近30日走势")

    kline_cols = st.columns(min(len(top5), 3))
    for i, (_, row) in enumerate(top5.head(3).iterrows()):
        code = row.get("stock_code", "")
        name = row.get("stock_name", "")
        with kline_cols[i]:
            _render_mini_kline(code, name)


def _render_mini_kline(stock_code: str, stock_name: str):
    """渲染个股迷你K线图"""
    df = load_stock_hist_cached(stock_code, days=30)
    if df is None or df.empty:
        st.caption(f"{stock_name}: 暂无行情数据")
        return

    # 标准化列名
    col_map = {}
    for col in df.columns:
        if col in ["开盘", "open"]:
            col_map[col] = "open"
        elif col in ["收盘", "close"]:
            col_map[col] = "close"
        elif col in ["最高", "high"]:
            col_map[col] = "high"
        elif col in ["最低", "low"]:
            col_map[col] = "low"
        elif col in ["成交量", "volume"]:
            col_map[col] = "volume"
    if col_map:
        df = df.rename(columns=col_map)

    required_cols = ["open", "high", "low", "close"]
    if not all(c in df.columns for c in required_cols):
        st.caption(f"{stock_name}: 数据不完整")
        return

    # 计算均线
    df["MA5"] = df["close"].rolling(5).mean()
    df["MA20"] = df["close"].rolling(20).mean()

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.02,
        row_heights=[0.75, 0.25],
    )

    fig.add_trace(
        go.Candlestick(
            x=df["date"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="",
            increasing_line_color="#F44336",
            decreasing_line_color="#4CAF50",
            showlegend=False,
        ),
        row=1, col=1,
    )

    fig.add_trace(
        go.Scatter(x=df["date"], y=df["MA5"], mode="lines",
                    line={"color": "#FF9800", "width": 0.8}, name="MA5", showlegend=False),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(x=df["date"], y=df["MA20"], mode="lines",
                    line={"color": "#2196F3", "width": 0.8}, name="MA20", showlegend=False),
        row=1, col=1,
    )

    if "volume" in df.columns:
        colors = ["#F44336" if df["close"].iloc[i] >= df["close"].iloc[i-1] else "#4CAF50"
                   for i in range(len(df))]
        fig.add_trace(
            go.Bar(x=df["date"], y=df["volume"], marker_color=colors,
                    opacity=0.4, showlegend=False),
            row=2, col=1,
        )

    fig.update_layout(
        title=f"{stock_name} ({stock_code})",
        title_font_size=12,
        height=250,
        margin={"l": 5, "r": 5, "t": 30, "b": 5},
        xaxis_rangeslider_visible=False,
    )
    fig.update_xaxes(showticklabels=False, row=1, col=1)
    fig.update_yaxes(title_text="", row=1, col=1)
    fig.update_yaxes(title_text="", row=2, col=1)

    st.plotly_chart(fig, use_container_width=True)


def _render_stock_funnel(merged_df: pd.DataFrame, sector_name: str):
    """渲染双重漏斗选股"""
    if merged_df.empty:
        st.warning("暂无选股数据")
        return

    st.subheader(f"🎯 双重漏斗选股")

    # 计算基本面得分
    if "pe" in merged_df.columns and "pb" in merged_df.columns:
        merged_df["fundamental_score"] = merged_df.apply(calculate_fundamental_score, axis=1)
    else:
        merged_df["fundamental_score"] = 50.0

    if "change_pct" in merged_df.columns and "turnover" in merged_df.columns:
        merged_df["capital_score"] = merged_df.apply(calculate_capital_score, axis=1)
    else:
        merged_df["capital_score"] = 50.0

    # 综合得分
    merged_df["total_score"] = merged_df["fundamental_score"] * 0.4 + merged_df["capital_score"] * 0.6

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### 🔍 基本面漏斗")
        st.caption("按估值合理性（PE/PB）筛选")
        pe_max = st.slider("PE上限", 0, 200, 80, 5, key="pe_filter")
        pb_max = st.slider("PB上限", 0.0, 20.0, 10.0, 0.5, key="pb_filter")

        fundamental_filtered = merged_df.copy()
        if "pe" in fundamental_filtered.columns:
            fundamental_filtered = fundamental_filtered[
                (fundamental_filtered["pe"] > 0) & (fundamental_filtered["pe"] <= pe_max)
            ]
        if "pb" in fundamental_filtered.columns:
            fundamental_filtered = fundamental_filtered[
                (fundamental_filtered["pb"] > 0) & (fundamental_filtered["pb"] <= pb_max)
            ]

        fundamental_filtered = fundamental_filtered.sort_values(
            "fundamental_score", ascending=False
        )

        st.metric("通过基本面筛选", f"{len(fundamental_filtered)}只 / {len(merged_df)}只")

        if not fundamental_filtered.empty:
            show_cols = ["stock_code", "stock_name", "pe", "pb", "fundamental_score"]
            show_cols = [c for c in show_cols if c in fundamental_filtered.columns]
            st.dataframe(
                fundamental_filtered[show_cols].head(10),
                use_container_width=True,
                hide_index=True,
            )

    with col2:
        st.markdown("#### 💰 资金面漏斗")
        st.caption("按资金流向、动量、活跃度筛选")
        change_min = st.slider("最低涨幅(%)", -10.0, 10.0, -3.0, 0.5, key="change_filter")
        turnover_min = st.slider("最低换手率(%)", 0.0, 30.0, 0.5, 0.5, key="turnover_filter")

        capital_filtered = merged_df.copy()
        if "change_pct" in capital_filtered.columns:
            capital_filtered = capital_filtered[capital_filtered["change_pct"] >= change_min]
        if "turnover" in capital_filtered.columns:
            capital_filtered = capital_filtered[capital_filtered["turnover"] >= turnover_min]

        capital_filtered = capital_filtered.sort_values("capital_score", ascending=False)

        st.metric("通过资金面筛选", f"{len(capital_filtered)}只 / {len(merged_df)}只")

        if not capital_filtered.empty:
            show_cols = ["stock_code", "stock_name", "change_pct", "turnover", "capital_score"]
            show_cols = [c for c in show_cols if c in capital_filtered.columns]
            st.dataframe(
                capital_filtered[show_cols].head(10),
                use_container_width=True,
                hide_index=True,
            )

    # 综合精选：双重漏斗交集
    st.markdown("---")
    st.markdown("#### ⭐ 综合精选（双重漏斗交集 Top 5）")

    if not fundamental_filtered.empty and not capital_filtered.empty:
        # 取两个过滤结果的交集
        fundamental_codes = set(fundamental_filtered["stock_code"])
        capital_codes = set(capital_filtered["stock_code"])
        intersection_codes = fundamental_codes & capital_codes

        intersection = merged_df[merged_df["stock_code"].isin(intersection_codes)]
        intersection = intersection.sort_values("total_score", ascending=False).head(5)

        if not intersection.empty:
            show_cols = ["stock_code", "stock_name", "pe", "pb", "change_pct",
                         "turnover", "fundamental_score", "capital_score", "total_score"]
            show_cols = [c for c in show_cols if c in intersection.columns]

            def highlight_score(val, score_type=""):
                if pd.isna(val):
                    return ""
                if isinstance(val, (int, float)):
                    if val >= 70:
                        return "background-color: #C8E6C9; color: #2E7D32"
                    elif val >= 50:
                        return "background-color: #FFF9C4; color: #F57F17"
                return ""

            styled = intersection[show_cols].style.applymap(
                highlight_score, subset=[c for c in ["total_score", "fundamental_score", "capital_score"] if c in show_cols]
            )
            st.dataframe(styled, use_container_width=True, hide_index=True)
        else:
            st.info("当前筛选条件下无交集，请放宽筛选条件")
    else:
        st.info("请调整筛选条件，确保两边漏斗均有结果后查看交集")


def _render_fund_flow_detail(merged_df: pd.DataFrame):
    """渲染资金流详情（可展开查看 Top 10 个股资金流）"""
    if merged_df.empty:
        return

    st.markdown("---")
    with st.expander("📊 查看 Top 10 个股资金流详情"):
        top10 = merged_df.head(10).copy()

        fund_data = []
        for _, row in top10.iterrows():
            code = row.get("stock_code", "")
            name = row.get("stock_name", "")

            ff_df = load_stock_fund_flow(code)
            if ff_df is not None and not ff_df.empty:
                # 取最近一天
                row_data = {
                    "stock_code": code,
                    "stock_name": name,
                }
                # 尝试提取资金流关键列
                for col in ff_df.columns:
                    col_str = str(col)
                    if "主力" in col_str and "净流入" in col_str:
                        row_data["主力净流入"] = ff_df[col].iloc[-1] if len(ff_df) > 0 else None
                    elif "超大单" in col_str and "净流入" in col_str:
                        row_data["超大单净流入"] = ff_df[col].iloc[-1] if len(ff_df) > 0 else None
                    elif "大单" in col_str and "净流入" in col_str:
                        row_data["大单净流入"] = ff_df[col].iloc[-1] if len(ff_df) > 0 else None
                fund_data.append(row_data)

        if fund_data:
            fund_df = pd.DataFrame(fund_data)
            st.dataframe(fund_df, use_container_width=True, hide_index=True)
        else:
            st.info("暂无资金流数据")


def _render_stock_detail_popup(code: str, name: str):
    """个股详情弹窗：日K + 资金流 + 基本面"""
    with st.expander(f"📈 个股详情: {name} ({code})", expanded=False):
        tab1, tab2, tab3 = st.tabs(["走势图", "资金流", "基本面"])

        with tab1:
            # 完整K线图
            df = load_stock_hist_cached(code, days=90)
            if df is not None and not df.empty:
                col_map = {}
                for col in df.columns:
                    if col in ["开盘", "open"]:
                        col_map[col] = "open"
                    elif col in ["收盘", "close"]:
                        col_map[col] = "close"
                    elif col in ["最高", "high"]:
                        col_map[col] = "high"
                    elif col in ["最低", "low"]:
                        col_map[col] = "low"
                    elif col in ["成交量", "volume"]:
                        col_map[col] = "volume"
                if col_map:
                    df = df.rename(columns=col_map)

                required = ["open", "high", "low", "close"]
                if all(c in df.columns for c in required):
                    df["MA5"] = df["close"].rolling(5).mean()
                    df["MA10"] = df["close"].rolling(10).mean()
                    df["MA20"] = df["close"].rolling(20).mean()

                    fig = make_subplots(
                        rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.03, row_heights=[0.7, 0.3],
                    )
                    fig.add_trace(
                        go.Candlestick(
                            x=df["date"], open=df["open"], high=df["high"],
                            low=df["low"], close=df["close"], name="",
                            increasing_line_color="#F44336",
                            decreasing_line_color="#4CAF50",
                        ), row=1, col=1,
                    )
                    fig.add_trace(
                        go.Scatter(x=df["date"], y=df["MA5"], mode="lines",
                                    line={"color": "#FF9800", "width": 1}, name="MA5"),
                        row=1, col=1,
                    )
                    fig.add_trace(
                        go.Scatter(x=df["date"], y=df["MA10"], mode="lines",
                                    line={"color": "#E91E63", "width": 1}, name="MA10"),
                        row=1, col=1,
                    )
                    fig.add_trace(
                        go.Scatter(x=df["date"], y=df["MA20"], mode="lines",
                                    line={"color": "#2196F3", "width": 1}, name="MA20"),
                        row=1, col=1,
                    )

                    if "volume" in df.columns:
                        colors = ["#F44336" if df["close"].iloc[i] >= df["close"].iloc[max(0, i-1)]
                                   else "#4CAF50" for i in range(len(df))]
                        fig.add_trace(
                            go.Bar(x=df["date"], y=df["volume"],
                                    marker_color=colors, opacity=0.4, name="成交量"),
                            row=2, col=1,
                        )

                    fig.update_layout(
                        height=400, margin={"l": 10, "r": 10, "t": 10, "b": 10},
                        xaxis_rangeslider_visible=False, legend=dict(orientation="h", y=1.1),
                    )
                    st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("暂无行情数据")

        with tab2:
            ff_df = load_stock_fund_flow(code)
            if ff_df is not None and not ff_df.empty:
                # 最近30个交易日资金流趋势
                if len(ff_df) > 0:
                    for col in ff_df.columns:
                        col_str = str(col)
                        if "日期" in col_str or "date" in col_str.lower():
                            ff_df = ff_df.rename(columns={col: "date"})
                            break
                    if "date" in ff_df.columns:
                        ff_df["date"] = pd.to_datetime(ff_df["date"])
                        ff_df = ff_df.sort_values("date").tail(30)

                    # 画主力净流入趋势
                    main_col = None
                    for col in ff_df.columns:
                        col_str = str(col)
                        if "主力" in col_str and "净流入" in col_str:
                            main_col = col
                            break
                    if main_col and "date" in ff_df.columns:
                        fig = go.Figure()
                        fig.add_trace(go.Bar(
                            x=ff_df["date"], y=ff_df[main_col],
                            marker_color=["#F44336" if v >= 0 else "#4CAF50" for v in ff_df[main_col]],
                            name="主力净流入",
                        ))
                        fig.update_layout(
                            height=300, margin={"l": 10, "r": 10, "t": 30, "b": 10},
                            title="近30日主力资金净流入",
                        )
                        st.plotly_chart(fig, use_container_width=True)

                    st.dataframe(ff_df.tail(30), use_container_width=True, hide_index=True)
            else:
                st.info("暂无资金流数据")

        with tab3:
            fin = load_financial_summary(code)
            if fin:
                st.markdown("#### 财务摘要")
                col1, col2 = st.columns(2)
                with col1:
                    for key in ["股票简称", "营业收入-同比增长", "净利润-同比增长",
                                "基本每股收益", "净资产收益率"]:
                        if key in fin and fin[key] is not None:
                            st.metric(key, f"{fin[key]}")
                with col2:
                    for key in ["营业收入-营业收入", "净利润-净利润",
                                "每股净资产", "总资产报酬率"]:
                        if key in fin and fin[key] is not None:
                            st.metric(key, f"{fin[key]}")
            else:
                st.info("暂无财务数据")


# ============================================================
# Tab 5: 九宫格状态关联
# ============================================================

# 九宫格 3×3 布局
_GRID_LAYOUT = [
    # row1: 上涨
    ["①领涨减速", "②稳健上行", "③加速冲顶"],
    # row2: 横盘
    ["④强转弱", "⑤中性震荡", "⑥弱转强"],
    # row3: 下跌
    ["⑦持续杀跌", "⑧下跌中继", "⑨底背离"],
]

# 状态颜色映射
_STATE_COLORS = {
    "①领涨减速": ("#FFF3E0", "#E65100"),  # 浅橙/深橙 — 警示
    "②稳健上行": ("#E8F5E9", "#2E7D32"),  # 浅绿/深绿 — 健康
    "③加速冲顶": ("#FCE4EC", "#C62828"),  # 浅红/深红 — 过热
    "④强转弱":   ("#FFF8E1", "#F9A825"),  # 浅黄/深黄 — 恶化
    "⑤中性震荡": ("#ECEFF1", "#546E7A"),  # 浅灰/深灰 — 中性
    "⑥弱转强":   ("#E3F2FD", "#1565C0"),  # 浅蓝/深蓝 — 转机
    "⑦持续杀跌": ("#FFEBEE", "#B71C1C"),  # 浅深红/深红 — 危险
    "⑧下跌中继": ("#F3E5F5", "#6A1B9A"),  # 浅紫/深紫 — 弱势
    "⑨底背离":   ("#E0F2F1", "#00695C"),  # 浅青/深青 — 机会
}

# 动作颜色映射
_ACTION_COLORS = {
    "加仓": "#2E7D32",
    "加仓（第二批）": "#388E3C",
    "分批建仓（第一批）": "#43A047",
    "持有": "#1565C0",
    "持有，不追": "#1976D2",
    "持有，设止盈": "#1E88E5",
    "减仓": "#F9A825",
    "减仓→观望": "#FBC02D",
    "清仓": "#C62828",
    "止损": "#B71C1C",
    "观察": "#78909C",
    "重点关注": "#FF6F00",
    "维持": "#546E7A",
}


def _render_state_grid(current_state: str):
    """渲染九宫格状态可视化 — 3×3 网格，高亮当前板块所在格"""
    st.markdown("#### 🎯 板块所属九宫格状态")

    # 构建 HTML 表格
    rows_html = ""
    for row in _GRID_LAYOUT:
        cells = ""
        for state in row:
            bg, text_color = _STATE_COLORS.get(state, ("#ECEFF1", "#546E7A"))
            if state == current_state:
                # 高亮当前状态：加粗边框 + 标记
                cells += (
                    f'<td style="background:{bg};color:{text_color};padding:16px 12px;'
                    f'text-align:center;font-size:14px;font-weight:bold;'
                    f'border:3px solid #FF6F00;border-radius:6px;">'
                    f'📍 {state}'
                    f'</td>'
                )
            else:
                cells += (
                    f'<td style="background:{bg};color:{text_color};padding:12px 10px;'
                    f'text-align:center;font-size:13px;opacity:0.7;border-radius:4px;">'
                    f'{state}'
                    f'</td>'
                )
        rows_html += f"<tr>{cells}</tr>"

    table_html = (
        f'<table style="width:100%;border-collapse:separate;border-spacing:6px;">'
        f'{rows_html}'
        f'</table>'
        f'<div style="margin-top:8px;display:flex;justify-content:space-around;font-size:12px;color:#888;">'
        f'<span>← 左: RS减弱 | 右: RS增强 →</span>'
        f'<span>↑ 上: 价格上涨 | 下: 价格下跌 ↓</span>'
        f'</div>'
    )
    st.markdown(table_html, unsafe_allow_html=True)


def _render_state_transitions(sector_code: str, sector_name: str):
    """渲染该板块的状态切换历史"""
    st.markdown("#### 📜 板块状态切换历史")

    sm = get_state_machine()
    tr = TransitionRules()

    try:
        state_series = sm.calc_state_series(sector_code)
        if state_series is None or state_series.empty:
            st.info("该板块暂无历史状态数据")
            return
    except Exception as e:
        st.info(f"无法获取状态序列: {e}")
        return

    # 找出切换点
    states = state_series["state"].tolist()
    dates = state_series["date"].tolist()

    transitions = []
    for i in range(1, len(states)):
        if states[i] != states[i - 1]:
            from_s = states[i - 1]
            to_s = states[i]
            action, logic = tr.get_transition_action(from_s, to_s)
            transitions.append({
                "日期": dates[i].strftime("%Y-%m-%d") if hasattr(dates[i], "strftime") else str(dates[i])[:10],
                "切换前": from_s,
                "切换后": to_s,
                "操作建议": f"{action} — {logic}",
            })

    # 显示最近切换
    if transitions:
        # 最近一次切换高亮卡片
        latest = transitions[-1]
        action_color = _ACTION_COLORS.get(latest["操作建议"].split("—")[0].strip(), "#78909C")
        st.markdown(
            f'<div style="border:1px solid #e0e0e0;border-radius:8px;padding:12px;margin-bottom:12px;">'
            f'<span style="font-size:12px;color:#888;">最近一次切换 · {latest["日期"]}</span><br>'
            f'<span style="font-size:18px;font-weight:bold;">{latest["切换前"]} → {latest["切换后"]}</span><br>'
            f'<span style="font-size:13px;background:{action_color};color:white;padding:2px 8px;border-radius:4px;">'
            f'{latest["操作建议"]}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # 最近 10 次切换
        st.caption(f"最近 10 次状态切换（共 {len(transitions)} 次）")
        tdf = pd.DataFrame(transitions[-10:][::-1])
        # 按操作建议着色
        def color_action(val):
            action = str(val).split("—")[0].strip()
            c = _ACTION_COLORS.get(action, "#78909C")
            return f"color:white;background:{c};padding:2px 6px;border-radius:3px;"
        styled = tdf.style.applymap(color_action, subset=["操作建议"])
        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.info("该板块暂无状态切换记录")

    # 当前状态详情
    st.markdown("---")
    latest_row = state_series.iloc[-1]
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("当前状态", latest_row.get("state", "N/A"))
    with col2:
        st.metric("价格趋势", latest_row.get("trend", "N/A"))
    with col3:
        rs_val = latest_row.get("rs_momentum_percentile")
        st.metric("RS动量分位数", f"{rs_val:.1f}%" if rs_val is not None and not pd.isna(rs_val) else "N/A")


def _render_same_state_sectors(current_state: str, current_code: str):
    """展示处于相同状态的其他板块，以及最近发生状态切换的板块"""
    all_states = load_all_sector_states()
    if all_states is None or all_states.empty:
        st.info("暂无板块状态数据")
        return

    st.markdown("#### 🌐 其他板块状态总览")

    # 同状态板块
    same_state = all_states[all_states["state"] == current_state].copy()
    same_state = same_state[same_state["sector_code"] != current_code]

    col1, col2 = st.columns(2)

    with col1:
        bg, _ = _STATE_COLORS.get(current_state, ("#ECEFF1", "#546E7A"))
        st.markdown(
            f'<div style="background:{bg};padding:8px 12px;border-radius:6px;margin-bottom:8px;">'
            f'<b>同处于「{current_state}」的板块</b> ({len(same_state)} 个)'
            f'</div>',
            unsafe_allow_html=True,
        )
        if not same_state.empty:
            for _, r in same_state.head(10).iterrows():
                sc = r.get("sector_code", "")
                sn = r.get("sector_name", sc)
                rs = r.get("rs_momentum_percentile", None)
                rs_str = f" | RS: {rs:.1f}%" if rs is not None and not pd.isna(rs) else ""
                st.caption(f"• {sn} ({sc}){rs_str}")
        else:
            st.caption("仅此板块处于该状态")

    with col2:
        # 最近发生切换的板块
        trans_df = detect_state_transitions(all_states, days_back=3)
        st.markdown("<b>最近3天发生切换的板块</b>", unsafe_allow_html=True)
        if not trans_df.empty:
            for _, r in trans_df.head(10).iterrows():
                sn = r.get("sector_name", "")
                sc = r.get("sector_code", "")
                state_chg = r.get("state_change", "")
                d = str(r.get("date", ""))[:10]
                st.caption(f"• {sn} — {state_chg} ({d})")
        else:
            st.caption("最近3天无板块状态切换")

    # 买入/卖出信号快速扫描
    st.markdown("---")
    st.markdown("#### 📡 信号快速扫描")

    buy_states = {"⑨底背离", "⑥弱转强"}
    sell_states = {"①领涨减速", "④强转弱", "⑦持续杀跌"}

    buy_sectors = all_states[all_states["state"].isin(buy_states)].copy()
    sell_sectors = all_states[all_states["state"].isin(sell_states)].copy()

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown(
            f'<span style="color:#2E7D32;font-weight:bold;">🟢 买入信号板块</span> ({len(buy_sectors)} 个)',
            unsafe_allow_html=True,
        )
        if not buy_sectors.empty:
            for _, r in buy_sectors.iterrows():
                st.caption(f"• {r.get('sector_name', '')} ({r.get('sector_code', '')}) — {r.get('state', '')}")
        else:
            st.caption("暂无买入信号")

    with col_b:
        st.markdown(
            f'<span style="color:#C62828;font-weight:bold;">🔴 卖出信号板块</span> ({len(sell_sectors)} 个)',
            unsafe_allow_html=True,
        )
        if not sell_sectors.empty:
            for _, r in sell_sectors.iterrows():
                st.caption(f"• {r.get('sector_name', '')} ({r.get('sector_code', '')}) — {r.get('state', '')}")
        else:
            st.caption("暂无卖出信号")


def _render_state_tab(sector_code: str, sector_name: str):
    """Tab 5 主入口：九宫格状态关联"""
    st.subheader(f"🧭 {sector_name} 九宫格状态关联")

    # 获取当前板块状态
    all_states = load_all_sector_states()
    current_state = None
    if all_states is not None and not all_states.empty:
        match = all_states[all_states["sector_code"] == sector_code]
        if not match.empty:
            current_state = match.iloc[0]["state"]

    if current_state is None:
        st.warning("无法获取该板块当前九宫格状态，请先运行数据更新")
        return

    # 1. 九宫格可视化
    _render_state_grid(current_state)

    st.markdown("---")

    # 2. 状态切换历史
    _render_state_transitions(sector_code, sector_name)

    st.markdown("---")

    # 3. 同状态板块 + 信号扫描
    _render_same_state_sectors(current_state, sector_code)


# ============================================================
# 辅助函数：三级联动选择器
# ============================================================

def _render_state_grid_visual(all_states_df, highlight_state=None):
    """渲染 3×3 九宫格可视化（含各状态板块数统计）"""
    grid_states = [
        ["①领涨减速", "②稳健上行", "③加速冲顶"],
        ["④强转弱", "⑤中性震荡", "⑥弱转强"],
        ["⑦持续杀跌", "⑧下跌中继", "⑨底背离"],
    ]

    state_counts = {}
    if all_states_df is not None and not all_states_df.empty:
        counts = all_states_df["state"].value_counts()
        for s, c in counts.items():
            state_counts[s] = c

    rows_html = ""
    for row in grid_states:
        cells = ""
        for state in row:
            bg, text_color = _STATE_COLORS.get(state, ("#ECEFF1", "#546E7A"))
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


def _render_state_picker(all_states_df):
    """按九宫格状态筛选 → 返回 (选定状态, 匹配板块DataFrame)"""
    st.markdown("#### 🎯 按九宫格状态筛选")

    _render_state_grid_visual(all_states_df)

    grid_states = [
        "①领涨减速", "②稳健上行", "③加速冲顶",
        "④强转弱", "⑤中性震荡", "⑥弱转强",
        "⑦持续杀跌", "⑧下跌中继", "⑨底背离",
    ]

    col1, col2 = st.columns([3, 2])
    with col1:
        selected_state = st.selectbox(
            "选择九宫格状态",
            options=[""] + grid_states,
            format_func=lambda x: "请选择状态..." if x == "" else x,
            key="state_picker",
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


def _render_transition_picker(all_states_df):
    """按状态切换筛选 → 返回 (选定切换类型, 匹配板块DataFrame)"""
    st.markdown("#### 🔄 按状态切换筛选")

    trans_df = detect_state_transitions(all_states_df, days_back=5)

    if trans_df.empty:
        st.info("最近5天没有板块发生状态切换")
        st.metric("全部板块", f"{len(all_states_df)} 个")
        return None, None

    # 汇总每种切换类型
    buy_targets = {"⑨底背离", "⑥弱转强"}
    sell_targets = {"①领涨减速", "④强转弱", "⑦持续杀跌"}

    transition_types = trans_df.groupby("state_change").agg(
        count=("sector_code", "count"),
        sectors=("sector_name", lambda x: list(x)),
        latest_date=("date", "max"),
    ).reset_index()
    transition_types = transition_types.sort_values("count", ascending=False)

    st.markdown("##### 最近5天状态切换统计")

    for _, row in transition_types.iterrows():
        state_chg = row["state_change"]
        count = row["count"]
        sectors = row["sectors"]
        latest = str(row["latest_date"])[:10]

        to_s = state_chg.split(" → ")[-1] if " → " in state_chg else ""
        badge = ""
        if to_s in buy_targets:
            badge = " 🟢买入"
        elif to_s in sell_targets:
            badge = " 🔴卖出"

        with st.expander(f"**{state_chg}**{badge} — {count} 个板块 | 最近: {latest}", expanded=False):
            for s in sectors[:20]:
                sc = all_states_df[all_states_df["sector_name"] == s]
                sc_state = sc.iloc[0]["state"] if not sc.empty else ""
                st.caption(f"• {s} [当前: {sc_state}]")
            if count > 20:
                st.caption(f"... 还有 {count - 20} 个板块")

    st.markdown("---")

    transition_options = [""] + list(transition_types["state_change"])
    selected_transition = st.selectbox(
        "选择具体的状态切换",
        options=transition_options,
        format_func=lambda x: "请选择切换类型..." if x == "" else x,
        key="transition_picker",
    )

    if not selected_transition:
        st.info("👆 请先选择一个状态切换类型")
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


def _render_sector_picker(matching_df):
    """行业选择器 — 仅显示匹配的板块 → 返回 (code, name)"""
    if matching_df is None or matching_df.empty:
        return None, None

    st.markdown("#### 📊 选择行业查看个股")

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
        key="sector_picker",
    )

    if not selected_code:
        return None, None

    return selected_code, sector_options[selected_code]


# ============================================================
# render() 入口 — 三级联动: 状态/切换 → 行业 → 个股
# ============================================================

_LOADED_COMPONENT_STOCKS = {}
_LOADED_SPOT = None


def render():
    """个股下钻 — 三级联动：九宫格状态/切换 → 行业 → 个股详情"""
    st.title("🔍 个股下钻")
    st.caption("三级联动：九宫格状态 / 状态切换 → 行业 → 成分股详情")

    # ================================================================
    # 第 1 级：选择筛选维度
    # ================================================================
    filter_mode = st.radio(
        "筛选维度",
        ["🎯 按九宫格状态筛选", "🔄 按状态切换筛选"],
        horizontal=True,
    )

    all_states = load_all_sector_states()
    if all_states is None or all_states.empty:
        st.error("无法加载板块状态数据，请先运行数据更新")
        return

    # ================================================================
    # 第 2 级：按状态/切换筛选 → 选行业
    # ================================================================
    matching_df = None
    selected_sector_code = None
    sector_label = ""

    if filter_mode.startswith("🎯"):
        _, matching_df = _render_state_picker(all_states)
    else:
        _, matching_df = _render_transition_picker(all_states)

    if matching_df is not None and not matching_df.empty:
        st.markdown("---")
        selected_sector_code, sector_label = _render_sector_picker(matching_df)

    if not selected_sector_code:
        return

    # ================================================================
    # 第 3 级：加载成分股 → Tab 展示
    # ================================================================
    with st.spinner(f"正在加载成分股数据..."):
        global _LOADED_COMPONENT_STOCKS, _LOADED_SPOT

        if selected_sector_code not in _LOADED_COMPONENT_STOCKS:
            component_df = load_component_stocks(selected_sector_code)
            _LOADED_COMPONENT_STOCKS[selected_sector_code] = component_df
        else:
            component_df = _LOADED_COMPONENT_STOCKS[selected_sector_code]

        if _LOADED_SPOT is None:
            _LOADED_SPOT = load_spot_all()
        spot_df = _LOADED_SPOT

    if component_df is None or component_df.empty:
        st.warning("⚠️ 成分股数据加载失败（网络不稳定或API限流），请稍后重试或切换板块")
        st.info(f"当前选中: **{sector_label}** — 板块状态/切换信息正常可用")
        return

    if spot_df is None or spot_df.empty:
        st.warning("⚠️ 实时行情数据加载失败，将以有限字段展示成分股列表")
        spot_df = pd.DataFrame()

    merged_df = build_component_table(component_df, spot_df)

    # ---- Tab 导航 ----
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📋 成分股排名", "🏆 龙头识别", "🎯 双重漏斗选股", "📈 个股详情", "🧭 九宫格状态"
    ])

    with tab1:
        _render_component_list(merged_df, sector_label)

    with tab2:
        _render_leaders(merged_df, sector_label)

    with tab3:
        _render_stock_funnel(merged_df, sector_label)

    with tab4:
        st.subheader("📈 个股详情查询")
        if not merged_df.empty and "stock_code" in merged_df.columns and "stock_name" in merged_df.columns:
            stock_options = {"": "请选择个股"}
            for _, row in merged_df.iterrows():
                code = row.get("stock_code", "")
                name = row.get("stock_name", "")
                stock_options[code] = f"{name} ({code})"

            selected_stock = st.selectbox(
                "选择个股", options=list(stock_options.keys()),
                format_func=lambda x: stock_options[x],
                key="stock_detail_picker",
            )
            if selected_stock:
                stock_name = merged_df[merged_df["stock_code"] == selected_stock]["stock_name"].iloc[0]
                _render_stock_detail_popup(selected_stock, stock_name)
        else:
            st.info("暂无个股数据")

    with tab5:
        _render_state_tab(selected_sector_code, sector_label)
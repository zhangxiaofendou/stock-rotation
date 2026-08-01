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
import requests
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from data.sources import get_data_source
from data.storage.parquet_store import ParquetStore
from config.sector_map import SW_LEVEL2_MAP, get_sector_name
from config.logger import get_logger
from model.state_machine import StateMachine
from model.transition import TransitionRules
from ai.multi_factor import rank_stocks
from dashboard.components.drill_pickers import (
    load_all_sector_states,
    detect_state_transitions,
    STATE_COLORS as _STATE_COLORS,
    GRID_LAYOUT as _GRID_LAYOUT,
    render_state_picker as _render_state_picker,
    render_transition_picker as _render_transition_picker,
    render_sector_picker as _render_sector_picker,
    render_state_grid_visual as _render_state_grid_visual,
)

logger = get_logger(__name__)


# ============================================================
# 缓存资源
# ============================================================
@st.cache_resource
def get_source():
    return get_data_source()


@st.cache_resource
def get_store():
    return ParquetStore()


@st.cache_resource
def get_state_machine():
    return StateMachine()


# ============================================================
# 数据加载（带缓存）
# ============================================================
def _resolve_repo_path(*parts: str) -> str:
    """
    从当前文件位置解析项目仓库根目录下的相对路径。
    stock_drill.py → dashboard/pages/stock_drill.py
    repo_root → stock-rotation/
    """
    _here = os.path.dirname(os.path.abspath(__file__))             # .../dashboard/pages
    _repo_root = os.path.dirname(os.path.dirname(_here))           # .../stock-rotation
    return os.path.normpath(os.path.join(_repo_root, *parts))


@st.cache_data(ttl=1800)
def load_spot_all():
    """
    加载全市场A股快照（baostock + 本地缓存）
    
    东方财富 push2 API 在部分网络环境被反爬拦截，改用 baostock
    的 query_daily_history_k_AStock 接口，一次获取全市场日K线数据
    （含 close/pctChg/peTTM/pbMRQ/turn/volume/amount），
    约 6 秒完成，数据 T+1 延迟。
    
    备份：本地 parquet 缓存，API 失败时降级。
    """
    import time
    import baostock as bs
    from config.settings import PARQUET_DIR
    
    # ---- 缓存路径 ----
    cache_dir = os.path.join(str(PARQUET_DIR), "cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, "spot_snapshot.parquet")
    
    # ---- 获取最新交易日 ----
    today = datetime.now()
    # 尝试今天，如果还没数据则降级到昨天
    dates_to_try = []
    for offset in range(5):
        d = today - timedelta(days=offset)
        if d.weekday() < 5:  # 排除周末
            dates_to_try.append(d.strftime("%Y-%m-%d"))
    
    df = None
    last_err = None
    
    for date_str in dates_to_try:
        try:
            lg = bs.login()
            if lg.error_code != "0":
                logger.warning(f"baostock 登录失败: {lg.error_msg}")
                continue
            
            rs = bs.query_daily_history_k_AStock(date_str)
            if rs.error_code == "0":
                # 读取全部数据
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                
                if rows:
                    df = pd.DataFrame(rows, columns=rs.fields)
                    logger.info(f"baostock 加载 {date_str} 快照: {len(df)} 只")
                else:
                    logger.info(f"baostock {date_str} 无数据，尝试更早日期")
            else:
                logger.warning(f"baostock {date_str} 查询失败: {rs.error_msg}")
            
            bs.logout()
            
            if df is not None and not df.empty:
                break
                
        except Exception as e:
            last_err = e
            try:
                bs.logout()
            except Exception:
                pass
            logger.warning(f"baostock {date_str} 异常: {e}")
    
    if df is not None and not df.empty:
        # ---- 转换为 build_component_table 期望的列名 ----
        # 代码：sh.600000 → 600000
        df["代码"] = df["code"].str.replace(r"^(sh|sz|bj)\.", "", regex=True).str.zfill(6)
        # 注意：query_daily_history_k_AStock 不返回 code_name，用代码作为占位符名称
        # build_component_table 会从 component_df 用真实名称覆盖
        df["名称"] = df["代码"]
        
        _rename = {}
        for src, dst in {
            "close": "最新价",
            "pctChg": "涨跌幅",
            "peTTM": "市盈率-动态",
            "pbMRQ": "市净率",
            "turn": "换手率",
            "volume": "成交量",
            "amount": "成交额",
        }.items():
            if src in df.columns:
                _rename[src] = dst
        df = df.rename(columns=_rename)
        
        # 仅保留下游需要的列
        keep_cols = ["代码", "名称", "最新价", "涨跌幅", "市盈率-动态", "市净率", "换手率", "成交量", "成交额"]
        df = df[[c for c in keep_cols if c in df.columns]].copy()
        
        # 数值类型转换
        for col in ["最新价", "涨跌幅", "市盈率-动态", "市净率", "换手率", "成交量", "成交额"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        
        logger.info(f"全市场快照加载完成: {len(df)}只 (baostock)")
        
        # 缓存到本地
        try:
            df.to_parquet(cache_file, index=False)
            logger.info(f"快照缓存已保存: {cache_file}")
        except Exception as e:
            logger.warning(f"快照缓存保存失败: {e}")
        
        return df
    
    # ---- API 失败 → 从运行缓存恢复 ----
    if os.path.exists(cache_file):
        cache_age = time.time() - os.path.getmtime(cache_file)
        cache_hours = cache_age / 3600
        logger.warning(f"baostock 加载失败，使用运行缓存（{cache_hours:.1f}小时前）")
        try:
            df = pd.read_parquet(cache_file)
            logger.info(f"加载运行缓存: {len(df)}只")
            return df
        except Exception as e:
            logger.error(f"加载运行缓存失败: {e}")
    
    # ---- 终极兜底：repo 内置缓存文件（不受网络环境影响）----
    _repo_cache = _resolve_repo_path("data", "cache", "daily_spot.parquet")
    if os.path.exists(_repo_cache):
        try:
            df = pd.read_parquet(_repo_cache)
            logger.info(f"加载内置缓存: {len(df)}只 (fallback)")
            return df
        except Exception as e:
            logger.error(f"加载内置缓存失败: {e}")
    
    logger.error("无可用快照数据（API / 运行缓存 / 内置缓存 均失败）")
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
    """加载个股资金流（带超时保护，东财 API 可能不可用）"""
    import concurrent.futures

    def _fetch():
        source = get_source()
        code = str(stock_code).zfill(6)
        if code.startswith(("6", "5")):
            market = "sh"
        else:
            market = "sz"
        return source.get_stock_individual_fund_flow(stock=code, market=market)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_fetch)
            return future.result(timeout=8)
    except (concurrent.futures.TimeoutError, Exception) as e:
        logger.warning(f"加载个股资金流 {stock_code} 超时/失败: {e}")
        return None


@st.cache_data(ttl=86400)
def load_stock_hist_cached(stock_code: str, days: int = 30):
    """加载个股历史行情（优先本地缓存 → baostock API）"""
    import baostock as bs

    store = get_store()
    code = str(stock_code).zfill(6)

    # 先尝试本地缓存
    df = store.load_stock_hist(code)
    if df is not None and not df.empty:
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df.tail(days)

    # 本地没有 → baostock API
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days + 60)).strftime("%Y-%m-%d")

    # 确定市场前缀
    if code.startswith(("6", "5")):
        bs_code = f"sh.{code}"
    elif code.startswith(("0", "3", "2")):
        bs_code = f"sz.{code}"
    elif code.startswith(("8", "4")):
        bs_code = f"bj.{code}"
    else:
        bs_code = f"sh.{code}"  # 默认上交所

    try:
        lg = bs.login()
        if lg.error_code != "0":
            logger.warning(f"baostock 登录失败: {lg.error_msg}")
            return None

        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,preclose,volume,amount,turn,pctChg,peTTM,pbMRQ",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="2",  # 前复权
        )

        if rs.error_code != "0":
            logger.warning(f"baostock 查询 {bs_code} 失败: {rs.error_msg}")
            bs.logout()
            return None

        rows = []
        while rs.next():
            rows.append(rs.get_row_data())

        bs.logout()

        if not rows:
            return None

        df = pd.DataFrame(rows, columns=rs.fields)

        # 数值转换
        for col in ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg", "peTTM", "pbMRQ"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])

        df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)

        # 保存到本地缓存
        try:
            # 只保留核心列避免缓存过大
            cache_cols = ["date", "open", "high", "low", "close", "volume"]
            cache_df = df[[c for c in cache_cols if c in df.columns]].copy()
            store.save_stock_hist(code, cache_df)
        except Exception as e:
            logger.warning(f"保存个股 {code} 历史缓存失败: {e}")

        return df.tail(days)

    except Exception as e:
        logger.warning(f"加载个股历史 {code} 失败: {e}")
        try:
            bs.logout()
        except Exception:
            pass
        return None


@st.cache_data(ttl=86400)
def load_financial_summary(stock_code: str):
    """加载个股财务摘要（ROE、营收增速等，带超时保护）"""
    import concurrent.futures

    def _fetch():
        source = get_source()
        code = str(stock_code).zfill(6)
        df = source.ak.stock_yjbb_em(date=(datetime.now() - timedelta(days=365)).strftime("%Y%m%d"))
        if df is not None and not df.empty:
            mask = df["股票代码"].astype(str).str.zfill(6) == code
            stock_df = df[mask]
            if not stock_df.empty:
                return stock_df.iloc[0].to_dict()
        return None

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_fetch)
            return future.result(timeout=8)
    except (concurrent.futures.TimeoutError, Exception) as e:
        logger.warning(f"加载财务摘要 {stock_code} 超时/失败: {e}")
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

    # ---- 统一成分股代码格式（兼容 .SZ/.SH/.BJ 后缀）----
    _clean_code = lambda s: str(s).replace("'", "").strip().replace(".SZ", "").replace(".SH", "").replace(".BJ", "").replace(".sz", "").replace(".sh", "").replace(".bj", "").zfill(6)
    component_codes_normalized = component_df["stock_code"].apply(_clean_code)

    if spot_df is None or spot_df.empty:
        # 没有快照数据，只返回成分股列表（保持下游兼容的英文列名）
        result = component_df[["stock_code", "stock_name"]].copy()
        result["change_pct"] = None
        result["latest_price"] = None
        result["pe"] = None
        result["pb"] = None
        result["turnover"] = None
        result["amount"] = None
        return result

    # ---- 统一 spot 代码格式 ----
    spot_codes = spot_df["代码"].astype(str).str.zfill(6)

    # 筛选属于本板块的股票（两种格式对比）
    spot_in_sector = spot_df[spot_codes.isin(component_codes_normalized)].copy()

    if spot_in_sector.empty:
        result = component_df[["stock_code", "stock_name"]].copy()
        result["change_pct"] = None
        result["latest_price"] = None
        result["pe"] = None
        result["pb"] = None
        result["turnover"] = None
        result["amount"] = None
        return result

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

    # 股票名称以 component_df 为准（spot 可能不含名称或含占位符）
    if "stock_name" in component_df.columns:
        name_map = dict(zip(component_codes_normalized, component_df["stock_name"]))
        result["stock_name"] = result["stock_code"].map(name_map)

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

    # 市值加分：中等市值（50-500亿）加分，小市值风险扣分
    total_mv = row.get("total_mv")
    if total_mv is not None and not pd.isna(total_mv) and total_mv > 0:
        mv_yi = total_mv / 1e8
        if 100 <= mv_yi <= 500:
            score += 10
        elif 50 <= mv_yi < 100:
            score += 5
        elif mv_yi < 20:
            score -= 5  # 太小市值风险高

    return max(0, min(100, score))


def calculate_capital_score(row: dict) -> float:
    """
    计算资金面得分（0-100分）
    维度：涨跌幅（动量）、换手率（活跃度）、量比（资金关注度）、成交额（流动性）
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

    # 换手率适中度：2%-12% 为活跃区间
    turnover = row.get("turnover")
    if turnover is not None and not pd.isna(turnover):
        if 3 <= turnover <= 10:
            score += 12
        elif 1 <= turnover < 3:
            score += 6
        elif turnover > 20:
            score -= 5

    # 量比：>1 说明今日成交活跃
    volume_ratio = row.get("volume_ratio")
    if volume_ratio is not None and not pd.isna(volume_ratio):
        if volume_ratio >= 2:
            score += 10
        elif volume_ratio >= 1.2:
            score += 5
        elif volume_ratio < 0.5:
            score -= 5

    # 成交额：>1亿 说明流动性好
    amount = row.get("amount")
    if amount is not None and not pd.isna(amount) and amount > 0:
        amount_yi = amount / 1e8
        if amount_yi >= 5:
            score += 8
        elif amount_yi >= 1:
            score += 4
        elif amount_yi < 0.3:
            score -= 3

    return max(0, min(100, score))


# ============================================================
# 渲染函数
# ============================================================

def _render_component_list(merged_df: pd.DataFrame, sector_name: str, has_spot_data: bool = True):
    """渲染成分股排名列表"""
    if merged_df.empty:
        st.warning("暂无该板块成分股数据")
        return

    st.subheader(f"📋 {sector_name} 成分股排名 ({len(merged_df)}只)")

    if not has_spot_data:
        st.warning("⚠️ 实时行情数据暂时不可用，仅展示成分股基本信息。涨跌幅、PE、PB 等指标无法显示。")

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

    # 显示数据表 — 使用 column_config 避免 Styler 导致的乱码
    display_df = merged_df[display_cols].copy()

    # 对涨跌幅列使用色阶列配置
    if "change_pct" in display_df.columns:
        col_config["change_pct"] = st.column_config.NumberColumn(
            "涨跌幅(%)",
            format="%+.2f",
            help="红涨绿跌",
        )

    st.dataframe(
        display_df,
        column_config=col_config,
        width="stretch",
        hide_index=True,
    )

    # 板块统计卡片
    if len(merged_df) > 0:
        st.markdown("---")
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            if "change_pct" in merged_df.columns and merged_df["change_pct"].notna().any():
                up_count = int((merged_df["change_pct"] > 0).sum())
                st.metric("上涨家数", up_count)
            else:
                st.metric("上涨家数", "--")

        with col2:
            if "change_pct" in merged_df.columns and merged_df["change_pct"].notna().any():
                down_count = int((merged_df["change_pct"] < 0).sum())
                st.metric("下跌家数", down_count)
            else:
                st.metric("下跌家数", "--")

        with col3:
            if "change_pct" in merged_df.columns and merged_df["change_pct"].notna().any():
                avg_change = merged_df["change_pct"].mean()
                st.metric("平均涨跌幅", f"{avg_change:+.2f}%")
            else:
                st.metric("平均涨跌幅", "--")

        with col4:
            if "pe" in merged_df.columns:
                valid_pe = merged_df["pe"][merged_df["pe"] > 0]
                if not valid_pe.empty:
                    st.metric("PE中位数", f"{valid_pe.median():.1f}")
                else:
                    st.metric("PE中位数", "--")
            else:
                st.metric("PE中位数", "--")


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

    st.plotly_chart(fig, width="stretch")


def _render_stock_funnel(merged_df: pd.DataFrame, sector_name: str, has_spot_data: bool = True,
                          sector_code: str = None):
    """双重漏斗选股：资金面 Top N% → 基本面 Top K 只，顺序筛选"""
    if merged_df.empty:
        st.warning("暂无选股数据")
        return

    st.subheader(f"🎯 双重漏斗选股 — {sector_name}")

    if not has_spot_data:
        st.warning("⚠️ 实时行情数据暂不可用，资金面和基本面得分均为默认值 50，筛选结果仅供参考。请等待行情数据加载完成后刷新。")
    else:
        st.caption("顺序筛选：先资金面筛出 Top 30%，再基本面从中精选 Top 5-10 只")

    # ---- 计算各维度得分 ----
    if "pe" in merged_df.columns and merged_df["pe"].notna().any() and "pb" in merged_df.columns:
        merged_df["fundamental_score"] = merged_df.apply(calculate_fundamental_score, axis=1)
    else:
        merged_df["fundamental_score"] = 50.0

    if ("change_pct" in merged_df.columns and merged_df["change_pct"].notna().any()) or \
       ("turnover" in merged_df.columns and merged_df["turnover"].notna().any()):
        merged_df["capital_score"] = merged_df.apply(calculate_capital_score, axis=1)
    else:
        merged_df["capital_score"] = 50.0

    total_count = len(merged_df)

    # ============================================================
    # Layer 1: 资金面 — 全板块排序，取 Top N%
    # ============================================================
    st.markdown("---")
    st.markdown("### 💰 第一层：资金面筛选")

    col_pct, _ = st.columns([1, 2])
    with col_pct:
        cap_pct = st.slider(
            "资金面头部比例",
            min_value=10, max_value=50, value=30, step=5,
            help="取资金面得分最高的前 N% 个股",
            key="cap_pct_filter",
        )

    # 按资金面得分全排序
    capital_ranked = merged_df.sort_values("capital_score", ascending=False)
    cap_cutoff = max(1, int(total_count * cap_pct / 100))
    cap_layer = capital_ranked.head(cap_cutoff).copy()

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("全板块个股", f"{total_count} 只")
    with c2:
        st.metric(f"资金面 Top {cap_pct}%", f"{len(cap_layer)} 只")
    with c3:
        avg_cap = cap_layer["capital_score"].mean() if not cap_layer.empty else 0
        st.metric("平均资金面得分", f"{avg_cap:.0f}")

    if cap_layer.empty:
        st.warning("资金面筛选无结果")
        return

    # 显示资金面 Top 10 预览
    with st.expander(f"📊 资金面 Top 10 预览（共 {len(cap_layer)} 只）", expanded=False):
        preview_cols = ["stock_code", "stock_name"]
        for c in ["change_pct", "turnover", "capital_score"]:
            if c in cap_layer.columns:
                preview_cols.append(c)
        st.dataframe(
            cap_layer[preview_cols].head(10),
            column_config={
                "stock_code": st.column_config.TextColumn("代码"),
                "stock_name": st.column_config.TextColumn("名称"),
                "change_pct": st.column_config.NumberColumn("涨跌幅(%)", format="%+.2f"),
                "turnover": st.column_config.NumberColumn("换手率(%)", format="%.1f"),
                "capital_score": st.column_config.NumberColumn("资金得分", format="%.0f"),
            },
            width="stretch",
            hide_index=True,
        )

    # ============================================================
    # Layer 2: 基本面 — 从 Layer 1 结果中精选 Top K 只
    # ============================================================
    st.markdown("---")
    st.markdown("### 🔍 第二层：基本面精选")

    # 保护：cap_layer 太少时跳过第二层筛选
    if len(cap_layer) < 3:
        st.info(f"资金面筛选后仅剩 {len(cap_layer)} 只个股，样本不足跳过第二层基本面精选。以下为全部资金面筛选结果。")
        final_pool = cap_layer.copy()
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("待筛选池", f"{len(cap_layer)} 只")
        with c2:
            st.metric(f"直接输出", f"{len(final_pool)} 只")
        with c3:
            avg_fund = final_pool["fundamental_score"].mean() if not final_pool.empty else 0
            st.metric("平均基本面得分", f"{avg_fund:.0f}")
    else:
        col_k, _ = st.columns([1, 2])
        with col_k:
            final_count = st.slider(
                "最终标的数",
                min_value=min(3, len(cap_layer)), max_value=min(15, len(cap_layer)),
                value=min(5, len(cap_layer)), step=1,
                help="从资金面筛选结果中，按基本面得分选出的最终标的数",
                key="final_count_filter",
            )

        # 按基本面得分排序
        fundamental_ranked = cap_layer.sort_values("fundamental_score", ascending=False)
        final_pool = fundamental_ranked.head(final_count).copy()

        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("待筛选池", f"{len(cap_layer)} 只")
        with c2:
            st.metric(f"基本面精选", f"{len(final_pool)} 只")
        with c3:
            avg_fund = final_pool["fundamental_score"].mean() if not final_pool.empty else 0
            st.metric("平均基本面得分", f"{avg_fund:.0f}")

    # ============================================================
    # 最终标的池展示
    # ============================================================
    st.markdown("---")
    st.markdown(f"### ⭐ 最终标的池（{len(final_pool)} 只）")

    if final_pool.empty:
        st.info("当前条件下无结果，请调整筛选参数")
        return

    # 构建展示表
    show_cols = ["stock_code", "stock_name"]
    for c in ["latest_price", "change_pct", "pe", "pb", "turnover", "capital_score", "fundamental_score"]:
        if c in final_pool.columns:
            show_cols.append(c)

    pool_col_config = {
        "stock_code": st.column_config.TextColumn("代码", width="small"),
        "stock_name": st.column_config.TextColumn("名称", width="medium"),
        "latest_price": st.column_config.NumberColumn("最新价", format="%.2f"),
        "change_pct": st.column_config.NumberColumn("涨跌幅(%)", format="%+.2f"),
        "pe": st.column_config.NumberColumn("PE", format="%.1f"),
        "pb": st.column_config.NumberColumn("PB", format="%.2f"),
        "turnover": st.column_config.NumberColumn("换手(%)", format="%.1f"),
        "capital_score": st.column_config.NumberColumn("资金得分", format="%.0f"),
        "fundamental_score": st.column_config.NumberColumn("基本面得分", format="%.0f"),
    }

    st.dataframe(
        final_pool[show_cols],
        column_config={k: v for k, v in pool_col_config.items() if k in show_cols},
        width="stretch",
        hide_index=True,
    )

    # 标的卡片
    st.markdown("---")
    st.markdown("#### 📌 标的速览")
    card_cols = st.columns(min(len(final_pool), 5))
    for i, (_, row) in enumerate(final_pool.iterrows()):
        with card_cols[i % len(card_cols)]:
            name = row.get("stock_name", "N/A")
            code = row.get("stock_code", "")
            change = row.get("change_pct", None)
            cap_s = row.get("capital_score", 0)
            fund_s = row.get("fundamental_score", 0)

            change_str = f"{change:+.2f}%" if change is not None and not pd.isna(change) else "N/A"
            change_color = "#F44336" if (change or 0) > 0 else ("#4CAF50" if (change or 0) < 0 else "#888")

            st.markdown(
                f"""
                <div style="border:1px solid #e0e0e0;border-radius:8px;padding:10px;text-align:center;">
                    <div style="font-size:14px;font-weight:bold;margin-bottom:2px;">{name}</div>
                    <div style="font-size:11px;color:#888;margin-bottom:4px;">{code}</div>
                    <div style="font-size:16px;font-weight:bold;color:{change_color};">{change_str}</div>
                    <div style="font-size:11px;margin-top:4px;">
                        资金: {cap_s:.0f} | 基本面: {fund_s:.0f}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    # ---- AI：ML 多因子排序（排序辅助，PRD §5.6.3 / §5.6.5）----
    _ml_names = dict(zip(final_pool["stock_code"], final_pool["stock_name"])) if "stock_name" in final_pool.columns else {}
    _render_ml_ranking(final_pool["stock_code"].tolist(), sector_code=sector_code, names=_ml_names)


def _render_ml_ranking(stock_codes: list, sector_code: str = None, names: dict = None):
    """ML 多因子排序辅助（PRD §5.6.3 / §5.6.5）。

    仅对本地已有行情缓存的个股排序，避免触发网络拉取；作为排序辅助参考，
    不生成交易指令。当前本地个股样本有限，走横截面 z-score 透明合成路径。
    """
    from config.settings import PARQUET_DIR
    stock_dir = os.path.join(str(PARQUET_DIR), "stock_hist")
    have = [c for c in (stock_codes or []) if os.path.exists(
        os.path.join(stock_dir, f"{str(c).zfill(6)}.parquet"))]
    if not have:
        st.info("🤖 ML 多因子排序：候选个股暂无本地行情缓存，暂无法排序"
                "（需在每日管线中补充个股历史数据后启用）。")
        return

    names = names or {}
    with st.spinner("🤖 正在计算多因子排序..."):
        res = rank_stocks(have, sector_code=sector_code, names=names)

    st.markdown("---")
    st.markdown("##### 🤖 ML 多因子排序（辅助参考）")
    st.caption((res.get("note") or "") + " ｜ 仅排序辅助，非交易指令。")
    df = res.get("ranked")
    if df is None or df.empty:
        st.info("无足够数据生成排序。")
        return

    show = ["rank", "stock_code", "stock_name", "score_0_100"]
    zmap = {
        "momentum_20d_z": "动量z", "recent_strength_z": "强度z",
        "volume_trend_z": "量能z", "low_vol_z": "低波动z",
        "low_drawdown_z": "低回撤z", "rs_sector_z": "相对板块z",
    }
    for zc in zmap:
        if zc in df.columns:
            show.append(zc)
    st.dataframe(
        df[show].rename(columns={
            "rank": "排名", "stock_code": "代码", "stock_name": "名称", "score_0_100": "综合分", **zmap,
        }),
        column_config={"综合分": st.column_config.NumberColumn("综合分", format="%.1f")},
        width="stretch",
        hide_index=True,
    )
    st.caption(
        f"模型模式：{res.get('model_mode')} ｜ 已排序 {res.get('n_ranked')}/{res.get('n_total')} 只"
        f"（仅含本地有行情者）；各 z 列为横截面标准化，越大越好。"
    )


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
            st.dataframe(fund_df, width="stretch", hide_index=True)
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
                    st.plotly_chart(fig, width="stretch")
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
                        st.plotly_chart(fig, width="stretch")

                    st.dataframe(ff_df.tail(30), width="stretch", hide_index=True)
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
        st.dataframe(tdf, width="stretch", hide_index=True)
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
# render() 入口 — 三级联动: 状态/切换 → 行业 → 个股
# ============================================================

_LOADED_COMPONENT_STOCKS = {}
_LOADED_SPOT = None


def render(selected_sector_code=None, sector_label="", embedded=False):
    """渲染个股下钻。

    默认模式提供完整三级筛选；嵌入模式复用上游已选行业，直接展示该行业成分股。
    """
    if not embedded:
        st.subheader("🔍 个股下钻")
        st.caption("三级联动：九宫格状态 / 状态切换 → 行业 → 成分股详情")

        # ================================================================
        # 第 1、2 级：选择筛选维度 → 选行业
        # ================================================================
        filter_mode = st.radio(
            "筛选维度",
            ["🎯 按九宫格状态筛选", "🔄 按状态切换筛选"],
            horizontal=True,
            key="stock_drill_filter_mode",
        )
        all_states = load_all_sector_states()
        if all_states is None or all_states.empty:
            st.error("无法加载板块状态数据，请先运行数据更新")
            return

        if filter_mode.startswith("🎯"):
            _, matching_df = _render_state_picker(all_states)
        else:
            _, matching_df = _render_transition_picker(all_states)

        if matching_df is not None and not matching_df.empty:
            st.markdown("---")
            selected_sector_code, sector_label = _render_sector_picker(matching_df)

        if not selected_sector_code:
            st.info("💡 请先在上方选择九宫格状态，然后选择一个行业板块，即可查看成分股详情。")
            return
    elif not selected_sector_code:
        st.info("💡 请先选择一个行业板块，即可查看个股下钻。")
        return

    # ================================================================
    # 第 3 级：先加载成分股（快），再尝试补充行情数据（慢但非阻塞）
    # ================================================================
    global _LOADED_COMPONENT_STOCKS, _LOADED_SPOT

    # -- 3a. 成分股加载（快，2-5秒）--
    with st.spinner(f"正在加载 {sector_label} 成分股..."):
        if selected_sector_code not in _LOADED_COMPONENT_STOCKS:
            component_df = load_component_stocks(selected_sector_code)
            _LOADED_COMPONENT_STOCKS[selected_sector_code] = component_df
        else:
            component_df = _LOADED_COMPONENT_STOCKS[selected_sector_code]

    if component_df is None or component_df.empty:
        st.warning("⚠️ 成分股数据加载失败，请稍后重试或切换板块")
        st.info(f"当前选中: **{sector_label}** — 板块状态/切换信息正常可用")
        return

    # -- 3b. 行情数据补充（可能较慢，独立加载不阻塞）--
    spot_df = _LOADED_SPOT
    if _LOADED_SPOT is None:
        with st.spinner("正在补充实时行情数据（首次加载较慢，约30-60秒）..."):
            try:
                spot_df = load_spot_all()
                if spot_df is not None and not spot_df.empty:
                    _LOADED_SPOT = spot_df
            except Exception:
                spot_df = None

    if spot_df is None or spot_df.empty:
        st.info("💡 实时行情数据暂不可用，正以基础字段展示。刷新页面可重试加载。")
        spot_df = pd.DataFrame()

    merged_df = build_component_table(component_df, spot_df)

    # ---- Tab 导航 ----
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📋 成分股排名", "🏆 龙头识别", "🎯 双重漏斗选股", "📈 个股详情", "🧭 九宫格状态"
    ])

    with tab1:
        _render_component_list(merged_df, sector_label, has_spot_data=(spot_df is not None and not spot_df.empty))

    with tab2:
        _render_leaders(merged_df, sector_label)

    with tab3:
        _render_stock_funnel(merged_df, sector_label, has_spot_data=(spot_df is not None and not spot_df.empty),
                             sector_code=selected_sector_code)

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
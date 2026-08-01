"""
A股板块轮动分析系统 - P3看板层
==============================
Streamlit多页应用，侧边栏导航。
"""

import streamlit as st
import sys
import os
from typing import Dict, Any

# 确保项目根目录在 Python path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from data.storage.sqlite_store import SQLiteStore
from config.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 数据状态与刷新
# ============================================================
@st.cache_data(ttl=3600, show_spinner=False)
def get_latest_source_date() -> str:
    """查询数据源(东方财富)最新交易日（取沪深300基准的最新日期）。

    返回空字符串表示查询失败，避免阻塞看板渲染。
    """
    try:
        from data.sources import get_data_source
        source = get_data_source()
        df = source.get_benchmark_hist(symbol="sh000300")
        if df is not None and not df.empty:
            for col in ["date", "日期"]:
                if col in df.columns:
                    latest = df[col].dropna().max()
                    return str(latest)[:10]
    except Exception as e:
        logger.warning(f"查询数据源最新日期失败: {e}")
    return ""


def get_local_data_status():
    """获取本地各数据类型的最新状态。

    返回:
        (latest_date: str, summary_df: pd.DataFrame)
    """
    store = SQLiteStore()
    fresh_df = store.get_freshness_report()

    latest_date = ""
    rows = []
    if fresh_df is not None and not fresh_df.empty:
        for dtype in fresh_df["data_type"].unique():
            sub = fresh_df[fresh_df["data_type"] == dtype]

            end_dates = sub["data_end_date"].dropna().astype(str).tolist()
            valid_ends = [
                d for d in end_dates
                if d and d.lower() not in ("nan", "none") and len(str(d)) == 10
            ]
            latest_end = max(valid_ends) if valid_ends else "—"

            last_update = sub["last_update"].dropna().max()
            total_records = sub["record_count"].sum()
            has_error = (sub["status"] == "error").any()

            if latest_end != "—" and (not latest_date or latest_end > latest_date):
                latest_date = latest_end

            rows.append({
                "数据类型": dtype,
                "最新数据日期": latest_end,
                "最后更新时间": str(last_update)[:19] if pd.notna(last_update) else "—",
                "记录数": int(total_records) if pd.notna(total_records) else "—",
                "状态": "异常" if has_error else "正常",
            })

    return latest_date, pd.DataFrame(rows)


def run_manual_data_update():
    """执行手动数据刷新，返回 (success, message)"""
    try:
        from data.daily_update import run_update
        with st.spinner("正在从东方财富拉取并更新数据，请稍候..."):
            updated, skipped, errors, report = run_update(dry_run=False)
        return True, f"更新完成：更新 {updated} 个板块，跳过 {skipped} 个，错误 {errors} 个。"
    except Exception as e:
        logger.exception("手动刷新数据失败")
        return False, f"刷新失败：{e}"


@st.cache_resource
def ensure_universe():
    """看板启动时确保板块宇宙(东财行业)就绪，并刷新 SQLite 板块元数据。"""
    try:
        from data.sources import get_data_source
        from data.sector_universe import ensure_em_industry_map
        from data.storage.sqlite_store import SQLiteStore
        src = get_data_source()
        ensure_em_industry_map(src)
        SQLiteStore().ensure_sectors()
    except Exception as e:
        logger.warning(f"板块宇宙初始化失败: {e}")


@st.cache_data(ttl=300, show_spinner=False)
def get_run_observability() -> Dict[str, Any]:
    """汇总运行保障信息：最近一次管线运行、下次计划运行、双源校验状态。"""
    out: Dict[str, Any] = {
        "last_run": None,
        "next_run": "—",
        "dual_source": "未配置",
    }
    try:
        from data.storage.sqlite_store import SQLiteStore
        from data.calendar import TradeCalendar
        from datetime import datetime, timedelta
        sqlite = SQLiteStore()
        runs = sqlite.get_last_pipeline_runs(1)
        if runs:
            r = runs[0]
            out["last_run"] = {
                "status": r.get("status"),
                "finished_at": r.get("finished_at"),
                "target_date": r.get("target_date"),
                "error": r.get("error"),
                "steps": r.get("steps"),
            }
            # 双源校验状态从 steps 中解析
            steps = r.get("steps") or ""
            for seg in steps.split(";"):
                seg = seg.strip()
                if seg.startswith("双源校验:"):
                    out["dual_source"] = seg.split(":", 1)[1].strip()
        # 下次计划运行：下一个交易日（每日 22:00 自动更新 + 07:30 兜底）
        today = datetime.now().strftime("%Y-%m-%d")
        if sqlite.count_trade_calendar() > 0:
            nxt = TradeCalendar().next_trading_day(today, offset=1)
            if nxt:
                out["next_run"] = f"{nxt} 22:00"
    except Exception as e:
        logger.warning(f"运行保障信息读取失败: {e}")
    return out


def _render_run_observability():
    """侧栏「运行保障」区块：最近运行 / 下次运行 / 失败原因 / 双源校验。"""
    st.markdown("### 🛠️ 运行保障")
    info = get_run_observability()

    last = info.get("last_run")
    if last:
        status = last.get("status")
        color = {"success": "🟢", "partial": "🟡", "failed": "🔴"}.get(status, "⚪")
        st.markdown(f"{color} **最近运行**：{last.get('finished_at') or '—'}")
        st.caption(f"目标交易日 {last.get('target_date') or '—'} ｜ 双源校验：{info.get('dual_source')}")
        if status == "failed" and last.get("error"):
            st.error(f"失败原因：{last['error'][:200]}")
        elif status == "partial":
            st.warning("部分步骤未完成，详见日志。")
    else:
        st.info("暂无管线运行记录")

    st.caption(f"📅 下次计划运行：{info.get('next_run')}")


def main():
    """主入口"""
    # 页面配置
    st.set_page_config(
        page_title="A股板块轮动分析系统",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # 启动时确保板块宇宙(东财行业)就绪，并刷新 SQLite 板块元数据
    ensure_universe()

    # ================================================================
    # 侧边栏
    # ================================================================
    with st.sidebar:
        st.title("📊 板块轮动分析")
        st.markdown("---")

        # 数据状态与手动刷新
        st.markdown("### 📡 数据状态")

        source_date = get_latest_source_date()
        local_date, summary_df = get_local_data_status()

        col1, col2 = st.columns(2)
        with col1:
            st.caption("东财最新交易日")
            st.markdown(f"**{source_date or '—'}**")
        with col2:
            st.caption("本地最新数据日期")
            st.markdown(f"**{local_date or '—'}**")

        # 手动刷新按钮
        if st.button("🔄 手动刷新全部数据", key="manual_refresh_data"):
            success, msg = run_manual_data_update()
            if success:
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)

        # 各数据类型最新日期明细
        if summary_df is not None and not summary_df.empty:
            st.markdown("##### 各数据类型最新日期")
            st.dataframe(
                summary_df,
                hide_index=True,
                column_config={
                    "数据类型": st.column_config.TextColumn("数据类型"),
                    "最新数据日期": st.column_config.TextColumn("最新数据日期"),
                    "最后更新时间": st.column_config.TextColumn("最后更新时间"),
                    "记录数": st.column_config.NumberColumn("记录数"),
                    "状态": st.column_config.TextColumn("状态"),
                },
            )
        else:
            st.info("暂无数据新鲜度记录")

        st.markdown("---")

        # 运行保障（可观测性）：最近运行 / 下次运行 / 失败原因 / 双源校验
        _render_run_observability()

        st.markdown("---")

        # 导航（刷新保留、关页重置）
        from dashboard.components.nav_state import persistent_radio
        page = persistent_radio(
            "page",
            ["市场温度计", "板块轮动监控", "持仓管理", "信号绩效", "策略研究", "盘后报告"],
            label="导航菜单",
        )

        st.markdown("---")
        st.markdown("### 图例说明")
        st.markdown("🟢 ⑥弱转强、⑨底背离 — 买入信号")
        st.markdown("🟡 ③加速冲顶 — 持有不追")
        st.markdown("🟠 ①领涨减速、④强转弱 — 减仓信号")
        st.markdown("🔴 ⑦持续杀跌、⑧下跌中继 — 风险信号")
        st.markdown("⚪ ⑤中性震荡 — 观望")
        st.markdown("🟢 ②稳健上行 — 持有")

        st.markdown("---")
        st.caption("P3看板层 v1.0")

    # ================================================================
    # 页面路由
    # ================================================================
    if page == "市场温度计":
        from dashboard.pages.market_temp import render
        render()
    elif page == "板块轮动监控":
        from dashboard.pages.rotation import render
        render()
    elif page == "持仓管理":
        from dashboard.pages.portfolio import render
        render()
    elif page == "信号绩效":
        from dashboard.pages.signal_perf import render
        render()
    elif page == "策略研究":
        from dashboard.pages.strategy_research import render
        render()
    elif page == "盘后报告":
        from dashboard.pages.reports import render
        render()


if __name__ == "__main__":
    main()

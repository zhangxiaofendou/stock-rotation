"""
A股板块轮动分析系统 - P3看板层
==============================
Streamlit多页应用，侧边栏导航。
"""

import streamlit as st
import sys
import os

# 确保项目根目录在 Python path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.storage.sqlite_store import SQLiteStore
from data.freshness import DataFreshness


def check_data_freshness():
    """检查数据新鲜度并返回状态信息"""
    store = SQLiteStore()
    freshness = DataFreshness(store)

    fresh_report = store.get_freshness_report()
    if fresh_report.empty:
        return None, None, "unknown", "暂无新鲜度数据"

    # 找最新的数据截止日期（优先使用 sector_hist 的数据）
    latest_date = None
    latest_update = None
    has_stale = False

    for _, row in fresh_report.iterrows():
        end_date = row.get("data_end_date")
        last_update = row.get("last_update")
        status = row.get("status", "ok")

        if status == "stale" or status == "error":
            has_stale = True

        if end_date is not None:
            end_str = str(end_date)
            if end_str and end_str != "nan" and end_str != "None":
                if latest_date is None or end_str > latest_date:
                    latest_date = end_str

        if last_update is not None:
            lu_str = str(last_update)
            if lu_str and lu_str != "nan" and lu_str != "None":
                if latest_update is None or lu_str > latest_update:
                    latest_update = lu_str

    # 从 RS 指标数据中获取实际最新日期（比 freshness 表更准确）
    try:
        import os
        from config.settings import PARQUET_DIR
        rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "rs")
        if os.path.exists(rs_dir):
            for f in os.listdir(rs_dir):
                if f.endswith(".parquet"):
                    import pandas as pd
                    df = pd.read_parquet(os.path.join(rs_dir, f))
                    if "date" in df.columns and len(df) > 0:
                        last = str(df["date"].iloc[-1])[:10]
                        if latest_date is None or last > latest_date:
                            latest_date = last
                    break  # 只检查第一个文件
    except Exception:
        pass

    if has_stale:
        return latest_date, latest_update, "stale", "部分数据已过期，请更新数据"
    elif latest_date:
        return latest_date, latest_update, "ok", "数据正常"
    else:
        return None, None, "unknown", "无法确定数据状态"


def main():
    """主入口"""
    # 页面配置
    st.set_page_config(
        page_title="A股板块轮动分析系统",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # ================================================================
    # 侧边栏
    # ================================================================
    with st.sidebar:
        st.title("📊 板块轮动分析")
        st.markdown("---")

        # 数据新鲜度
        latest_date, latest_update, fresh_status, fresh_msg = check_data_freshness()

        if fresh_status == "stale":
            st.error(f"⚠️ {fresh_msg}")
            if latest_date:
                st.caption(f"最新数据日期: {latest_date}")
            if latest_update:
                st.caption(f"最后更新: {latest_update[:19]}")
        elif latest_date:
            st.success(f"数据更新至 {latest_date}")
            if latest_update:
                st.caption(f"最后更新: {latest_update[:19]}")
        else:
            st.warning("数据状态未知")

        st.markdown("---")

        # 导航（刷新保留、关页重置）
        from dashboard.components.nav_state import persistent_radio
        page = persistent_radio(
            "page",
            ["市场温度计", "板块轮动监控"],
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


if __name__ == "__main__":
    main()

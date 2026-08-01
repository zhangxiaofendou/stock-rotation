"""持仓管理页面：记录真实持仓、查看成本口径的组合概览和操作日志。"""

from datetime import date

import pandas as pd
import streamlit as st

from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from model.state_machine import StateMachine
from portfolio.holdings import PortfolioHoldings
from portfolio.advisor import PortfolioAdvisor
from dashboard.components.data_source_badge import render_src_badge


@st.cache_resource
def get_holdings_service() -> PortfolioHoldings:
    """复用同一账本服务；数据本身不缓存。"""
    return PortfolioHoldings()


def _fmt_cny(value: float) -> str:
    return f"¥{float(value):,.2f}"


@st.cache_data(ttl=3600)
def load_sector_states_for_portfolio() -> pd.DataFrame:
    """独立加载行业状态，避免持仓页依赖轮动页面模块及其 UI 导入链。"""
    return StateMachine(ParquetStore(), SQLiteStore()).calc_all_sectors_state()


def _render_record_form(service: PortfolioHoldings):
    """渲染实际交易/调账录入表单。"""
    with st.expander("➕ 记录实际操作", expanded=False):
        st.caption("录入后会同步更新当前持仓；这里只记录真实成交或数量调整，不生成交易指令。")
        with st.form("portfolio_record_trade", clear_on_submit=True):
            row1 = st.columns([1, 1, 1, 1])
            with row1[0]:
                security_code = st.text_input("证券代码", placeholder="例如 600000")
            with row1[1]:
                security_name = st.text_input("证券名称", placeholder="例如 浦发银行")
            with row1[2]:
                side = st.selectbox("操作", ["BUY", "SELL", "ADJUST"], format_func={"BUY": "买入", "SELL": "卖出", "ADJUST": "调账"}.get)
            with row1[3]:
                trade_date = st.date_input("操作日期", value=date.today())

            row2 = st.columns([1, 1, 1, 1])
            with row2[0]:
                quantity = st.number_input("数量", min_value=0.0001, value=100.0, step=100.0, format="%.4f")
            with row2[1]:
                price = st.number_input("成交价 / 调账参考价", min_value=0.0, value=0.0, step=0.01, format="%.4f")
            with row2[2]:
                fee = st.number_input("费用", min_value=0.0, value=0.0, step=0.01)
            with row2[3]:
                sector_name = st.text_input("所属行业（可选）", placeholder="例如 银行")

            row3 = st.columns([1, 1, 1])
            with row3[0]:
                sector_code = st.text_input("行业代码（可选）", placeholder="例如 801780.SI")
            with row3[1]:
                target_weight = st.number_input("目标仓位 %（可选）", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
            with row3[2]:
                stop_loss = st.number_input("止损价（可选）", min_value=0.0, value=0.0, step=0.01)
            note = st.text_area("备注（可选）", placeholder="例如：按计划建仓 / 组合调仓原因")
            submitted = st.form_submit_button("保存实际操作", type="primary", width="stretch")

        if submitted:
            try:
                if not security_code.strip() or not security_name.strip():
                    raise ValueError("请填写证券代码和证券名称")
                service.record_trade(
                    security_code=security_code,
                    security_name=security_name,
                    side=side,
                    quantity=quantity,
                    price=price,
                    trade_date=trade_date.isoformat(),
                    fee=fee,
                    sector_code=sector_code or None,
                    sector_name=sector_name or None,
                    target_weight=target_weight / 100 if target_weight else None,
                    stop_loss=stop_loss or None,
                    note=note or None,
                )
                st.success("已保存实际操作，并更新当前持仓。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"保存失败：{exc}")


def _render_positions(service: PortfolioHoldings):
    summary = service.summary()
    positions = service.positions()

    st.subheader("我的持仓")
    render_src_badge("derived", base=["user"])
    st.caption("当前为成本口径账本：市值、浮盈亏和组合建议将在行情与风险模块接入后补齐。")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("持仓标的", f"{summary['position_count']} 只")
    c2.metric("成本金额", _fmt_cny(summary["total_cost"]))
    c3.metric("覆盖行业", f"{summary['sector_count']} 个")
    c4.metric("最大单一成本", _fmt_cny(summary["largest_position_cost"]))

    if positions.empty:
        st.info("暂无持仓记录。可通过下方“记录实际操作”录入首笔买入。")
        return

    display = positions.copy()
    display["目标仓位"] = display["target_weight"].map(lambda x: f"{x:.1%}" if pd.notna(x) else "—")
    display["止损价"] = display["stop_loss"].map(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    display = display.rename(columns={
        "security_code": "代码", "security_name": "名称", "asset_type": "类型",
        "sector_name": "所属行业", "quantity": "持仓数量", "avg_cost": "平均成本",
        "cost_amount": "成本金额", "opened_date": "首次建仓日", "note": "备注",
    })
    st.dataframe(
        display[["代码", "名称", "类型", "所属行业", "持仓数量", "平均成本", "成本金额", "目标仓位", "止损价", "首次建仓日", "备注"]],
        hide_index=True,
        width="stretch",
        column_config={
            "平均成本": st.column_config.NumberColumn(format="¥%.4f"),
            "成本金额": st.column_config.NumberColumn(format="¥%.2f"),
            "持仓数量": st.column_config.NumberColumn(format="%.4f"),
        },
    )


def _render_pending_items(service: PortfolioHoldings):
    """展示可追溯的持仓复核事项，避免把通用信号直接转成自动交易。"""
    st.subheader("待处理事项")
    render_src_badge("derived", base=["ths_kline", "em_flow"])
    positions = service.positions()
    if positions.empty:
        st.info("录入持仓后，这里将结合九宫格状态、止损条件、行业集中度和市场风险生成待处理事项。")
        return

    try:
        state_df = load_sector_states_for_portfolio()
    except Exception:
        state_df = None

    items = PortfolioAdvisor(service).build_pending_items(state_df)
    st.caption("事项仅用于复核：行业状态是通用研究信号；止损项需以最新个股行情核验。系统不会自动下单。")
    if items.empty:
        st.success("当前未发现需要按既有规则复核的事项。仍建议定期检查持仓逻辑和风险预算。")
        return

    high_count = int((items["优先级"] == "高").sum())
    medium_count = int((items["优先级"] == "中").sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("待复核事项", f"{len(items)} 项")
    c2.metric("高优先级", f"{high_count} 项")
    c3.metric("中优先级", f"{medium_count} 项")

    st.dataframe(
        items,
        hide_index=True,
        width="stretch",
        column_config={
            "依据": st.column_config.TextColumn("依据", width="large"),
            "待处理事项": st.column_config.TextColumn("待处理事项", width="large"),
        },
    )


def _render_transactions(service: PortfolioHoldings):
    st.subheader("操作日志")
    render_src_badge("derived", base=["user"])
    transactions = service.transactions()
    if transactions.empty:
        st.caption("暂无实际操作记录。")
        return
    display = transactions.rename(columns={
        "trade_date": "日期", "security_code": "代码", "security_name": "名称",
        "side": "操作", "quantity": "数量", "price": "价格", "fee": "费用", "note": "备注",
    })
    display["操作"] = display["操作"].map({"BUY": "买入", "SELL": "卖出", "ADJUST": "调账"})
    st.dataframe(
        display[["日期", "代码", "名称", "操作", "数量", "价格", "费用", "备注"]],
        hide_index=True,
        width="stretch",
        column_config={
            "价格": st.column_config.NumberColumn(format="¥%.4f"),
            "费用": st.column_config.NumberColumn(format="¥%.2f"),
            "数量": st.column_config.NumberColumn(format="%.4f"),
        },
    )


def render():
    """持仓管理主入口。"""
    st.title("💼 持仓管理")
    st.caption("管理你真实持有的标的与实际操作；通用行业信号仍以板块轮动监控中的状态机展示为准。")
    service = get_holdings_service()
    _render_positions(service)
    _render_record_form(service)
    st.markdown("---")
    _render_pending_items(service)
    st.markdown("---")
    _render_transactions(service)

"""持仓管理页面：记录真实持仓、查看成本口径的组合概览和操作日志。"""

from datetime import date

import numpy as np
import pandas as pd
import streamlit as st

from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from model.state_machine import StateMachine
from portfolio.holdings import PortfolioHoldings
from portfolio.advisor import PortfolioAdvisor
from portfolio.stock_lookup import lookup_stock_info, normalize_code
from dashboard.components.data_source_badge import render_src_badge


@st.cache_resource
def get_holdings_service(user_id: str = "") -> PortfolioHoldings:
    """复用同一账本服务；按 user_id 隔离不同使用者的持仓。缓存以 user_id 为键。"""
    return PortfolioHoldings(user_id=user_id)


def _fmt_cny(value: float) -> str:
    return f"¥{float(value):,.2f}"


@st.cache_data(ttl=3600)
def load_sector_states_for_portfolio() -> pd.DataFrame:
    """独立加载行业状态，避免持仓页依赖轮动页面模块及其 UI 导入链。"""
    return StateMachine(ParquetStore(), SQLiteStore()).calc_all_sectors_state()


@st.cache_data(ttl=300, show_spinner=False)
def load_live_quotes_for_portfolio(codes: tuple[str, ...]) -> pd.DataFrame:
    """读取当前持仓实时行情；失败返回空表，不把行情写入持仓账本。"""
    from data.sources.eastmoney_source import EastMoneyLiveSource, _secid_of_stock

    rows = []
    source = EastMoneyLiveSource()
    secids = [_secid_of_stock(code) for code in codes]
    secids = [x for x in secids if x]
    if not secids:
        return pd.DataFrame(columns=["security_code", "market_price", "quote_source"])
    for item in source.get_live_quote(secids, fields="f43,f57,f58,f127,f169,f170"):
        code = str(item.get("f57") or "")
        if not code:
            continue
        try:
            raw = item.get("f43")
            price = float(raw) / 100.0 if isinstance(raw, int) else float(raw)
            rows.append({
                "security_code": code,
                "market_price": price,
                "quote_name": str(item.get("f58") or ""),
                "quote_sector_name": str(item.get("f127") or "") or None,
                "quote_pct": (float(item.get("f170")) / 100.0 if item.get("f170") is not None else np.nan),
                "quote_source": "eastmoney_realtime",
            })
        except (TypeError, ValueError):
            continue
    return pd.DataFrame(rows)


def _build_position_analysis(positions: pd.DataFrame, quotes: pd.DataFrame, states: pd.DataFrame) -> pd.DataFrame:
    """合并成本、实时行情、行业状态，生成左侧交易者的条件式建议。"""
    if positions.empty:
        return pd.DataFrame()
    df = positions.copy()
    if quotes is not None and not quotes.empty:
        df = df.merge(quotes, on="security_code", how="left")
    else:
        df["market_price"] = np.nan
        df["quote_pct"] = np.nan
        df["quote_source"] = None
    df["market_value"] = df["quantity"] * df["market_price"]
    df["profit_amount"] = (df["market_price"] - df["avg_cost"]) * df["quantity"]
    df["profit_pct"] = (df["market_price"] / df["avg_cost"] - 1.0) * 100.0

    state_map = {}
    if states is not None and not states.empty:
        for row in states.to_dict("records"):
            code = str(row.get("sector_code") or "")
            name = str(row.get("sector_name") or "")
            payload = {"state": row.get("state"), "trend": row.get("trend"), "date": str(row.get("date", ""))[:10]}
            if code:
                state_map["code:" + code] = payload
            if name:
                state_map["name:" + name] = payload

    risk_states = {"①领涨减速", "③加速冲顶", "④强转弱", "⑦持续杀跌", "⑧下跌中继"}
    weak_states = {"④强转弱", "⑦持续杀跌", "⑧下跌中继"}
    action_rank = {"尽快决策": 0, "优先复核": 1, "左侧观察": 2, "持有观望": 3, "数据不足": 4}
    rows = []
    for row in df.to_dict("records"):
        sector_code = str(row.get("sector_code") or "")
        sector_name = str(row.get("sector_name") or row.get("quote_sector_name") or "")
        state_info = state_map.get("code:" + sector_code) or state_map.get("name:" + sector_name)
        state = state_info.get("state") if state_info else None
        profit_pct = row.get("profit_pct")
        stop_loss = row.get("stop_loss")
        if not state_info:
            priority, action, reason = "数据不足", "补充行业映射 / 等待数据", "缺少可关联的板块状态，不对持仓方向做推断。"
        elif state in weak_states and pd.notna(profit_pct) and float(profit_pct) < -8:
            priority, action, reason = "尽快决策", "复核逻辑，暂不盲目补仓", f"行业处于{state}，且当前浮亏 {float(profit_pct):.1f}%；左侧交易也要先确认逻辑未破坏。"
        elif state in weak_states:
            priority, action, reason = "优先复核", "观察板块止跌信号", f"行业处于{state}；左侧策略可观察，但不在板块转弱阶段主动摊低成本。"
        elif state in {"⑨底背离", "⑥弱转强"}:
            priority, action, reason = "左侧观察", "分批观察，不追涨", f"行业出现{state}修复信号；左侧交易以试错仓和回踩承接为主，等待板块状态继续改善。"
        elif state in risk_states:
            priority, action, reason = "优先复核", "持有但不加仓", f"行业状态为{state}，暂不适合追高加仓。"
        else:
            priority, action, reason = "持有观望", "持有观望", f"行业状态为{state}，当前没有触发高优先级复核条件。"
        if pd.notna(stop_loss) and pd.notna(row.get("market_price")) and float(row["market_price"]) <= float(stop_loss):
            priority, action, reason = "尽快决策", "核验止损条件", f"实时价已触及/低于预设止损价 {float(stop_loss):.2f}，请结合个股逻辑和成交情况核验。"
        rows.append({
            **row,
            "sector_state": state or "—",
            "state_date": state_info.get("date") if state_info else "",
            "priority": priority,
            "action": action,
            "reason": reason,
        })
    out = pd.DataFrame(rows)
    out["_rank"] = out["priority"].map(action_rank).fillna(9)
    return out.sort_values(["_rank", "security_code"]).drop(columns=["_rank"]).reset_index(drop=True)


def _render_record_form(service: PortfolioHoldings):
    """渲染实际交易/调账录入表单：最少录入 + 公共信息自动带出。

    必填仅：证券代码 + 操作 + 数量 + 成交价。
    证券名称、最新价、所属行业均从公共行情自动带出（查询失败时降级手动填写）。
    """
    with st.expander("➕ 记录实际操作", expanded=False):
        st.caption("只填最少字段：代码 + 操作 + 数量 + 价格。名称 / 最新价 / 行业自动从公共行情带出，可手动修改。")
        # ── 代码查询区（表单外，保证按钮响应）──
        q1, q2 = st.columns([3, 1])
        with q1:
            security_code = st.text_input("证券代码", placeholder="6 位数字，如 600519", key="pl_code")
        with q2:
            query_clicked = st.button("🔍 查询行情", width="stretch")
        if query_clicked:
            code = normalize_code(security_code)
            info = lookup_stock_info(code) if code else None
            if info:
                st.session_state["pl_lookup"] = {"code": code, **info}
                st.session_state["pl_name"] = info["name"]
                if info.get("price"):
                    st.session_state["pl_price"] = float(info["price"])
                st.session_state["pl_sector_name"] = info.get("sector_name") or ""
                st.rerun()
            else:
                st.session_state.pop("pl_lookup", None)
                st.warning("未能自动获取该代码的公共行情（网络不可用或代码有误）。请手动填写名称与价格。")
        info = st.session_state.get("pl_lookup")
        if info:
            tip = f"📋 {info['name']}"
            if info.get("price"):
                tip += f" ｜ 最新价 ¥{float(info['price']):,.2f}"
            if info.get("sector_name"):
                tip += f" ｜ 行业 {info['sector_name']}"
            st.caption(tip)

        with st.form("portfolio_record_trade", clear_on_submit=True):
            row1 = st.columns([1, 1, 1])
            with row1[0]:
                side = st.selectbox("操作", ["BUY", "SELL", "ADJUST"], format_func={"BUY": "买入", "SELL": "卖出", "ADJUST": "调账"}.get)
            with row1[1]:
                quantity = st.number_input("数量", min_value=0.0001, value=100.0, step=100.0, format="%.4f")
            with row1[2]:
                price = st.number_input(
                    "成交价 / 调账参考价", min_value=0.0,
                    value=st.session_state.get("pl_price", 0.0),
                    step=0.01, format="%.4f", key="pl_price",
                    help="已查询到行情时自动填入最新价，可修改。",
                )
            row2 = st.columns([1, 1])
            with row2[0]:
                trade_date = st.date_input("操作日期", value=date.today())
            with row2[1]:
                fee = st.number_input("费用", min_value=0.0, value=0.0, step=0.01)
            security_name = st.text_input(
                "证券名称（留空则保存时自动带出）",
                value=st.session_state.get("pl_name", ""),
                key="pl_name",
            )

            with st.expander("⚙️ 高级选项（可选）"):
                a1, a2, a3 = st.columns(3)
                with a1:
                    sector_name_manual = st.text_input("行业名称（覆盖自动带出）", placeholder="如 白酒Ⅱ")
                with a2:
                    sector_code = st.text_input("行业代码", placeholder="如 801780.SI")
                with a3:
                    target_weight = st.number_input("目标仓位 %", min_value=0.0, max_value=100.0, value=0.0, step=1.0)
                a4, a5 = st.columns([1, 2])
                with a4:
                    stop_loss = st.number_input("止损价", min_value=0.0, value=0.0, step=0.01)
                with a5:
                    note = st.text_input("备注", placeholder="例如：按计划建仓 / 组合调仓原因")
            submitted = st.form_submit_button("保存实际操作", type="primary", width="stretch")

        if submitted:
            try:
                code = normalize_code(security_code)
                if not code:
                    raise ValueError("证券代码必须是 6 位数字")
                # 名称/最新价自动补全：查询区未查过或代码不一致时，保存前补一次
                auto = st.session_state.get("pl_lookup")
                if not auto or auto.get("code") != code:
                    auto = lookup_stock_info(code)
                name = (auto or {}).get("name") or security_name.strip()
                if not name:
                    raise ValueError("无法自动获取证券名称（网络不可用？）。请点击「查询行情」或手动填写证券名称")
                price = price if price and price > 0 else float((auto or {}).get("price") or 0.0)
                if price <= 0:
                    raise ValueError("成交价不能为 0，且未能自动获取最新价。请手动填写成交价")
                sector_name = sector_name_manual.strip() or (auto or {}).get("sector_name")
                service.record_trade(
                    security_code=code,
                    security_name=name,
                    side=side,
                    quantity=quantity,
                    price=price,
                    trade_date=trade_date.isoformat(),
                    fee=fee,
                    sector_code=sector_code.strip() or None,
                    sector_name=sector_name or None,
                    target_weight=target_weight / 100 if target_weight else None,
                    stop_loss=stop_loss or None,
                    note=note.strip() or None,
                )
                for k in ("pl_lookup", "pl_name", "pl_price", "pl_sector_name", "pl_code"):
                    st.session_state.pop(k, None)
                side_label = {"BUY": "买入", "SELL": "卖出", "ADJUST": "调账"}.get(side, side)
                st.success(f"已保存：{name} {side_label} 完成，并同步更新当前持仓。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"保存失败：{exc}")


def _render_positions(service: PortfolioHoldings):
    summary = service.summary()
    positions = service.positions()

    st.subheader("我的持仓与收益")
    render_src_badge("derived", base=["user", "eastmoney_realtime"])
    if positions.empty:
        st.info("暂无持仓记录。可通过下方“记录实际操作”录入首笔买入。")
        return positions, pd.DataFrame()

    quotes = load_live_quotes_for_portfolio(tuple(positions["security_code"].astype(str).tolist()))
    display = positions.merge(quotes, on="security_code", how="left") if not quotes.empty else positions.copy()
    if "market_price" not in display:
        display["market_price"] = np.nan
    display["market_value"] = display["quantity"] * display["market_price"]
    display["profit_amount"] = (display["market_price"] - display["avg_cost"]) * display["quantity"]
    display["profit_pct"] = (display["market_price"] / display["avg_cost"] - 1.0) * 100.0
    has_quote = display["market_value"].notna()
    total_market = float(display.loc[has_quote, "market_value"].sum()) if has_quote.any() else None
    total_profit = float(display.loc[has_quote, "profit_amount"].sum()) if has_quote.any() else None
    cost_amount = float(display["cost_amount"].sum())
    profit_pct = total_profit / cost_amount * 100 if total_profit is not None and cost_amount else None

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("持仓标的", f"{summary['position_count']} 只")
    c2.metric("成本金额", _fmt_cny(cost_amount))
    c3.metric("当前市值", _fmt_cny(total_market) if total_market is not None else "暂无行情")
    c4.metric("浮盈亏", _fmt_cny(total_profit) if total_profit is not None else "暂无行情", f"{profit_pct:+.2f}%" if profit_pct is not None else None)
    c5.metric("覆盖行业", f"{summary['sector_count']} 个")
    st.caption("成本和成交来自你的持仓账本；现价、浮盈亏来自公共实时行情快照，行情获取失败时不替代真实成交数据。")

    table = display.copy()
    table["现价"] = table["market_price"].map(lambda x: f"¥{x:,.2f}" if pd.notna(x) else "暂无")
    table["浮盈亏"] = table["profit_amount"].map(lambda x: f"¥{x:,.2f}" if pd.notna(x) else "暂无")
    table["收益率"] = table["profit_pct"].map(lambda x: f"{x:+.2f}%" if pd.notna(x) else "暂无")
    table["目标仓位"] = table["target_weight"].map(lambda x: f"{x:.1%}" if pd.notna(x) else "—")
    table["止损价"] = table["stop_loss"].map(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    table = table.rename(columns={
        "security_code": "代码", "security_name": "名称", "asset_type": "类型",
        "sector_name": "所属行业", "quantity": "持仓数量", "avg_cost": "平均成本",
        "cost_amount": "成本金额", "opened_date": "首次建仓日", "note": "备注",
    })
    st.dataframe(
        table[["代码", "名称", "类型", "所属行业", "持仓数量", "平均成本", "现价", "成本金额", "浮盈亏", "收益率", "目标仓位", "止损价", "首次建仓日", "备注"]],
        hide_index=True, width="stretch",
        column_config={
            "平均成本": st.column_config.NumberColumn(format="¥%.4f"),
            "成本金额": st.column_config.NumberColumn(format="¥%.2f"),
            "持仓数量": st.column_config.NumberColumn(format="%.4f"),
        },
    )
    return positions, quotes


def _render_position_analysis(service: PortfolioHoldings, positions: pd.DataFrame, quotes: pd.DataFrame):
    st.subheader("持仓分析与决策优先级")
    render_src_badge("derived", base=["user", "eastmoney_realtime", "sector_state"])
    if positions.empty:
        st.info("录入持仓后，这里会按每只股票的板块状态、浮盈亏和止损条件生成左侧交易建议。")
        return
    try:
        states = load_sector_states_for_portfolio()
    except Exception:
        states = pd.DataFrame()
    analysis = _build_position_analysis(positions, quotes, states)
    st.caption("排序原则：需要尽快决策的在前；左侧观察与持有观望在后。建议只给条件化复核，不替代你的交易决定，也不把右侧追涨逻辑套用到左侧交易。")
    urgent = int((analysis["priority"] == "尽快决策").sum())
    review = int((analysis["priority"] == "优先复核").sum())
    observe = int((analysis["priority"].isin(["左侧观察", "持有观望"])).sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("尽快决策", f"{urgent} 只")
    c2.metric("优先复核", f"{review} 只")
    c3.metric("观察/持有", f"{observe} 只")
    view = analysis[["priority", "security_code", "security_name", "sector_name", "sector_state", "market_price", "profit_pct", "action", "reason"]].copy()
    view = view.rename(columns={
        "priority": "优先级", "security_code": "代码", "security_name": "名称", "sector_name": "所属行业",
        "sector_state": "板块状态", "market_price": "现价", "profit_pct": "收益率", "action": "建议动作", "reason": "分析依据",
    })
    view["现价"] = view["现价"].map(lambda x: f"¥{x:,.2f}" if pd.notna(x) else "暂无")
    view["收益率"] = view["收益率"].map(lambda x: f"{x:+.2f}%" if pd.notna(x) else "暂无")
    st.dataframe(view, hide_index=True, width="stretch", column_config={
        "分析依据": st.column_config.TextColumn(width="large"),
        "建议动作": st.column_config.TextColumn(width="medium"),
    })


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
    # 多用户：从登录会话取当前用户，确保只看自己的仓位
    user_id = st.session_state.get("username", "")
    if not user_id:
        st.warning("未识别到登录用户，无法加载持仓。请重新登录后重试。")
        return
    service = get_holdings_service(user_id=user_id)
    st.info(f"👤 当前账户：**{user_id}**（仅显示你自己的持仓）")
    positions, quotes = _render_positions(service)
    st.markdown("---")
    _render_position_analysis(service, positions, quotes)
    _render_record_form(service)
    st.markdown("---")
    _render_pending_items(service)
    st.markdown("---")
    _render_transactions(service)

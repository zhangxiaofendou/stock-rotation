"""持仓管理页面：记录真实持仓、查看成本口径的组合概览和操作日志。"""

from datetime import date
import re
import time

import numpy as np
import pandas as pd
import streamlit as st

from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from model.state_machine import StateMachine
from model.state_history import (
    STATE_CELL,
    X_LABELS,
    Y_LABELS,
    recent_state_runs,
    format_runs_path,
)
from portfolio.holdings import PortfolioHoldings
from portfolio.advisor import PortfolioAdvisor
from portfolio.stock_lookup import lookup_stock_info, normalize_code, resolve_sector
from portfolio.sector_fill import check_and_fill_position, summarize_traces
from portfolio.fees import estimate_trade_fee
from portfolio.trade_import import parse_trades, trades_to_records, trade_dedup_key
from dashboard.components.data_source_badge import render_src_badge
from dashboard.components.state_grid import STATE_COLORS
from dashboard.components.version_indicator import render_version_indicator


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


def _fetch_quotes_batch(codes: tuple[str, ...]) -> list[dict]:
    """批量拉取一组代码的实时行情（东财主源 + 腾讯兜底）。

    抽离为纯函数，由 ``load_live_quotes_for_portfolio`` 只针对缺失标的调用；
    不按整个持仓集合缓存，避免「删一笔 → 集合变化 → 整批行情重拉」的卡顿。
    """
    from data.sources.eastmoney_source import EastMoneyLiveSource, _secid_of_stock
    from portfolio.stock_lookup import _fetch_tencent  # 复用腾讯解析逻辑

    codes = tuple(codes)
    rows: list[dict] = []
    if not codes:
        return rows
    code_to_secid: dict[str, str] = {}
    for code in codes:
        sid = _secid_of_stock(code)
        if sid:
            code_to_secid[code] = sid

    if code_to_secid:
        try:
            source = EastMoneyLiveSource()
            for secid, item in zip(
                code_to_secid.values(),
                source.get_live_quote(
                    list(code_to_secid.values()),
                    fields="f43,f57,f58,f127,f169,f170",
                ),
            ):
                code = str(item.get("f57") or "")
                if not code:
                    continue
                try:
                    raw = item.get("f43")
                    price = float(raw) / 100.0 if isinstance(raw, int) else float(raw)
                except (TypeError, ValueError):
                    continue
                if price <= 0 or not np.isfinite(price):
                    continue
                rows.append({
                    "security_code": code,
                    "market_price": round(price, 4),
                    "quote_name": str(item.get("f58") or ""),
                    "quote_sector_name": str(item.get("f127") or "") or None,
                    "quote_pct": (float(item.get("f170")) / 100.0 if item.get("f170") is not None else np.nan),
                    "quote_source": "eastmoney_realtime",
                })
        except Exception as e:  # noqa: BLE001
            # 整批主源失败时不影响后续腾讯兜底
            import logging
            logging.getLogger(__name__).warning("东财实时快照整体失败: %s", e)

    # 已拿到价的代码集合
    priced = {r["security_code"] for r in rows}
    # 没拿到价的代码用腾讯实时兜底
    for code in codes:
        if code in priced:
            continue
        info = _fetch_tencent(code)
        if not info or not info.get("price"):
            continue
        rows.append({
            "security_code": code,
            "market_price": round(float(info["price"]), 4),
            "quote_name": info.get("name") or "",
            "quote_sector_name": info.get("sector_name") or None,
            "quote_pct": np.nan,
            "quote_source": "tencent_realtime",
        })
    return rows


_QUOTE_TTL = 300  # 单标的行情缓存时长（秒）


def load_live_quotes_for_portfolio(codes: tuple[str, ...]) -> pd.DataFrame:
    """读取当前持仓实时行情；失败返回空表，不把行情写入持仓账本。

    按 code 做 per-code TTL 缓存（存于 session_state）：删除单个标的只让该
    code 的缓存失效，其余标的命中缓存，不会触发「删一笔 -> 整批行情重拉」的卡顿。
    """
    cols = ["security_code", "market_price", "quote_name", "quote_sector_name", "quote_pct", "quote_source"]
    codes = tuple(str(c) for c in codes)
    if not codes:
        return pd.DataFrame(columns=cols)
    cache = st.session_state.setdefault("_quote_cache", {})
    now = time.time()
    fresh, missing = [], []
    for c in codes:
        ent = cache.get(c)
        if ent and (now - ent["t"]) < _QUOTE_TTL:
            fresh.append(ent["row"])
        else:
            missing.append(c)
    if missing:
        for r in _fetch_quotes_batch(tuple(missing)):
            cache[r["security_code"]] = {"t": now, "row": r}
            fresh.append(r)
    return pd.DataFrame(fresh, columns=cols)


def _substr_lookup(name_index: dict, query: str):
    """name_index 里找不到时，按「互为子串」回退一次。

    让简称/全称变体互通（「旅游」⊂「旅游及酒店」）。
    仅当前面所有精确查都失败时才走这里；只用「互为子串」匹配，**不**做
    前缀匹配——否则「板块三」会误中「板块一」「板块二」。
    """
    if not query:
        return None
    q = str(query).strip()
    if len(q) < 2:
        return None
    # 仅在「互为子串」且 query 非空时返回第一个命中
    for k, v in name_index.items():
        if not k:
            continue
        if q in k or k in q:
            return v
    return None


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
    name_index = {}  # 归一化名 → payload，兼容 "IT服务Ⅱ" vs "IT服务" 等变体
    if states is not None and not states.empty:
        for row in states.to_dict("records"):
            code = str(row.get("sector_code") or "")
            name = str(row.get("sector_name") or "")
            payload = {"state": row.get("state"), "trend": row.get("trend"), "date": str(row.get("date", ""))[:10]}
            if code:
                state_map["code:" + code] = payload
            if name:
                state_map["name:" + name] = payload
                # 归一化索引：去掉 Ⅱ / 及 / 行业 / 板块 等连接后缀，简称/全称互通
                norm = re.sub(r"[ⅡI()（）]|及|行业|产业|板块|概念|指数|\s", "", name)
                if norm:
                    name_index[norm] = payload
                # 同时存原始名作为子串索引：让 "旅游" 这种简称能命中 "旅游及酒店" 全名
                if name and name not in name_index:
                    name_index[name] = payload

    risk_states = {"①领涨减速", "③加速冲顶", "④强转弱", "⑦持续杀跌", "⑧下跌中继"}
    weak_states = {"④强转弱", "⑦持续杀跌", "⑧下跌中继"}
    action_rank = {"尽快决策": 0, "优先复核": 1, "左侧观察": 2, "持有观望": 3, "数据不足": 4}
    rows = []
    for row in df.to_dict("records"):
        sector_code = str(row.get("sector_code") or "")
        sector_name = str(row.get("sector_name") or row.get("quote_sector_name") or "")
        # 存量录入时若未带出行业、且本次实时批拉又没带出（多 secid 批量偶发丢行），
        # 按 code 单独补一次，确保不会永远停在「数据不足」。
        if not sector_name:
            try:
                info = lookup_stock_info(str(row.get("security_code") or ""))
                sector_name = str(info.get("sector_name") or "") if info else ""
            except Exception:
                sector_name = ""
        # 存量记录若只存了行业名（如「旅游」）没存代码，反查 881xxx 再匹配状态机，
        # 这样已录入的标的也能正确关联到板块状态，而不是显示「数据不足」。
        if not sector_code and sector_name:
            sector_code = resolve_sector(sector_name)[0] or ""
        # 三级兜底：code → name 原样 → name 归一化（兼容 Ⅱ / 及 / 行业 等变体）
        state_info = (
            state_map.get("code:" + sector_code)
            or state_map.get("name:" + sector_name)
            or name_index.get(re.sub(r"[ⅡI()（）]|及|行业|产业|板块|概念|指数|\s", "", sector_name))
            or name_index.get(sector_name)
            or _substr_lookup(name_index, sector_name)
        )
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
            "sector_name": sector_name,
            "sector_code": sector_code or row.get("sector_code") or "",
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
                # 行情带出的行业名（如「旅游」）自动解析成同花顺 881xxx 代码，
                # 写入 session_state，自动填入「行业代码」框（用户可覆盖）。
                resolved = resolve_sector(info.get("sector_name")) if info.get("sector_name") else (None, "", "其他")
                st.session_state["pl_sector_code"] = resolved[0] or ""
                st.session_state["pl_sector_group"] = resolved[2] or ""
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
                code = st.session_state.get("pl_sector_code") or ""
                grp = st.session_state.get("pl_sector_group") or ""
                tip += f" ｜ 行业 {info['sector_name']}"
                if code:
                    tip += f" ｜ 代码 {code}" + (f"（{grp}）" if grp else "")
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
                estimate = estimate_trade_fee(security_code, side, quantity, price)
                fee = st.number_input(
                    "费用（自动估算，可修改）",
                    min_value=0.0,
                    value=float(estimate.total),
                    step=0.01,
                    help=f"估算明细：佣金 ¥{estimate.commission:.2f} + 印花税 ¥{estimate.stamp_tax:.2f} + 过户费 ¥{estimate.transfer_fee:.2f}。实际以券商交割单为准。",
                )
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
                    sector_code = st.text_input(
                        "行业代码", value=st.session_state.get("pl_sector_code", ""),
                        placeholder="如 881160（查询行情后自动带出，可改）",
                    )
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
                # 行情带出的行业名可能是简称（如「旅游」）。未手填行业代码时，用它反查
                # 881xxx 代码；行业名保留行情原值以便展示，关联靠代码命中状态机。
                auto_code = None
                if not sector_code.strip() and sector_name:
                    auto_code = resolve_sector(sector_name)[0]
                service.record_trade(
                    security_code=code,
                    security_name=name,
                    side=side,
                    quantity=quantity,
                    price=price,
                    trade_date=trade_date.isoformat(),
                    fee=fee,
                    sector_code=(sector_code.strip() or auto_code) or None,
                    sector_name=sector_name or None,
                    asset_type=(auto or {}).get("asset_type") or "stock",
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


def _is_blank_name(v) -> bool:
    """None/NaN/空串 都算「没有行业名」。"""
    if v is None:
        return True
    s = str(v).strip()
    return s == "" or s.lower() in ("none", "nan")


def _sector_name_fallback(code: str) -> str:
    """按代码单独补查行业名（session 级缓存，避免每次渲染重复打网）。"""
    try:
        cache = st.session_state.setdefault("_sector_name_lookup_cache", {})
    except Exception:  # noqa: BLE001 — 无 Streamlit 运行上下文（测试/后台线程）时降级为无缓存
        cache = {}
    if code in cache:
        return str(cache[code] or "")
    try:
        info = lookup_stock_info(code)
        name = str(info.get("sector_name") or "") if info else ""
    except Exception:
        name = ""
    cache[code] = name
    return name


def fill_sector_display(display: pd.DataFrame, service=None) -> pd.DataFrame:
    """顶部持仓表的行业兜底，与下方分析表 `_build_position_analysis` 同源三级链：

    DB sector_name → 实时行情 quote_sector_name → 按 code 单独补查。
    存量录入未带出行业的标的（如 603887）不再在持仓表显示 None。

    若传入 `service`，兜底补出的行业会**立即写回账本**（仅真实补出的行写一次），
    这样实时行情偶发失败时也不丢失行业显示。
    直接修改传入的 display（调用方持有副本，不影响传入的原始账本快照）。
    """
    if display.empty or "sector_name" not in display.columns:
        return display
    blank = display["sector_name"].map(_is_blank_name)
    if blank.any() and "quote_sector_name" in display.columns:
        fill_mask = blank & display["quote_sector_name"].map(lambda v: not _is_blank_name(v))
        display.loc[fill_mask, "sector_name"] = display.loc[fill_mask, "quote_sector_name"]
        if service is not None:
            for idx in display.index[fill_mask]:
                _name = str(display.at[idx, "sector_name"] or "")
                if _name:
                    _persist_sector(service, str(display.at[idx, "security_code"]), _name)
        blank = display["sector_name"].map(_is_blank_name)
    for idx in display.index[blank]:
        name = _sector_name_fallback(str(display.at[idx, "security_code"])) or None
        if name:
            display.at[idx, "sector_name"] = name
            if service is not None:
                _persist_sector(service, str(display.at[idx, "security_code"]), str(name))
    return display


def _persist_sector(service, code: str, name: str) -> None:
    """把补出的行业名 + 离线解析出的行业代码（881xxx）写回持仓账本；失败静默。

    行业代码用本地同花顺映射 resolve_sector(行业名) 推导，无需联网，
    与 advisor 复盘所需的 sector_code 对齐，避免「有行业名却仍提示补全行业代码」。
    """
    updates = {"sector_name": name}
    try:
        res = resolve_sector(name)
        if res and res[0]:
            updates["sector_code"] = res[0]
    except Exception:
        pass
    try:
        service.update_metadata(code, **updates)
    except Exception:
        pass


def autofill_missing_sectors(user_id: str, max_lookup: int = 20) -> int:
    """App 启动自检：扫描当前用户全部持仓，行业名/行业代码缺失的离线优先补全并写回账本。

    统一复用 ``check_and_fill_position``（与录入/导入同一套逻辑），保证三入口行为
    一致且均有可见留痕。离线优先、绝不联网（online_fallback=False）——云端沙箱常断
    网，离线包已覆盖全 A 股，绝大多数可在此命中；联网兜底留给交互式录入/导入。
    每次检查/补全落库 sector_fill_log，页面「行业补全检查留痕」据此可见。
    永不抛出、绝不阻断 App 启动。
    """
    try:
        service = PortfolioHoldings(user_id=str(user_id))
        positions = service.positions()
    except Exception:  # noqa: BLE001 — 自检失败绝不阻断 App 启动
        return 0
    if positions is None or positions.empty or "security_code" not in positions.columns:
        return 0
    filled = 0
    for _, r in positions.head(max_lookup).iterrows():
        code = str(r.get("security_code") or "").strip()
        if not code:
            continue
        try:
            trace = check_and_fill_position(
                service, code, security_name=r.get("security_name"),
                source_tag="登录自检", online_fallback=False,
            )
            if trace.get("filled"):
                filled += 1
        except Exception:  # noqa: BLE001
            pass
    return filled


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
    fill_sector_display(display, service)
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
    with st.expander("✏️ 修改标的元数据（不新增交易）", expanded=False):
        edit_code = st.selectbox("选择标的", positions["security_code"].astype(str).tolist(), format_func=lambda c: f"{c} ｜ {positions.loc[positions['security_code'].astype(str) == c, 'security_name'].iloc[0]}")
        current = positions[positions["security_code"].astype(str) == str(edit_code)].iloc[0]
        st.caption("这里只能改类型、行业、板块代码、备注等元数据；持仓数量和成本由下方逐笔交易自动聚合，不在此处修改。")
        with st.form("portfolio_edit_metadata"):
            edit_type = st.selectbox("类型", ["stock", "etf", "fund"], index=["stock", "etf", "fund"].index(str(current.get("asset_type") or "stock")))
            e4, e5 = st.columns(2)
            with e4:
                edit_sector = st.text_input("所属行业/板块", value=str(current.get("sector_name") or ""))
            with e5:
                edit_sector_code = st.text_input("板块代码（可选）", value=str(current.get("sector_code") or ""))
            edit_note = st.text_input("备注", value=str(current.get("note") or ""))
            edit_submitted = st.form_submit_button("保存修改", type="primary", width="stretch")
        if edit_submitted:
            try:
                service.update_metadata(
                    str(edit_code), asset_type=edit_type, sector_name=edit_sector.strip() or None,
                    sector_code=edit_sector_code.strip() or None, note=edit_note.strip() or None,
                )
                st.success(f"已修改 {edit_code} 的元数据，不会新增交易记录。")
                st.rerun()
            except Exception as exc:
                st.error(f"修改失败：{exc}")
    return positions, quotes


def _render_position_analysis(service: PortfolioHoldings, positions: pd.DataFrame, quotes: pd.DataFrame):
    st.subheader("持仓分析与决策优先级")
    render_src_badge("derived", base=["user", "eastmoney_realtime", "sector_state"])
    if positions.empty:
        st.info("录入持仓后，这里会按每只股票的板块状态、浮盈亏和止损条件生成左侧交易建议。")
        return pd.DataFrame()
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
    view = analysis[["priority", "security_code", "security_name", "sector_name", "sector_code", "sector_state", "market_price", "profit_pct", "action", "reason"]].copy()
    view = view.rename(columns={
        "priority": "优先级", "security_code": "代码", "security_name": "名称", "sector_name": "所属行业",
        "sector_code": "行业代码", "sector_state": "板块状态", "market_price": "现价", "profit_pct": "收益率", "action": "建议动作", "reason": "分析依据",
    })
    view["现价"] = view["现价"].map(lambda x: f"¥{x:,.2f}" if pd.notna(x) else "暂无")
    view["收益率"] = view["收益率"].map(lambda x: f"{x:+.2f}%" if pd.notna(x) else "暂无")
    st.dataframe(view, hide_index=True, width="stretch", column_config={
        "分析依据": st.column_config.TextColumn(width="large"),
        "建议动作": st.column_config.TextColumn(width="medium"),
    })
    return analysis


# ================================================================
# 持仓行业九宫格分布 + 状态变化轨迹
# ================================================================
@st.cache_data(ttl=3600, show_spinner=False)
def load_state_series_cached(sector_code: str) -> pd.DataFrame:
    """按行业代码缓存历史状态序列，避免每次 rerun 重复读 RS/趋势 parquet。"""
    try:
        series = StateMachine(ParquetStore(), SQLiteStore()).calc_state_series(sector_code)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    if series is None or series.empty:
        return pd.DataFrame()
    return series[["date", "state"]].copy()


def _grid_offsets(k: int) -> list[tuple]:
    """同一格内 k 个点的确定性错位偏移（环形排布）。

    不用随机偏移：随机会让点在每次 rerun 后跳位置，无法对照上一次读图。
    """
    if k <= 1:
        return [(0.0, 0.0)]
    import math
    radius = 0.16 if k <= 4 else 0.24
    return [
        (radius * math.cos(2 * math.pi * i / k), radius * math.sin(2 * math.pi * i / k))
        for i in range(k)
    ]


def _hex_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _render_portfolio_grid(analysis: pd.DataFrame):
    """把持仓标的按其所属行业的九宫格状态，画在 3×3 网格上。"""
    import plotly.graph_objects as go  # 懒加载:plotly ~10MB,仅在使用此函数时加载
    st.subheader("持仓行业九宫格分布")
    render_src_badge("derived", base=["user", "sector_state"])
    if analysis is None or analysis.empty:
        st.info("录入持仓后，这里会显示每只标的所属行业落在九宫格的哪个位置。")
        return

    df = analysis.copy()
    df["_cell"] = df["sector_state"].map(lambda s: STATE_CELL.get(str(s)))
    placed = df[df["_cell"].notna()].copy()
    missing = df[df["_cell"].isna()]

    if placed.empty:
        st.warning(
            f"当前 {len(df)} 只持仓都还没关联到行业状态，无法定位九宫格。"
            "可在上方「修改标的元数据」补一个 881xxx 板块代码，或等数据管线补齐行业状态。"
        )
        return

    placed["gx"] = placed["_cell"].map(lambda c: c[0])
    placed["gy"] = placed["_cell"].map(lambda c: c[1])

    fig = go.Figure()

    # 背景格子 + 格子标题
    cell_stat = {}
    for (gx, gy), grp in placed.groupby(["gx", "gy"]):
        mv = pd.to_numeric(grp.get("market_value"), errors="coerce").sum()
        cell_stat[(gx, gy)] = (len(grp), float(mv) if pd.notna(mv) else 0.0)

    for state, (x, y) in STATE_CELL.items():
        color = STATE_COLORS.get(state, "#9E9E9E")
        cnt, mv = cell_stat.get((x, y), (0, 0.0))
        # 有持仓的格子底色更明显，空格子淡化，一眼看出仓位聚在哪
        fig.add_shape(
            type="rect", x0=x - 0.5, x1=x + 0.5, y0=y - 0.5, y1=y + 0.5,
            line={"color": color if cnt else "#DDDDDD", "width": 2 if cnt else 0.6},
            fillcolor=_hex_rgba(color, 0.16 if cnt else 0.04),
            layer="below",
        )
        fig.add_annotation(
            x=x, y=y + 0.42, text=f"<b>{state}</b>", showarrow=False,
            font={"size": 12, "color": color},
        )
        label = f"{cnt} 只 · ¥{mv:,.0f}" if cnt else "无持仓"
        fig.add_annotation(
            x=x, y=y - 0.42,
            text=f"<span style='color:{'#333' if cnt else '#BBB'};font-size:11px'>{label}</span>",
            showarrow=False,
        )

    # 持仓散点：颜色随盈亏（A股习惯 红涨绿跌），大小随市值
    xs, ys, texts, colors, sizes, custom = [], [], [], [], [], []
    mv_series = pd.to_numeric(placed.get("market_value"), errors="coerce")
    mv_max = float(mv_series.max()) if mv_series.notna().any() else 0.0
    for (gx, gy), grp in placed.groupby(["gx", "gy"]):
        offs = _grid_offsets(len(grp))
        for (dx, dy), (_, row) in zip(offs, grp.iterrows()):
            xs.append(gx + dx)
            ys.append(gy + dy)
            texts.append(str(row.get("security_name") or row.get("security_code") or "")[:6])
            pct = pd.to_numeric(pd.Series([row.get("profit_pct")]), errors="coerce").iloc[0]
            if pd.isna(pct):
                colors.append("#9E9E9E")
            else:
                colors.append("#e23c3c" if pct >= 0 else "#16a34a")
            mv = pd.to_numeric(pd.Series([row.get("market_value")]), errors="coerce").iloc[0]
            if pd.isna(mv) or mv_max <= 0:
                sizes.append(14)
            else:
                sizes.append(12 + 20 * (float(mv) / mv_max) ** 0.5)
            custom.append([
                str(row.get("security_code") or ""),
                str(row.get("security_name") or ""),
                str(row.get("sector_name") or "—"),
                str(row.get("sector_code") or "—"),
                str(row.get("sector_state") or "—"),
                f"¥{float(mv):,.0f}" if pd.notna(mv) else "暂无",
                f"{float(pct):+.2f}%" if pd.notna(pct) else "暂无",
            ])

    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="markers+text",
        text=texts, textposition="bottom center",
        textfont={"size": 10, "color": "#444"},
        marker={"size": sizes, "color": colors, "opacity": 0.85,
                "line": {"width": 1.5, "color": "white"}},
        customdata=custom,
        hovertemplate=(
            "<b>%{customdata[1]}</b>（%{customdata[0]}）<br>"
            "行业：%{customdata[2]}　%{customdata[3]}<br>"
            "行业状态：%{customdata[4]}<br>"
            "市值：%{customdata[5]}　收益率：%{customdata[6]}"
            "<extra></extra>"
        ),
        showlegend=False,
    ))

    fig.update_layout(
        xaxis={"tickmode": "array", "tickvals": [0, 1, 2], "ticktext": X_LABELS,
               "title": "RS 相对强弱动量方向 →", "range": [-0.75, 2.75],
               "zeroline": False, "showgrid": False},
        yaxis={"tickmode": "array", "tickvals": [0, 1, 2], "ticktext": Y_LABELS,
               "title": "价格趋势 ↑", "range": [-0.75, 2.75],
               "zeroline": False, "showgrid": False},
        height=560, margin={"l": 60, "r": 20, "t": 20, "b": 60},
        plot_bgcolor="white", paper_bgcolor="white",
        hovermode="closest",
    )
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "点＝一只持仓标的，按其**所属行业**的九宫格状态定位（同格内环形错开，位置固定不随刷新跳动）。"
        "点的颜色：🔴 浮盈 / 🟢 浮亏；点的大小：市值占比。"
        "格子定位直接取自状态标签，不用原始 RS 分位反推——状态机含横截面闸门与斜率门槛的降级逻辑，"
        "按分位反推会让点落错格子。"
    )

    if not missing.empty:
        names = "、".join(
            f"{r.get('security_name') or r.get('security_code')}" for _, r in missing.iterrows()
        )
        st.warning(
            f"⚠️ {len(missing)} 只标的未能关联行业状态，未在图中显示：{names}。"
            "可在上方「✏️ 修改标的元数据」里补填板块代码（881xxx）。"
        )


def _render_state_transitions(analysis: pd.DataFrame, n_changes: int = 3):
    """展示每个持仓行业最近 n 次九宫格状态变化：变化日期 + 每段持续时间。"""
    st.subheader(f"持仓行业状态变化轨迹（近 {n_changes} 次）")
    render_src_badge("derived", base=["ths_kline", "sector_state"])
    if analysis is None or analysis.empty:
        st.info("录入持仓后，这里会展示每个持仓行业最近几次九宫格状态切换的日期与持续时间。")
        return

    # 状态是行业级的：按行业去重，避免同行业多只标的重复计算
    df = analysis.copy()
    df["sector_code"] = df["sector_code"].astype(str)
    sectors = {}
    for row in df.to_dict("records"):
        code = str(row.get("sector_code") or "").strip()
        if not code:
            continue
        entry = sectors.setdefault(code, {
            "sector_name": str(row.get("sector_name") or "") or code,
            "holdings": [],
        })
        entry["holdings"].append(str(row.get("security_name") or row.get("security_code") or ""))

    if not sectors:
        st.warning("当前持仓都没有可用的行业代码，无法追溯状态变化。请先在「修改标的元数据」补填 881xxx 板块代码。")
        return

    summary_rows, runs_map, detail_rows = [], {}, []
    for code, info in sectors.items():
        series = load_state_series_cached(code)
        runs = recent_state_runs(series, n_changes=n_changes)
        if not runs:
            summary_rows.append({
                "行业": info["sector_name"], "行业代码": code,
                "持有标的": "、".join(info["holdings"]),
                "当前状态": "—", "已持续": "—", "最近变化日": "—",
                f"近 {n_changes} 次状态变化": "暂无历史状态数据",
            })
            continue
        runs_map[code] = (info, runs)
        cur = runs[-1]
        changed_on = runs[-1]["start_date"] if len(runs) > 1 else "—"
        summary_rows.append({
            "行业": info["sector_name"], "行业代码": code,
            "持有标的": "、".join(info["holdings"]),
            "当前状态": cur["state"],
            "已持续": f"{'≥' if cur.get('truncated_start') else ''}{cur['trading_days']} 个交易日",
            "最近变化日": changed_on,
            f"近 {n_changes} 次状态变化": format_runs_path(runs),
        })
        for r in runs:
            detail_rows.append({
                "行业": info["sector_name"], "行业代码": code, "状态": r["state"],
                "进入日期": ("（数据起点）" if r.get("truncated_start") else "") + r["start_date"],
                "结束日期": "至今" if r["is_current"] else r["end_date"],
                "持续交易日": r["trading_days"],
                "持续自然日": r["calendar_days"],
                "是否当前": "✅ 当前" if r["is_current"] else "",
            })

    st.dataframe(
        pd.DataFrame(summary_rows), hide_index=True, width="stretch",
        column_config={
            f"近 {n_changes} 次状态变化": st.column_config.TextColumn(width="large"),
            "持有标的": st.column_config.TextColumn(width="medium"),
        },
    )
    st.caption(
        "「已持续」按交易日计。带 ≥ 表示该状态在可用历史数据的起点就已存在，实际开始更早。"
        "状态为行业级信号，同一行业下的多只标的共用一条轨迹。"
    )

    if runs_map:
        _render_transition_timeline(runs_map)
        with st.expander("📋 状态变化明细（每段的进入/结束日期与持续时间）", expanded=False):
            st.dataframe(pd.DataFrame(detail_rows), hide_index=True, width="stretch")


def _render_transition_timeline(runs_map: dict):
    """状态变化甘特时间线：每行一个行业，色块=一个状态段。"""
    import plotly.graph_objects as go  # 懒加载:plotly ~10MB,仅在使用此函数时加载
    fig = go.Figure()
    y_labels, seen_states = [], set()
    for code, (info, runs) in runs_map.items():
        label = f"{info['sector_name']}({code})"
        y_labels.append(label)
        for r in runs:
            start = pd.Timestamp(r["start_date"])
            # 色块延伸到结束日的次日 0 点，视觉上覆盖完整的最后一天
            end = pd.Timestamp(r["end_date"]) + pd.Timedelta(days=1)
            width_ms = (end - start).total_seconds() * 1000.0
            color = STATE_COLORS.get(r["state"], "#9E9E9E")
            show_legend = r["state"] not in seen_states
            seen_states.add(r["state"])
            fig.add_trace(go.Bar(
                y=[label], x=[width_ms], base=[start], orientation="h",
                marker={"color": color, "line": {"color": "white", "width": 1}},
                name=r["state"], legendgroup=r["state"], showlegend=show_legend,
                hovertemplate=(
                    f"<b>{info['sector_name']}</b><br>"
                    f"状态：{r['state']}<br>"
                    f"进入：{r['start_date']}<br>"
                    f"结束：{'至今' if r['is_current'] else r['end_date']}<br>"
                    f"持续：{r['trading_days']} 个交易日（{r['calendar_days']} 自然日）"
                    "<extra></extra>"
                ),
            ))

    fig.update_layout(
        barmode="stack",
        height=max(220, 46 * len(y_labels) + 130),
        margin={"l": 10, "r": 20, "t": 10, "b": 40},
        xaxis={"type": "date", "title": "日期", "showgrid": True, "gridcolor": "#EEEEEE"},
        yaxis={"autorange": "reversed", "title": ""},
        plot_bgcolor="white", paper_bgcolor="white",
        legend={"orientation": "h", "yanchor": "bottom", "y": -0.35,
                "xanchor": "center", "x": 0.5},
        bargap=0.35,
    )
    st.plotly_chart(fig, width="stretch")


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


def _set_editing_tx(tid: int):
    """点「编辑」时记录正在编辑的流水 id，仅展开那一笔的完整表单。"""
    st.session_state["editing_tx_id"] = tid


def _render_transactions(service: PortfolioHoldings):
    st.subheader("逐笔操作记录")
    render_src_badge("derived", base=["user"])
    st.caption("同一标的的多次操作会自动聚合；修改或删除流水后，当前持仓会重新计算。点「编辑」才展开该笔表单，减少页面组件数，删除/修改更顺滑。")
    transactions = service.transactions()
    if transactions.empty:
        st.caption("暂无实际操作记录。")
        return
    editing_id = st.session_state.get("editing_tx_id")
    for code, group in transactions.groupby("security_code", sort=False):
        with st.expander(f"{code} · {group.iloc[0]['security_name']}（{len(group)} 笔）"):
            for _, row in group.iterrows():
                tid = int(row["id"])
                st.write(f"{row['trade_date']}｜{'买入' if row['side']=='BUY' else '卖出' if row['side']=='SELL' else '调账'}｜数量 {row['quantity']:g}｜价格 ¥{row['price']:.4f}")
                c1, c2 = st.columns(2)
                with c1:
                    st.button("编辑", key=f"edit_tx_{tid}", on_click=_set_editing_tx, args=(tid,))
                with c2:
                    if st.button("删除这笔记录", key=f"delete_tx_{tid}"):
                        try:
                            service.delete_transaction(tid)
                            if st.session_state.get("editing_tx_id") == tid:
                                st.session_state.pop("editing_tx_id", None)
                            st.success("已删除并重新聚合")
                            # 删除在 fragment 内执行，fragment 重跑即刷新本区与上方持仓表，不整页重跑
                        except Exception as exc:
                            st.error(f"删除失败：{exc}")
                # 仅当前正在编辑的那一笔展开完整表单，其余只保留按钮（大幅减少组件数）
                if editing_id == tid:
                    with st.form(f"edit_tx_{tid}"):
                        nd = st.date_input("日期", value=pd.to_datetime(row["trade_date"]).date())
                        ns = st.selectbox("操作", ["BUY", "SELL", "ADJUST"], index=["BUY", "SELL", "ADJUST"].index(row["side"]), format_func={"BUY":"买入", "SELL":"卖出", "ADJUST":"调账"}.get)
                        nq = st.number_input("数量", min_value=0.0001, value=float(row["quantity"]), format="%.4f")
                        np = st.number_input("价格", min_value=0.0, value=float(row["price"]), format="%.4f")
                        nf = st.number_input("费用", min_value=0.0, value=float(row["fee"]), format="%.2f")
                        nn = st.text_area("备注", value=row["note"] or "")
                        if st.form_submit_button("保存修改", type="primary"):
                            try:
                                service.update_transaction(tid, trade_date=nd.isoformat(), side=ns, quantity=nq, price=np, fee=nf, note=nn or None)
                                st.session_state.pop("editing_tx_id", None)
                                st.success("已修改并重新聚合")
                                # fragment 重跑即刷新
                            except Exception as exc:
                                st.error(f"修改失败：{exc}")


@st.fragment
def _frag_holdings(service: PortfolioHoldings):
    """持仓表 + 分析 + 待办 + 逐笔记录作为独立 fragment。

    删除/修改交易只重跑本 fragment（不整页、不重建录入表单、不重拉整批行情），
    解决了「删一笔卡很久」的问题。
    """
    positions, quotes = _render_positions(service)
    st.markdown("---")
    analysis = _render_position_analysis(service, positions, quotes)
    st.markdown("---")
    _render_portfolio_grid(analysis)
    st.markdown("---")
    _render_state_transitions(analysis)
    st.markdown("---")
    _render_pending_items(service)
    st.markdown("---")
    _render_transactions(service)
    st.markdown("---")
    # 手动触发一次全持仓行业检查（登录自检已自动跑，这里让用户随时可复核并看到留痕）
    if st.button("🔍 重新检查全部持仓的行业补全", key="recheck_sectors"):
        _filled = autofill_missing_sectors(getattr(service, "user_id", "") or "")
        if _filled:
            st.toast(f"已补全 {_filled} 个持仓的行业信息")
        else:
            st.toast("全部持仓行业已齐全，无需补全")
        st.rerun()
    _render_sector_fill_report(service)


def _render_sector_fill_report(service: PortfolioHoldings):
    """展示行业补全检查留痕（用户明确要求的「检查留痕」可见化）。

    读取 sector_fill_log：每次登录 / 导入 / 添加记录都会落库一条，这里汇总计数并
    列出明细，让用户清楚看到「哪些标的检查过、补了什么、哪些补不到」。
    """
    st.subheader("📋 行业补全检查留痕")
    render_src_badge("derived", base=["user"])
    user_id = getattr(service, "user_id", "") or ""
    try:
        logs = service.store.get_sector_fill_logs(user_id=user_id, limit=500)
    except Exception:  # noqa: BLE001
        logs = pd.DataFrame()
    if logs is None or logs.empty:
        st.info("暂无行业补全检查记录。任何一次登录 / 导入 / 添加记录都会在这里留下检查留痕。")
        return

    # 汇总计数
    traces = logs.to_dict("records")
    summ = summarize_traces(traces)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("检查次数", summ["total"])
    c2.metric("补全成功", summ["filled"])
    c3.metric("原本齐全", summ["already"])
    c4.metric("未能补全", summ["failed"])

    # 明细表（补全前 → 补全后）
    view = logs.copy()
    view["时间"] = view["ts"].map(lambda v: str(v)[:19] if v is not None else "")
    view["代码"] = view["security_code"]
    view["名称"] = view["security_name"].map(lambda v: str(v or ""))
    view["补全前"] = view.apply(
        lambda r: f"{r.get('before_name') or '—'}/{r.get('before_code') or '—'}", axis=1)
    view["补全后"] = view.apply(
        lambda r: f"{r.get('after_name') or '—'}/{r.get('after_code') or '—'}", axis=1)
    view["动作"] = view["action"]
    view["来源"] = view["source"]
    view = view[["时间", "代码", "名称", "补全前", "补全后", "动作", "来源"]]
    st.dataframe(view, hide_index=True, width="stretch",
                 column_config={"补全前": st.column_config.TextColumn(width="medium"),
                                "补全后": st.column_config.TextColumn(width="medium")})
    st.caption("「补全前/补全后」格式为 行业名/行业代码。未能补全的标的通常是离线包未覆盖的小众代码，"
               "可在「修改标的元数据」手动补一个 881xxx 板块代码。")


def render():
    """持仓管理主入口。"""
    import plotly.graph_objects as go  # 懒加载:plotly ~10MB,只在打开本页时加载
    st.title("💼 持仓管理")
    render_version_indicator()
    st.caption("管理你真实持有的标的与实际操作；通用行业信号仍以板块轮动监控中的状态机展示为准。")
    # 多用户：从登录会话取当前用户，确保只看自己的仓位
    user_id = st.session_state.get("username", "")
    if not user_id:
        st.warning("未识别到登录用户，无法加载持仓。请重新登录后重试。")
        return
    service = get_holdings_service(user_id=user_id)
    st.info(f"👤 当前账户：**{user_id}**（仅显示你自己的持仓）")
    _frag_holdings(service)
    st.markdown("---")
    _render_record_form(service)
    st.markdown("---")
    _render_import_panel(service)


def _annotate_import_duplicates(records: list[dict], service: PortfolioHoldings) -> int:
    """标记预览记录中与已登记交易重复的条目（duplicate=True 并追加提示），返回重复笔数。

    去重键由 portfolio.trade_import.trade_dedup_key 统一计算，与确认写入时完全一致。
    """
    existing = service.store.get_transactions_for_dedup(service.user_id)
    existing_keys = {trade_dedup_key(d, c, s, q, p) for (d, c, s, q, p) in existing}
    dup = 0
    for rec in records:
        key = trade_dedup_key(
            rec.get("trade_date"), rec.get("security_code"), rec.get("side"),
            rec.get("quantity"), rec.get("price"),
        )
        if key in existing_keys:
            rec["duplicate"] = True
            rec["warn"] = (rec.get("warn") or "") + "；与已登记记录重复，确认导入时将自动跳过"
            dup += 1
    return dup


def _auto_parse_import(service: PortfolioHoldings) -> None:
    """上传文件 / 粘贴文本变更时自动解析，结果写入 session_state['import_preview']。

    用于替代独立的「解析」按钮——用户只要选了文件或粘贴了内容，就立刻解析出预览，
    下方只保留一个「确认导入」按钮即可完成导入。

    注意：本函数在 on_change 回调里被调用，回调内**不要直接调用 st.warning 等展示
    命令**（部分云端 Streamlit 版本在回调里渲染元素会异常导致整段不显示）。所有提示
    统一写入 session_state['import_parse_msgs']，由渲染阶段统一展示。
    """
    msgs: list[str] = []
    uploaded = st.session_state.get("import_file")
    pasted = (st.session_state.get("import_paste") or "").strip()
    file_bytes = uploaded.getvalue() if uploaded is not None else None
    filename = getattr(uploaded, "name", None)
    if not file_bytes and not pasted:
        # 文件被清空且无文本 → 顺手清掉旧预览，避免残留上一次的结果
        st.session_state.pop("import_preview", None)
        st.session_state.pop("import_parse_msgs", None)
        return
    try:
        trades, errors = parse_trades(
            raw_text=pasted or None,
            file_bytes=file_bytes,
            filename=filename,
        )
        msgs.extend(errors)
        if trades:
            # 先写入预览，确保表格一定显示；标重复这一步即使失败也不阻塞预览
            records = trades_to_records(trades)
            st.session_state["import_preview"] = records
            # 每次解析换新的 editor key：避免 session 残留旧编辑状态与新数据形状冲突
            st.session_state["import_editor_key"] = f"import_editor_{time.time_ns()}"
            try:
                _annotate_import_duplicates(records, service)
            except Exception as ann_err:  # 标重复失败不应让表格消失
                msgs.append(f"重复标记跳过（不影响导入）：{ann_err}")
        else:
            msgs.append("没有解析出任何交易记录（请检查内容或表头）")
            st.session_state.pop("import_preview", None)
    except Exception as exc:  # 任何异常都记录下来，渲染阶段显式提示，避免“静默无反应”
        msgs.append(f"解析出错：{exc}")
        st.session_state.pop("import_preview", None)
    st.session_state["import_parse_msgs"] = msgs


def _render_import_preview_safe(service: PortfolioHoldings) -> None:
    """渲染导入预览：兜底解析 + 显眼状态 + 多级降级渲染，保证表格一定出现。

    修复「上传后表格一直不出现」：
      1) 若文件/文本已存在但预览缺失（on_change 未触发或被吞），渲染时再解析一次；
      2) 解析有结果 → 成功提示 + 表格；解析失败/空 → 显眼的失败提示与排查清单；
      3) 表格渲染逐级降级（dataframe → table → write），绝不让云端渲染差异把表格吞掉。
    """
    uploaded = st.session_state.get("import_file")
    pasted = (st.session_state.get("import_paste") or "").strip()
    has_input = (uploaded is not None) or bool(pasted)

    # 兜底：有输入但还没解析出来 → 渲染时再解析一次（解决 on_change 不触发导致不显示）
    if has_input and not st.session_state.get("import_preview"):
        try:
            _auto_parse_import(service)
        except Exception:  # 兜底解析自身失败不应让面板崩溃
            pass

    # 先把解析过程中的提示显眼地展示出来
    for m in (st.session_state.get("import_parse_msgs") or []):
        st.warning(m)

    preview = st.session_state.get("import_preview")
    if not preview:
        if has_input:
            st.error(
                "⚠️ 未能从上传的文件 / 粘贴文本中解析出任何交易记录。请排查：\n"
                "① 文件是否为券商导出的 CSV / Excel 交割单；\n"
                "② 首行是否含表头：日期,代码,名称,方向,数量,价格,费用；\n"
                "③ 若 CSV 仍解析不出，请改用下方「粘贴文本」方式，或把文件发我帮你转成标准格式。"
            )
        else:
            st.caption("👆 上传文件或粘贴文本后，会自动解析并在这里显示预览表格；核对无误后点「确认导入」即可。")
        return

    _n = len(preview)
    _dup = sum(1 for r in preview if r.get("duplicate"))
    st.success(
        f"✅ 已自动解析 {_n} 笔"
        + (f"，其中 {_dup} 笔与已登记记录重复（确认导入时会自动跳过）" if _dup else "")
        + "，请核对下方表格，然后点「确认导入」。"
    )

    df = pd.DataFrame(preview)
    # 防御清洗：异常文件可能解析出超长/混合类型文本，会把前端表格压垮，统一规整
    for c in ("trade_date", "security_code", "security_name", "side", "warn"):
        if c in df.columns:
            df[c] = df[c].map(
                lambda v: str(v).strip()[:80]
                if v is not None and str(v).strip().lower() != "nan" else ""
            )
    for c in ("quantity", "price", "fee"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    for c in ("duplicate", "ok"):
        if c in df.columns:
            df[c] = df[c].map(bool)
    view_cols = [c for c in ["trade_date", "security_code", "security_name", "side",
                             "quantity", "price", "fee", "duplicate", "warn"]
                 if c in df.columns]
    df = df[view_cols]

    # 多级降级渲染：保证表格一定出现，不被云端渲染差异吞掉
    try:
        st.dataframe(df, use_container_width=True, hide_index=True)
    except Exception:
        try:
            st.table(df)
        except Exception:
            st.write(df)
    st.caption("💡 表格里是要导入的内容；「重复」列打勾的行，确认导入时会自动跳过（去重）。")

    if st.button("✅ 确认导入", type="primary", key="import_confirm"):
        _import_confirm(service, df)


def _render_import_panel(service: PortfolioHoldings):
    """批量导入逐笔交易：粘贴 / 上传 CSV·Excel → 解析 → 可编辑预览 → 确认写入。

    设计要点：
      - 券商 App 交易截图无法在云端直接 OCR，因此截图走「对话发图 → 我识别成标准
        CSV → 用户粘贴到文本框」的协作路径；本面板负责把结构化数据落到云端账本。
      - 解析器（portfolio.trade_import）对中文表头、数值/代码/方向/日期做容错。
      - 预览用 st.data_editor 可编辑，确认时逐条调用 service.record_trade，
        单条失败不影响其余，并汇总成功/失败明细。
    """
    st.subheader("📥 批量导入逐笔交易")
    render_src_badge("derived", base=["user"])
    st.caption("支持粘贴文本 / 上传 CSV / Excel（券商 App 导出的交割单）。表头列名会自动识别。")

    with st.expander("📥 从交割单导入（粘贴 / CSV / Excel）", expanded=False):
        st.file_uploader(
            "上传 CSV / Excel 交割单", type=["csv", "xlsx", "xls"],
            key="import_file", on_change=lambda: _auto_parse_import(service),
        )
        st.text_area(
            "或在此粘贴文本（每行一笔，逗号 / 制表符 / 空格分隔；首行写表头更佳）",
            height=170,
            key="import_paste",
            on_change=lambda: _auto_parse_import(service),
            placeholder="日期,代码,名称,方向,数量,价格,费用\n2026-01-05,600519,贵州茅台,买入,100,1680.00,0",
        )
        st.caption("上传文件或粘贴文本后会**自动解析**，核对下方表格后点「确认导入」即可。")
        _render_import_preview_safe(service)


def _apply_import(service: PortfolioHoldings, edited: pd.DataFrame) -> tuple[int, int, list[str]]:
    """执行导入（写库 + 去重），返回 (成功笔数, 跳过笔数, 失败明细)。不触碰 Streamlit 状态，便于测试。

    去重实时重算：拉取库里已有交易的去重键集合，写入一笔后立即并入集合，
    因此「历史已存在」与「本批内重复」都能正确跳过。
    """
    ok_count = 0
    skipped = 0
    fails: list[str] = []
    existing = {
        trade_dedup_key(d, c, s, q, p)
        for (d, c, s, q, p) in service.store.get_transactions_for_dedup(service.user_id)
    }
    for _, r in edited.iterrows():
        code = str(r.get("security_code", "")).strip()
        name = str(r.get("security_name", "")).strip() or code
        side = str(r.get("side", "")).strip().upper()
        try:
            qty = float(r.get("quantity") or 0)
            price = float(r.get("price") or 0)
            fee = float(r.get("fee") or 0)
        except (TypeError, ValueError):
            fails.append(f"{code}: 数量/价格不是数字")
            continue
        if not code:
            fails.append("存在空代码行")
            continue
        if side not in ("BUY", "SELL", "ADJUST"):
            fails.append(f"{code}: 方向无效（需 BUY/SELL/ADJUST）")
            continue
        if qty <= 0 or price < 0:
            fails.append(f"{code}: 数量必须>0、价格≥0")
            continue
        key = trade_dedup_key(str(r.get("trade_date", "")).strip(), code, side, qty, price)
        if key in existing:
            skipped += 1
            continue
        try:
            service.record_trade(
                security_code=code,
                security_name=name,
                side=side,
                quantity=qty,
                price=price,
                trade_date=str(r.get("trade_date", "")).strip() or date.today().isoformat(),
                fee=fee,
                note="批量导入",
                source_tag="导入",
            )
            ok_count += 1
            existing.add(key)
        except Exception as exc:
            fails.append(f"{code} {r.get('trade_date', '')}: {exc}")
    return ok_count, skipped, fails


def _import_confirm(service: PortfolioHoldings, edited: pd.DataFrame):
    """逐条写入解析结果，自动跳过与已登记记录重复的行，汇总成功/跳过/失败。"""
    ok_count, skipped, fails = _apply_import(service, edited)
    if ok_count:
        st.success(f"✅ 成功导入 {ok_count} 笔")
    if skipped:
        st.info(f"⏭️ 已自动跳过 {skipped} 笔与已登记记录重复的交易")
    if fails:
        st.error("以下记录导入失败：\n" + "\n".join(fails))
    st.session_state.pop("import_preview", None)
    st.rerun()

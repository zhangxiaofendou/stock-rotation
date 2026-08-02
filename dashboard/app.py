"""
A股板块轮动分析系统 - P3看板层
==============================
Streamlit多页应用，侧边栏导航。
"""

import streamlit as st
import sys
import os
import time
import json
import threading
import datetime
import logging
import concurrent.futures
from typing import Dict, Any

# 确保项目根目录在 Python path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from config.settings import LOG_DIR
from data.storage.sqlite_store import SQLiteStore
from config.logger import get_logger
from dashboard.components.data_source_badge import render_legend, render_src_badge

logger = get_logger(__name__)

# ============================================================
# 手动刷新：后台线程 + 进度轮询（避免 Streamlit 同步阻塞导致进程被杀）
# ============================================================
PIPELINE_PROGRESS_PATH = LOG_DIR / "pipeline_progress.json"

# 云端新部署时 logs/ 目录可能不存在，必须先创建，否则进度文件写入会静默失败
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _load_progress() -> Dict[str, Any]:
    try:
        if PIPELINE_PROGRESS_PATH.exists():
            return json.loads(PIPELINE_PROGRESS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_progress(state: Dict[str, Any]):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        PIPELINE_PROGRESS_PATH.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        logger.exception("写入管线进度文件失败")


# 全局线程池：用于给联网调用套超时，避免云端网络慢/不通时卡死整个页面脚本
_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def _with_timeout(func, seconds: float = 8, default=None):
    """在线程中执行 func，超时（seconds 秒）则返回 default，避免联网阻塞主线程。"""
    fut = _TIMEOUT_EXECUTOR.submit(func)
    try:
        return fut.result(timeout=seconds)
    except Exception:
        return default


class _PipelineProgressHandler(logging.Handler):
    """把管线运行日志的最新一行写入进度文件，供前端轮询展示。"""

    def emit(self, record):
        try:
            state = _load_progress()
            if state.get("status") != "running":
                return
            state["current_step"] = record.getMessage()[:300]
            state["last_heartbeat"] = datetime.datetime.now().isoformat()
            _save_progress(state)
        except Exception:
            pass


def _background_pipeline_run():
    """后台线程入口：执行完整数据管线，实时更新进度文件。"""
    try:
        _save_progress({
            "status": "running",
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "last_heartbeat": datetime.datetime.now().isoformat(timespec="seconds"),
            "current_step": "管线启动中...",
            "error": None,
        })
        handler = _PipelineProgressHandler()
        logging.getLogger().addHandler(handler)  # 挂到 root，捕获所有子模块日志
        try:
            from data.daily_pipeline import main as run_pipeline
            run_pipeline()
            state = _load_progress()
            state["status"] = "done"
            state["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            state["current_step"] = "管线执行完成"
            _save_progress(state)
        finally:
            logging.getLogger().removeHandler(handler)
    except Exception as e:
        import traceback
        state = _load_progress()
        state["status"] = "failed"
        state["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        state["error"] = f"{e}\n{traceback.format_exc()[-1500:]}"
        _save_progress(state)


# ============================================================
# 数据状态与刷新
# ============================================================
@st.cache_data(ttl=3600, show_spinner=False)
def get_latest_source_date() -> str:
    """查询数据源最新交易日（取沪深300基准的最新日期）。

    返回空字符串表示查询失败，避免阻塞看板渲染。
    """
    def _do():
        from data.sources import get_data_source
        source = get_data_source()
        df = source.get_benchmark_hist(symbol="sh000300")
        if df is not None and not df.empty:
            for col in ["date", "日期"]:
                if col in df.columns:
                    latest = df[col].dropna().max()
                    return str(latest)[:10]
        return ""
    return _with_timeout(_do, seconds=8, default="")


@st.cache_data(ttl=60, show_spinner=False)
def get_local_data_status():
    """获取本地各数据类型的最新状态（缓存 60s，避免每次交互重查 SQLite）。

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


def run_manual_data_update() -> tuple:
    """启动后台线程执行完整数据管线（非阻塞），返回 (started, message)。

    旧实现用 st.spinner 同步跑 5–10 分钟，Streamlit websocket 超时把脚本进程杀掉，
    导致资金流/板块组等后续步骤总是跑不到。改为后台线程 + 进度文件轮询，
    点击按钮立即返回，管线在后台跑，前端定时 rerun 读取进度。
    """
    state = _load_progress()
    if state.get("status") == "running":
        return False, "管线正在后台运行中，侧栏可见实时进度，请耐心等待（约 5–10 分钟）。"

    # 清除可能存在的旧进度文件，避免误判为仍在进行
    try:
        if PIPELINE_PROGRESS_PATH.exists():
            PIPELINE_PROGRESS_PATH.unlink()
    except Exception:
        pass

    t = threading.Thread(target=_background_pipeline_run, daemon=True)
    t.start()
    return True, "已启动完整数据管线（后台运行，约 5–10 分钟）。完成后页面会自动刷新；也可随时手动刷新查看进度。"


@st.cache_resource
def ensure_universe():
    """看板启动时确保板块宇宙(同花顺行业)就绪，并刷新 SQLite 板块元数据。"""
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


def _render_pipeline_progress():
    """侧栏展示后台管线运行进度（轮询 + 自动刷新）。"""
    state = _load_progress()
    if not state:
        return
    status = state.get("status")

    if status == "running":
        step = state.get("current_step", "")
        st.info(f"⏳ **管线运行中**\n\n{step}")
        # 心跳超时（>30 分钟无更新）视为卡死
        stuck = False
        hb = state.get("last_heartbeat")
        if hb:
            try:
                dt = datetime.datetime.fromisoformat(hb)
                if (datetime.datetime.now() - dt).total_seconds() > 1800:
                    stuck = True
            except Exception:
                pass
        if stuck:
            st.error("⚠️ 管线超过 30 分钟无进度更新，可能已卡死。可重新点击「手动刷新全部数据」重试。")
        else:
            st.caption("每 5 秒自动刷新进度，请保持页面打开…")
            time.sleep(5)
            st.rerun()
    elif status == "done":
        st.success(f"✅ **管线已完成**（{state.get('finished_at', '')}）")
        st.info("页面缓存已刷新，可在各模块查看最新数据。")
        try:
            if PIPELINE_PROGRESS_PATH.exists():
                PIPELINE_PROGRESS_PATH.unlink()
            st.cache_data.clear()
        except Exception:
            pass
    elif status == "failed":
        st.error(f"❌ **管线运行失败**\n\n{state.get('error', '')[:800]}")
        try:
            if PIPELINE_PROGRESS_PATH.exists():
                PIPELINE_PROGRESS_PATH.unlink()
        except Exception:
            pass


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


# ============================================================
# 数据源诊断 & 盘中实时行情条
# ============================================================
@st.cache_data(ttl=180, show_spinner=False)
def _probe_data_source() -> dict:
    """缓存 3 分钟的数据源连通性探针（解释为何行业数据滞后到 7.30）。"""
    def _do():
        from data.health_probe import probe_ths, verdict_ths
        p = probe_ths()
        p["_verdict"] = verdict_ths(p)
        return p
    try:
        return _with_timeout(_do, seconds=8, default={"_err": "数据源探针超时（>8s），已降级"})
    except Exception as e:  # noqa: BLE001
        return {"_err": str(e)}


@st.cache_data(ttl=180, show_spinner=False)
def _get_em_industry_count() -> int:
    """缓存板块宇宙可用行业数（避免每次交互都调 get_data_source().get_em_industry_list()）。"""
    try:
        from data.sources import get_data_source
        em_map = get_data_source().get_em_industry_list() or {}
        return len(em_map)
    except Exception:  # noqa: BLE001
        return 0


def render_data_source_health():
    """侧栏数据源诊断：一眼看清同花顺各接口是否可达、行业数据为何滞后。"""
    with st.sidebar.expander("🔧 数据源诊断（同花顺滞后排查）", expanded=True):
        render_src_badge("ths")
        try:
            p = _probe_data_source()
        except Exception as e:  # noqa: BLE001
            st.error(f"诊断异常: {e}")
            return
        if "_err" in p:
            st.error(f"诊断模块异常: {p['_err']}")
            return
        v = p.get("_verdict", "")
        if v.startswith("✅"):
            st.success(v)
        elif v.startswith("⚠️"):
            st.warning(v)
        else:
            st.error(v)
        st.caption(
            f"行业清单: {'✅' if p.get('list_ok') else '❌'}"
            f"（{p.get('list_count', 0)}个）"
            f" ｜ K线: {'✅' if p.get('kline_ok') else '❌'}"
            f" ｜ 实时: {'✅' if p.get('realtime_ok') else '❌'}"
        )
        if p.get("kline_date"):
            st.caption(f"抽样(半导体881121)最新K线 = {p['kline_date']}")
        if p.get("sample_name"):
            st.caption(f"抽样首个行业 = {p['sample_name']}")

        # 补充：实际可用于更新的行业数（含兜底，已缓存）
        cnt = _get_em_industry_count()
        if cnt:
            fallback_note = "（含静态兜底）" if not p.get("list_ok") else ""
            st.caption(f"当前板块宇宙可用行业数：{cnt}{fallback_note}")


def is_trading_now() -> bool:
    """判断当前是否为 A 股交易时段（周一至周五 9:30-11:30, 13:00-15:00）。"""
    import datetime
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    m1 = datetime.time(9, 30) <= t <= datetime.time(11, 30)
    m2 = datetime.time(13, 0) <= t <= datetime.time(15, 0)
    return m1 or m2


REALTIME_INTERVAL = 20  # 秒


@st.cache_data(ttl=20, show_spinner=False)
def _fetch_realtime_quotes() -> list:
    """拉取盘中实时行情（腾讯指数/ETF 实时源）。失败回退同花顺静态兜底。

    同花顺实时行业接口被反爬（运行时返回 None/401），故实时行情条改用
    腾讯 qt.gtimg.cn（运行时直连可用），展示主要指数 + 行业 ETF 实时涨跌。
    """
    try:
        from data.sources.tencent_source import TencentRealtimeSource
        src = TencentRealtimeSource()
        quotes = src.get_realtime_quotes()
        if quotes:
            return quotes
    except Exception as e:  # noqa: BLE001
        logger.warning(f"腾讯实时行情拉取失败: {e}")
    # 回退：同花顺静态兜底（涨幅置 0）
    try:
        from data.sources import get_data_source
        src = get_data_source()
        if hasattr(src, "get_realtime_sector_quotes"):
            fb = src.get_realtime_sector_quotes() or []
            if fb:
                return fb
    except Exception as e:  # noqa: BLE001
        logger.warning(f"实时行情回退源也失败: {e}")
    return []


def _chip_html(name, pct, up):
    color = "#d4380d" if up else "#389e0d"  # 红涨绿跌（A 股惯例）
    bg = "#fff1f0" if up else "#f6ffed"
    return (
        f'<span style="display:inline-block; margin:3px 4px; padding:3px 8px; '
        f'border-radius:10px; background:{bg}; color:{color}; '
        f'font-size:13px; font-weight:600; border:1px solid {color};">'
        f'{name} {pct:+.2f}%</span>'
    )


def render_realtime_ticker(live: bool):
    """顶部盘中实时行情条。live=True 时由 main() 在渲染完整个页面后统一休眠重跑。"""
    quotes = _fetch_realtime_quotes()

    col_a, col_b = st.columns([4, 1])
    with col_a:
        st.markdown("#### 📈 盘中实时行情 · 腾讯（指数 / 行业 ETF）")
        render_src_badge("tencent")
    with col_b:
        if not quotes:
            st.caption("⚪ 实时源不可用")
        elif is_trading_now():
            st.caption("🔴 交易时段")
        else:
            st.caption("⚪ 已收盘")

    if not quotes:
        st.info("当前未取到实时行情（需同花顺源且网络可达；本沙箱网络受限时可能为空）。")
        return

    ups = [q for q in quotes if q["pct"] > 0]
    downs = [q for q in quotes if q["pct"] < 0]
    flat = len(quotes) - len(ups) - len(downs)
    st.caption(f"共 {len(quotes)} 个行业 ｜ 🔴上涨 {len(ups)} ｜ 🟢下跌 {len(downs)} ｜ ⚪平 {flat}")

    top_up = sorted(ups, key=lambda x: -x["pct"])[:24]
    top_down = sorted(downs, key=lambda x: x["pct"])[:24]
    chips = "".join(_chip_html(q["name"], q["pct"], True) for q in top_up)
    chips += "".join(_chip_html(q["name"], q["pct"], False) for q in top_down)
    st.markdown(
        f'<div style="overflow-x:auto; white-space:nowrap; padding:6px 0; '
        f'border-top:1px solid #eee; border-bottom:1px solid #eee;">{chips}</div>',
        unsafe_allow_html=True,
    )


def main():
    """主入口"""
    # 页面配置
    st.set_page_config(
        page_title="A股板块轮动分析系统",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # 登录守卫：未登录只显示登录/注册界面，不渲染任何业务页面
    from auth import guard, logout_control, deploy_tag
    current_user = guard()
    if not current_user:
        return
    st.session_state["username"] = current_user

    # 启动时确保板块宇宙(同花顺行业)就绪，并刷新 SQLite 板块元数据
    ensure_universe()

    # ================================================================
    # 侧边栏
    # ================================================================
    with st.sidebar:
        st.title("📊 板块轮动分析")

        # 导航（刷新保留、关页重置）—— 上移到侧边栏顶部，便于快速切换页面
        from dashboard.components.nav_state import persistent_radio
        page = persistent_radio(
            "page",
            ["市场温度计", "板块轮动监控", "持仓管理", "信号绩效", "策略研究", "盘后报告"],
            label="导航菜单",
        )

        # 退出登录
        logout_control()

        # 一键全链路自检：出问题时点开即可拿到完整报告，无需逐项试错
        from auth import render_diagnostics_panel
        render_diagnostics_panel(in_sidebar=True)

        st.markdown("---")

        # 数据源诊断：为何刷新后行业数据仍滞后到 7.30（置顶 + 默认展开，确保一眼可见）
        render_data_source_health()

        st.markdown("---")

        # 数据状态与手动刷新
        st.markdown("### 📡 数据状态")
        render_src_badge("derived", base=["ths_kline", "em_flow"])

        source_date = get_latest_source_date()
        local_date, summary_df = get_local_data_status()

        col1, col2 = st.columns(2)
        with col1:
            st.caption("数据源最新交易日")
            st.markdown(f"**{source_date or '—'}**")
        with col2:
            st.caption("本地最新数据日期")
            st.markdown(f"**{local_date or '—'}**")

        # 手动刷新按钮
        if st.button("🔄 手动刷新全部数据", key="manual_refresh_data"):
            success, msg = run_manual_data_update()
            if success:
                st.success(msg)
            else:
                st.warning(msg)
            st.rerun()

        # 后台管线进度展示（运行中实时轮询，完成后自动刷新页面）
        _render_pipeline_progress()

        # 盘中实时刷新开关
        live_realtime = st.checkbox(
            "🔴 盘中实时刷新（每 20 秒）",
            value=False,
            help="开启后行情条在交易时段每 20 秒自动刷新；需同花顺源且网络可达。",
        )

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
        render_src_badge("derived", base=["history"])

        st.markdown("---")
        # 数据来源图例：解码界面各数据块的来源徽标
        render_legend()

        st.markdown("---")
        st.markdown("### 图例说明")
        st.markdown("🟢 ⑥弱转强、⑨底背离 — 买入信号")
        st.markdown("🟡 ③加速冲顶 — 持有不追")
        st.markdown("🟠 ①领涨减速、④强转弱 — 减仓信号")
        st.markdown("🔴 ⑦持续杀跌、⑧下跌中继 — 风险信号")
        st.markdown("⚪ ⑤中性震荡 — 观望")
        st.markdown("🟢 ②稳健上行 — 持有")

        st.markdown("---")
        st.caption(f"P3看板层 v1.0 ｜ 部署版本 {deploy_tag()}")
        # 存储后端提示：让「数据会不会丢」随时可见
        try:
            from data.storage import pg_store as _pg
            st.caption("💾 云数据库持久化：已启用" if _pg.is_enabled()
                       else "⚠️ 本地临时存储：重部署会清空数据")
        except Exception:
            pass

    # ================================================================
    # 顶部盘中实时行情条
    # ================================================================
    render_realtime_ticker(live_realtime)

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

    # 盘中实时自动刷新：渲染完整个页面后统一休眠并重跑（避免遮挡当前页）
    if live_realtime:
        time.sleep(REALTIME_INTERVAL)
        st.rerun()


if __name__ == "__main__":
    main()

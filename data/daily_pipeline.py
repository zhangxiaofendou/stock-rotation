"""
每日全量更新与指标重算管线
=======================
每天 22:00（收盘后）由定时任务调用，并额外配置 07:30 兜底自动化。
目标是：无论何时运行，都把数据补到**最新交易日收盘**，覆盖"22:00 因无网失败、
次日有网时再补"的场景。

确保：
  - 全部板块行情更新到最新交易日收盘
  - 基准指数（沪深300/中证500/中证1000）同步更新（RS 强依赖基准）
  - 所有指标重算：RS 指标 + 横截面排名 + 绝对价格趋势（落盘）
  - 清除派生快照缓存，看板下次加载即用最新数据

设计原则：
  - 幂等：重复运行安全；已是最新交易日收盘则直接跳过，不重复劳动
  - 兜底：网络步骤失败自动重试；次日 07:30 兜底自动化在联网时点补跑
  - 单步失败不阻断其余步骤，失败项写入日志
  - 周末/法定节假日自动跳过（最新交易日回退到最近的 weekday）

用法：
    cd stock-rotation
    python -m data.daily_pipeline
"""

import sys
import os
import subprocess
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402
from config.logger import get_logger  # noqa: E402
from config.settings import PARQUET_DIR  # noqa: E402
from config.sector_map import SW_LEVEL2_MAP  # noqa: E402
from data.sources import get_data_source  # noqa: E402
from data.storage.parquet_store import ParquetStore  # noqa: E402
from data.storage.sqlite_store import SQLiteStore  # noqa: E402
from data.freshness import DataFreshness  # noqa: E402
from data.daily_update import update_benchmarks as refresh_benchmarks  # noqa: E402
from indicators.price_trend import PriceTrend  # noqa: E402
from model.state_machine import StateMachine  # noqa: E402
from model.signal_ledger import SignalEventLedger  # noqa: E402

logger = get_logger(__name__)

# SQLite benchmark_map 中的基准代码 -> Parquet 存储文件名前缀
# （与 relative_strength._load_benchmark_data 的格式转换一致）
BENCHMARKS = {
    "000300.SH": "sh000300",  # 沪深300
    "000905.SH": "sh000905",  # 中证500
    "000852.SH": "sh000852",  # 中证1000
}

TREND_DIR = os.path.join(str(PARQUET_DIR), "indicators", "trend")


def is_trading_day(today: str) -> bool:
    """
    判断是否为交易日。

    优先查已入库的**真实交易日历**（含法定节假日，data/market_calendar.py +
    trade_calendar 表）；日历为空或过旧时回退到「周一至周五」近似，保证
    管线在任何环境下都能跑（节假日照常运行，AkShare 无新数据则下游自动跳过）。
    """
    try:
        from data.market_calendar import TradeCalendar
        from data.storage.sqlite_store import SQLiteStore
        if SQLiteStore().count_trade_calendar() > 0:
            return TradeCalendar().is_trading_day(today)
    except Exception as e:
        logger.debug(f"真实交易日历查询失败，回退 weekday：{e}")
    return datetime.strptime(today, "%Y-%m-%d").weekday() < 5


def close_data_available(today: str, now: datetime) -> bool:
    """判断 `today` 这天的收盘行情在 `now` 时刻是否已经可拉到。

    历史日期的收盘数据恒可得（A 股收盘即落库）；未来日期永远不可能有；只有
    「同一天」才需要看时刻：必须 ≥ 15:30（沪深收盘集合竞价结束，THS/东财
    接口此时开始提供当日 K 线 / 板块收盘）。

    之所以非要 15:30 这一刀：早盘 07:30 这种兜底自动化如果把目标日定为当天
    （因为当天是 weekday），下游就拉到空数据、产物的「最新日期」被刷新为空，
    看板因此一直停留在上一个真正成功的真实交易日（看板上看到「板块涨幅统计
    2026-07-31」就是这条 bug 的表现）。正确做法是把任何尚未收盘的候选日都
    当作「不可得」，继续向历史回溯。
    """
    try:
        target = datetime.strptime(today, "%Y-%m-%d").date()
    except Exception:
        return False
    if target < now.date():
        return True   # 历史日 → 永可得
    if target > now.date():
        return False  # 未来日 → 永不可得
    # 同日：必须是交易日，且当前时刻已过 15:30
    if not is_trading_day(today):
        return False
    cutoff = datetime.strptime("15:30", "%H:%M").time()
    return now.time() >= cutoff


def latest_trading_day(today: str, now: datetime = None) -> str:
    """
    返回 ≤ today 的、且收盘数据在 `now` 时刻已可得的最近交易日。

    优先用真实交易日历（含节假日）；日历为空或最近 30 天无任何交易日
    （日历过旧）时回退 weekday 法。

    与原版的差异：原版只看「今天是不是 weekday」就直接返回，导致周一 07:30
    这种「今天是交易日但当天收盘数据根本还没出」的场景把目标错误地定为当天，
    然后下游拉到空数据、产物的最新日期被刷新为空——最终板块页面停留在上一次
    真正成功跑出来的日期（实测就是 2026-07-31）。本版本在候选日就是 `today`
    时再叠一层 close_data_available 过滤，确保目标一定是「跑就一定能拉到数据」
    的那一个交易日。历史日直接采纳（历史数据恒可得）。
    """
    if now is None:
        now = datetime.now()
    try:
        from data.market_calendar import TradeCalendar
        from data.storage.sqlite_store import SQLiteStore
        if SQLiteStore().count_trade_calendar() > 0:
            cal = TradeCalendar()
            d = datetime.strptime(today, "%Y-%m-%d")
            for _ in range(30):
                ds = d.strftime("%Y-%m-%d")
                if cal.is_trading_day(ds):
                    if d.date() < now.date() or close_data_available(ds, now):
                        return ds
                d -= timedelta(days=1)
            logger.debug("真实日历最近 30 天无交易日（可能过旧），回退 weekday")
    except Exception as e:
        logger.debug(f"真实交易日历查询失败，回退 weekday：{e}")
    # weekday 回退：同样按 close_data_available 过滤
    d = datetime.strptime(today, "%Y-%m-%d")
    for _ in range(10):
        if d.weekday() < 5 and (
            d.date() < now.date()
            or close_data_available(d.strftime("%Y-%m-%d"), now)
        ):
            return d.strftime("%Y-%m-%d")
        d -= timedelta(days=1)
    return today


def ensure_trade_calendar():
    """确保交易日历已入库（含法定节假日）。

    优先 AkShare 拉取（在线）；失败/离线则回填 2000-2030 的 weekday 近似
    作为安全网。仅在 trade_calendar 表为空时执行，幂等。
    """
    from data.storage.sqlite_store import SQLiteStore
    sqlite = SQLiteStore()
    if sqlite.count_trade_calendar() > 0:
        logger.info("交易日历已存在（%s 条），跳过初始化。", sqlite.count_trade_calendar())
        return True
    try:
        from data.market_calendar import TradeCalendar
        ok = TradeCalendar(get_data_source(), sqlite).fetch_and_store()
        if ok:
            logger.info("交易日历通过 AkShare 初始化完成。")
            return True
    except Exception as e:
        logger.warning(f"AkShare 交易日历拉取失败，回退 weekday 近似：{e}")
    _build_weekday_calendar(sqlite)
    return True


def _build_weekday_calendar(sqlite: SQLiteStore):
    """离线回退：把 2000-01-01 ~ 2030-12-31 的周一至周五写入交易日历。
    不含法定节假日，仅作安全网。"""
    from datetime import date
    rows = []
    d = date(2000, 1, 1)
    end = date(2030, 12, 31)
    while d <= end:
        if d.weekday() < 5:
            rows.append((d.strftime("%Y-%m-%d"), 1, d.weekday()))
        d += timedelta(days=1)
    if rows:
        sqlite.insert_trade_calendar_batch(rows)
        logger.info(f"离线 weekday 近似日历已写入 {len(rows)} 条（不含法定节假日）。")


def _max_date_in_dir(d: str, col: str = "date"):
    """返回目录下所有 parquet 文件某日期列的最大日期；目录为空/无文件返回 None。"""
    if not os.path.isdir(d):
        return None
    files = [f for f in os.listdir(d) if f.endswith(".parquet")]
    if not files:
        return None
    mx = None
    for f in files:
        try:
            s = pd.read_parquet(os.path.join(d, f), columns=[col])[col]
            cur = pd.to_datetime(s).max()
            if mx is None or cur > mx:
                mx = cur
        except Exception:
            continue
    return mx


def data_is_current(target: str) -> bool:
    """
    判断本地数据是否已更新到目标交易日收盘。

    同时检查趋势(trend)与 RS 两个最终产物目录的最新日期——只有两者都
    ≥ 目标交易日，才认为整条管线已成功完成，可安全幂等跳过。任一步骤
    （行情/基准/RS/趋势）失败导致产物滞后，都返回 False 触发补跑。
    """
    trend_dir = TREND_DIR
    rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "rs")
    mt = _max_date_in_dir(trend_dir)
    mr = _max_date_in_dir(rs_dir)
    if mt is None or mr is None:
        return False
    return mt.strftime("%Y-%m-%d") >= target and mr.strftime("%Y-%m-%d") >= target


def _record_parquet_freshness(data_type: str, directory: str, col: str = "date",
                              freshness: DataFreshness = None) -> str:
    """记录 parquet 目录的最新日期到 data_freshness（无 key 聚合）。"""
    f = freshness or DataFreshness()
    mx = _max_date_in_dir(directory, col=col)
    end_date = mx.strftime("%Y-%m-%d") if mx is not None else None
    count = 0
    if os.path.isdir(directory):
        count = len([fn for fn in os.listdir(directory) if fn.endswith(".parquet")])
    f.record_update(
        data_type=data_type,
        data_key=None,
        data_end=end_date,
        record_count=count,
        status="ok" if end_date else "stale",
    )
    return end_date


def _record_sqlite_freshness(data_type: str, date_sql: str, count_sql: str,
                             freshness: DataFreshness = None) -> str:
    """记录 SQLite 表最新日期与行数到 data_freshness（无 key 聚合）。"""
    f = freshness or DataFreshness()
    sqlite = SQLiteStore()
    end_date = None
    count = 0
    try:
        conn = sqlite._get_conn()
        try:
            row = conn.execute(date_sql).fetchone()
            end_date = row[0] if row and row[0] else None
            row = conn.execute(count_sql).fetchone()
            count = row[0] if row and row[0] else 0
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"记录 {data_type} 新鲜度失败: {e}")
    f.record_update(
        data_type=data_type,
        data_key=None,
        data_end=end_date,
        record_count=count,
        status="ok" if end_date else "stale",
    )
    return end_date


def update_benchmarks():
    """同步更新基准指数与新鲜度记录，复用手动刷新唯一实现。"""
    updated, errors = refresh_benchmarks(
        get_data_source(), ParquetStore(), DataFreshness()
    )
    logger.info(f"基准指数更新完成：成功 {updated}，失败 {errors}")


def recompute_trends():
    """重算并落盘所有板块的绝对价格趋势。

    state_machine / scoring / circuit_breaker 均读取 indicators/trend/*.parquet，
    但项目内无其它入口负责写回，故每日管线必须显式重算，否则状态机用旧趋势。
    """
    pt = PriceTrend(ParquetStore(), SQLiteStore())
    os.makedirs(TREND_DIR, exist_ok=True)
    ok = err = 0
    for code in SW_LEVEL2_MAP:
        try:
            df = pt.calc_trend_series(code)
            if df is None or df.empty:
                continue
            safe = code.replace(".", "_")
            df.to_parquet(os.path.join(TREND_DIR, f"{safe}.parquet"), index=False)
            ok += 1
        except Exception as e:
            logger.error(f"趋势重算失败 {code}: {e}")
            err += 1
    logger.info(f"趋势重算完成: 成功 {ok}, 失败 {err}")


def sync_signal_events():
    """将重算后的状态序列同步到共享信号事件账本。"""
    parquet = ParquetStore()
    sqlite = SQLiteStore()
    # 元数据表可能为空（新环境/云端），先确保 sectors 存在，避免外键约束失败
    sqlite.ensure_sectors()
    ledger = SignalEventLedger(sqlite, StateMachine(parquet, sqlite))
    summary = ledger.sync()
    logger.info(
        "信号事件同步完成：板块 %s，事件 %s，失败 %s",
        summary["processed_sectors"],
        summary["written_events"],
        len(summary["failed_sectors"]),
    )
    return summary


def enrich_signal_performance():
    """补齐最近窗口内信号事件的后续表现，供信号绩效/失效预警/历史回放复用。

    采用增量模式（默认仅处理最近 400 个交易日之后发生的事件），因为更早
    事件的 T+20 表现已固定且不会变化；增量模式使每日管线开销可控。
    首次上线或口径升级需全量历史时，单独运行 `python -m signal_tracker.tracker`。
    """
    try:
        from signal_tracker.tracker import SignalTracker
        since = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
        tracker = SignalTracker()
        summary = tracker.enrich_events(since_date=since)
        logger.info(
            "信号后续表现补全完成：评估 %s，写入 %s，跳过 %s，失败 %s",
            summary["evaluated"], summary["written"], summary["skipped"], len(summary["failed_sectors"]),
        )
    except Exception as e:
        logger.error(f"信号后续表现补全失败: {e}")


def enrich_fund_flow(target: str):
    """计算每个板块的资金流信号并落盘。

    真实主力资金流来自 AkShare 同花顺行业资金流（运行时可达，映射到 881xxx），
    替代此前用涨跌幅代理的方案。AkShare 不可用时回退到 MoneyFlowIndicator 的
    涨跌幅代理逻辑，保证离线可运行。

    单步失败不阻断其余板块；rec 始终包含全部绑定字段，避免 executemany 绑定缺失。
    """
    try:
        parquet, sqlite = ParquetStore(), SQLiteStore()

        # 清除旧的资金流排名缓存，避免跨日/跨源时用上昨日或测试数据
        try:
            for cache_path in parquet.fund_flow_dir.glob("sector_fund_flow_*.parquet"):
                cache_path.unlink()
                logger.info(f"已清除资金流缓存: {cache_path.name}")
        except Exception as e:
            logger.warning(f"清除资金流缓存失败: {e}")

        # 1) 优先取 AkShare 真实同花顺行业资金流（映射到 881xxx）
        real_ff: dict = {}
        try:
            from data.sources.akshare_fund_flow import fetch_ths_industry_fund_flow
            real_ff = fetch_ths_industry_fund_flow()
            logger.info(f"真实行业资金流获取成功: {len(real_ff)} 个板块")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"AkShare 真实资金流获取失败，回退涨跌幅代理: {e}")

        # 2) 回退源（仅当 AkShare 失败时使用）
        mfi = None
        if not real_ff:
            try:
                from indicators.money_flow import MoneyFlowIndicator
                mfi = MoneyFlowIndicator(parquet, sqlite, get_data_source())
            except Exception as e:  # noqa: BLE001
                logger.warning(f"资金流回退指标初始化失败: {e}")

        rows = []
        for code in SW_LEVEL2_MAP:
            rec = {
                "sector_code": code,
                "date": target,
                "signal": "中性",
                "rank": None,
                "rank_change": None,
                "trend": None,
                "main_net_inflow": None,
            }
            rf = real_ff.get(code)
            if rf:
                # 真实资金流：主力净流入决定信号与排名
                net = rf["net_inflow"]
                rec["main_net_inflow"] = net
                rec["signal"] = "正向" if net > 0 else ("反向" if net < 0 else "中性")
            elif mfi is not None:
                # 回退：涨跌幅代理
                try:
                    sig = mfi.calc_fund_flow_signal(code)
                    rec["signal"] = sig or "中性"
                    trend = mfi.calc_fund_flow_trend(code, days=5)
                    if trend:
                        rec["rank"] = trend.get("current_rank")
                        rec["rank_change"] = trend.get("rank_change")
                        rec["trend"] = trend.get("trend")
                except Exception as e:
                    logger.debug(f"板块 {code} 资金流回退计算失败: {e}")
            rows.append(rec)
        sqlite.upsert_sector_fund_flow(rows)
        logger.info(f"资金流落盘完成: {len(rows)} 个板块（真实 {len(real_ff)} 个）")
    except Exception as e:
        logger.error(f"资金流落盘失败: {e}")
        raise


def enrich_divergence(target: str):
    """计算每个板块的分化度（一致性指标）并落盘。

    无成分股时用指数日内波动率替代，离线即可运行（依赖 index_hist parquet）。
    """
    try:
        from indicators.divergence import SectorDivergence
        parquet, sqlite = ParquetStore(), SQLiteStore()
        sd = SectorDivergence(parquet, sqlite)
        rows = []
        ok = skip = 0
        for code in SW_LEVEL2_MAP:
            try:
                d = sd.calc_divergence(code, target)
                if d is None:
                    skip += 1
                    continue
                rows.append({
                    "sector_code": code, "date": target,
                    "divergence": float(d), "method": "auto",
                })
                ok += 1
            except Exception as e:
                skip += 1
        if rows:
            sqlite.upsert_sector_divergence(rows)
        logger.info(f"分化度落盘完成: 成功 {ok}, 跳过 {skip}")
    except Exception as e:
        logger.error(f"分化度落盘失败: {e}")
        raise


def enrich_confirmation_factors(target: str):
    """P3.4 确认因子落盘：资金流 + 分化度。任一失败不阻断另一条。"""
    enrich_fund_flow(target)
    enrich_divergence(target)


def generate_daily_report():
    """管线末尾生成当日盘后报告，并按订阅推送通知（PRD 阶段 D）。

    报告只汇总已计算结果，失败不影响主管线。通知走统一 NotificationService，
    仅当事件已订阅且渠道已配置时才真正发送。
    """
    try:
        from report.generator import generate_report
        res = generate_report()
        logger.info("盘后报告已生成：%s", res["as_of_date"])
        # 失败告警：若市场环境为防御/熔断，单独补发一条通知（复用统一服务）
        try:
            from notification.service import NotificationService
            svc = NotificationService()
            if svc.should_notify("report_generated"):
                svc.notify_event(
                    "report_generated",
                    f"盘后报告 {res['as_of_date']} 已生成",
                    "盘后报告已生成，详见应用内「盘后报告」页。",
                )
        except Exception as e:
            logger.warning("盘后报告通知失败（不影响报告生成）: %s", e)
    except Exception as e:
        logger.error(f"盘后报告生成失败: {e}")


def run_module(mod: str) -> int:
    """以子进程运行一个 python -m 模块，捕获日志。返回 returncode。"""
    logger.info(f"▶ 执行 python -m {mod}")
    r = subprocess.run(
        [sys.executable, "-m", mod],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        logger.error(f"{mod} 失败 (returncode={r.returncode}):\n{r.stderr[-1500:]}")
    else:
        tail = [l for l in r.stdout.strip().splitlines() if l.strip()][-6:]
        for line in tail:
            logger.info(f"  {mod}: {line}")
    return r.returncode


def run_with_retry(mod: str, tries: int = 2, backoff: int = 300) -> int:
    """
    带重试地运行一个 python -m 模块（用于联网拉取等易失败步骤）。

    失败（returncode≠0）时按 backoff 秒退避重试，最多 tries 次。
    覆盖"22:00 当晚短暂断网/接口抖动"的场景，避免当日直接跳过。
    """
    rc = 1
    for i in range(tries):
        rc = run_module(mod)
        if rc == 0:
            return rc
        if i < tries - 1:
            logger.warning(f"{mod} 第 {i + 1} 次失败，{backoff}s 后重试（共 {tries} 次）")
            time.sleep(backoff)
    return rc


def _backfill_missing_sector_klines():
    """管线兜底：对照当前板块宇宙，补齐缺失的行业 K 线 parquet。

    根因：当其它板块均已「current」时，data_is_current 守卫会让整条管线跳过，
    导致个别板块（如 881121 半导体）偶发漏拉后永远不被补齐。此函数在管线末尾
    无条件执行，确保 90 个同花顺行业 K 线齐全。
    """
    try:
        from data.sources import get_data_source
        from data.storage.parquet_store import ParquetStore
        from config.sector_map import SW_LEVEL2_MAP

        parquet = ParquetStore()
        source = get_data_source()
        target_codes = list(SW_LEVEL2_MAP.keys())
        if not target_codes:
            return
        missing = [c for c in target_codes if not parquet.index_hist_exists(c)]
        if not missing:
            logger.info("行业 K 线齐全（%d 个），无需补齐", len(target_codes))
            return
        logger.warning("检测到 %d 个行业 K 线缺失，启动补齐: %s", len(missing), missing)
        for code in missing:
            try:
                df = source.get_sw_index_hist(symbol=code, period="day")
                if df is not None and not df.empty:
                    parquet.save_index_hist(code, df)
                    logger.info("补齐行业 K 线 %s 成功（%d 行）", code, len(df))
                else:
                    logger.warning("补齐行业 K 线 %s 仍失败（源返回空）", code)
            except Exception as e:  # noqa: BLE001
                logger.warning("补齐行业 K 线 %s 异常: %s", code, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("行业 K 线补齐流程异常（非致命）: %s", e)


def main():
    run_id = datetime.now().isoformat(timespec="seconds")
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    steps = []
    run_status = "success"
    run_error = None
    today = datetime.now().strftime("%Y-%m-%d")

    # 0. 确保板块宇宙就绪（东财行业清单 → config.sector_map），并刷新 SQLite 元数据
    try:
        from data.sources import get_data_source
        from data.sector_universe import ensure_em_industry_map
        from data.storage.sqlite_store import SQLiteStore
        ensure_em_industry_map(get_data_source())
        SQLiteStore().ensure_sectors()
    except Exception as e:
        logger.warning("板块宇宙初始化失败(非致命): %s", e)

    # 0. 真实交易日历（含法定节假日）best-effort 保证已入库，供后续判断使用
    try:
        ensure_trade_calendar()
        steps.append("交易日历: ok")
    except Exception as e:
        steps.append(f"交易日历: 跳过({e})")

    # 目标：补到最新交易日收盘。任意时点运行都以此为目标，天然支持"失败后续补"。
    # 必须把当前时刻一并传入，否则早盘 07:30 兜底自动化会把目标错误地定在
    # 当天（当天虽是 weekday 但尚未收盘），下游拉到空数据、产物最新日期被刷新为空。
    target = latest_trading_day(today, datetime.now())

    # 幂等守卫（优先级最高）：趋势与 RS 产物均已覆盖目标交易日，说明整条管线已完成，
    # 直接跳过。这能正确覆盖「周末/节假日的正常无更新」场景（数据已是最近交易日收盘）。
    try:
        if data_is_current(target):
            logger.info(f"数据已为最新交易日 {target} 收盘，无需更新（幂等跳过）。")
            # 刷新各产物新鲜度，保证「数据状态」表格完整
            _record_parquet_freshness("indicators/rs", os.path.join(str(PARQUET_DIR), "indicators", "rs"))
            _record_parquet_freshness("indicators/crowding", os.path.join(str(PARQUET_DIR), "indicators", "crowding"))
            _record_parquet_freshness("indicators/trend", TREND_DIR)
            _record_sqlite_freshness(
                "sector_fund_flow",
                "SELECT date FROM sector_fund_flow ORDER BY date DESC LIMIT 1",
                "SELECT COUNT(*) FROM sector_fund_flow",
            )
            _record_sqlite_freshness(
                "sector_divergence",
                "SELECT date FROM sector_divergence ORDER BY date DESC LIMIT 1",
                "SELECT COUNT(*) FROM sector_divergence",
            )
            # 即便行情和指标已是最新，也同步账本：首次上线、状态规则升级后可补齐历史事件。
            sync_signal_events()
            _record_sqlite_freshness(
                "signal_events",
                "SELECT event_date FROM signal_events ORDER BY event_date DESC LIMIT 1",
                "SELECT COUNT(*) FROM signal_events",
            )
            steps.append("信号事件同步: ok")
            # 同步账本后增量补全信号后续表现，保证绩效数据不滞后。
            enrich_signal_performance()
            steps.append("信号表现补全: ok")
            # 管线末尾生成当日盘后报告（含通知）
            generate_daily_report()
            steps.append("盘后报告: ok")
        else:
            # 非交易日但数据滞后：仍补跑到最新交易日收盘（如周六补周五数据），
            # 确保「当日更新失败 → 下一次有网时必补到最新交易日收盘」。
            if not is_trading_day(today):
                logger.info(
                    f"{today} 非交易日，但数据滞后于最新交易日 {target}，执行补跑。"
                )

            logger.info(f"========== 每日全量更新管线 {today}（目标：{target} 收盘）==========")
            # 1. 板块行情（含快照失效）—— 联网步骤，失败自动重试
            run_with_retry("data.daily_update", tries=2, backoff=300)
            steps.append("板块行情: ok")
            # 2. 基准指数（RS 强依赖，必须同步）
            update_benchmarks()
            steps.append("基准指数: ok")
            # 3. RS 指标 + 横截面排名
            run_module("indicators.calc_all")
            _record_parquet_freshness("indicators/rs", os.path.join(str(PARQUET_DIR), "indicators", "rs"))
            _record_parquet_freshness("indicators/crowding", os.path.join(str(PARQUET_DIR), "indicators", "crowding"))
            steps.append("RS/横截面: ok")
            # 4. 绝对价格趋势落盘（state_machine / scoring 读取）
            recompute_trends()
            _record_parquet_freshness("indicators/trend", TREND_DIR)
            steps.append("价格趋势: ok")
            # 4.5 确认因子落盘（资金流 + 分化度），无网络/无数据则优雅跳过
            try:
                enrich_confirmation_factors(target)
                _record_sqlite_freshness(
                    "sector_fund_flow",
                    "SELECT date FROM sector_fund_flow ORDER BY date DESC LIMIT 1",
                    "SELECT COUNT(*) FROM sector_fund_flow",
                )
                _record_sqlite_freshness(
                    "sector_divergence",
                    "SELECT date FROM sector_divergence ORDER BY date DESC LIMIT 1",
                    "SELECT COUNT(*) FROM sector_divergence",
                )
                steps.append("确认因子: ok")
            except Exception as e:
                steps.append(f"确认因子: 跳过({e})")
            # 5. 状态已经按最新指标重算，固化发生变化的状态事件供绩效/回放/报告复用。
            sync_signal_events()
            _record_sqlite_freshness(
                "signal_events",
                "SELECT event_date FROM signal_events ORDER BY event_date DESC LIMIT 1",
                "SELECT COUNT(*) FROM signal_events",
            )
            steps.append("信号事件同步: ok")
            # 6. 补齐最近窗口信号事件的后续实际表现（T+5/T+20 收益、成败判定）。
            enrich_signal_performance()
            steps.append("信号表现补全: ok")
            # 7. 管线末尾生成当日盘后报告（含通知）
            generate_daily_report()
            steps.append("盘后报告: ok")
            logger.info(f"========== 每日全量更新管线完成 {today}（已更新至 {target} 收盘）==========")

        # 9. 行业 K 线兜底补齐（无论行情是否 current，确保 90 行业齐全，修复个别板块漏拉）
        try:
            _backfill_missing_sector_klines()
            steps.append("行业K线补齐: ok")
        except Exception as e:  # noqa: BLE001
            steps.append(f"行业K线补齐: 跳过({e})")

        # 8. 双源校验（AkShare/Tushare），未配置第二数据源则跳过
        try:
            from data.dual_source_check import run_dual_source_check
            ds = run_dual_source_check()
            steps.append(f"双源校验: {ds.get('status')}")
        except Exception as e:
            steps.append(f"双源校验: 跳过({e})")
    except Exception as e:
        run_status = "failed"
        run_error = str(e)
        logger.exception("每日管线执行失败: %s", e)
    finally:
        finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            from data.storage.sqlite_store import SQLiteStore
            SQLiteStore().log_pipeline_run(
                run_id, started_at, finished_at, run_status, target,
                "; ".join(steps), run_error,
            )
        except Exception:
            pass
        # 管线收尾：把最新派生 parquet 镜像进云库，供 Reboot 后秒级恢复基数据
        # （防御式：失败仅记录，不影响主管线）
        try:
            from data.storage.parquet_mirror import upload_parquet_mirror
            upload_parquet_mirror()
        except Exception:
            logger.warning("管线收尾：parquet 镜像上传失败（非致命）", exc_info=True)


if __name__ == "__main__":
    main()

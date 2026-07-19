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
from data.sources.akshare_source import AkShareSource  # noqa: E402
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
    判断是否为交易日（轻量版）。

    周一至周五视为交易日；法定节假日会照常运行，但 AkShare 无新数据、
    下游各步骤自动跳过，不会误报错。完整的交易日历（含节假日）见
    data/calendar.py，后续可在此替换 weekday 判断。
    """
    return datetime.strptime(today, "%Y-%m-%d").weekday() < 5


def latest_trading_day(today: str) -> str:
    """
    返回 ≤ today 的最近交易日（weekday 回退法）。

    用于决定"本次更新应补到哪个交易日收盘"。非交易日则向前回退到
    最近的 weekday。注：与 is_trading_day 保持一致的轻量实现，不依赖
    需联网入库的完整交易日历。
    """
    d = datetime.strptime(today, "%Y-%m-%d")
    for _ in range(10):
        if d.weekday() < 5:
            return d.strftime("%Y-%m-%d")
        d -= timedelta(days=1)
    return today


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


def update_benchmarks():
    """同步更新基准指数与新鲜度记录，复用手动刷新唯一实现。"""
    updated, errors = refresh_benchmarks(
        AkShareSource(), ParquetStore(), DataFreshness()
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


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    # 目标：补到最新交易日收盘。任意时点运行都以此为目标，天然支持"失败后续补"。
    target = latest_trading_day(today)

    # 幂等守卫（优先级最高）：趋势与 RS 产物均已覆盖目标交易日，说明整条管线已完成，
    # 直接跳过。这能正确覆盖「周末/节假日的正常无更新」场景（数据已是最近交易日收盘）。
    if data_is_current(target):
        logger.info(f"数据已为最新交易日 {target} 收盘，无需更新（幂等跳过）。")
        # 即便行情和指标已是最新，也同步账本：首次上线、状态规则升级后可补齐历史事件。
        sync_signal_events()
        # 同步账本后增量补全信号后续表现，保证绩效数据不滞后。
        enrich_signal_performance()
        # 管线末尾生成当日盘后报告（含通知）
        generate_daily_report()
        return

    # 非交易日但数据滞后：仍补跑到最新交易日收盘（如周六补周五数据），
    # 确保「当日更新失败 → 下一次有网时必补到最新交易日收盘」。
    if not is_trading_day(today):
        logger.info(
            f"{today} 非交易日，但数据滞后于最新交易日 {target}，执行补跑。"
        )

    logger.info(f"========== 每日全量更新管线 {today}（目标：{target} 收盘）==========")
    # 1. 板块行情（含快照失效）—— 联网步骤，失败自动重试
    run_with_retry("data.daily_update", tries=2, backoff=300)
    # 2. 基准指数（RS 强依赖，必须同步）
    update_benchmarks()
    # 3. RS 指标 + 横截面排名
    run_module("indicators.calc_all")
    # 4. 绝对价格趋势落盘（state_machine / scoring 读取）
    recompute_trends()
    # 5. 状态已经按最新指标重算，固化发生变化的状态事件供绩效/回放/报告复用。
    sync_signal_events()
    # 6. 补齐最近窗口信号事件的后续实际表现（T+5/T+20 收益、成败判定）。
    enrich_signal_performance()
    # 7. 管线末尾生成当日盘后报告（含通知）
    generate_daily_report()
    logger.info(f"========== 每日全量更新管线完成 {today}（已更新至 {target} 收盘）==========")


if __name__ == "__main__":
    main()

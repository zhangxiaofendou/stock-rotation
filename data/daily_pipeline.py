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
    logger.info(f"========== 每日全量更新管线完成 {today}（已更新至 {target} 收盘）==========")


if __name__ == "__main__":
    main()

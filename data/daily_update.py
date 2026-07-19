"""
每日增量数据更新脚本
====================
每天收盘后运行一次，拉取所有板块最新行情数据，合并到 Parquet 存储，
并清除快照缓存使 Dashboard 下次刷新时重新计算。

用法:
    cd stock-rotation
    python -m data.daily_update

或直接:
    python data/daily_update.py
"""

import sys
import time
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 提升控制台日志级别，确保用户能看到进度
_logger = logging.getLogger("stock_rotation")
if _logger.handlers:
    _logger.handlers[0].setLevel(logging.INFO)

import pandas as pd

from config.logger import get_logger
from config.settings import BATCH_SIZE, BATCH_SLEEP
from config.sector_map import SW_LEVEL2_MAP
from data.sources.akshare_source import AkShareSource
from data.storage.parquet_store import ParquetStore
from data.freshness import DataFreshness

logger = get_logger(__name__)

# RS 计算使用的基准指数。键为储存/新鲜度记录的规范代码，值为 AkShare 接口代码。
BENCHMARKS = {
    "sh000300": "沪深300",
    "sh000905": "中证500",
    "sh000852": "中证1000",
    "sh000016": "上证50",
    "sz399006": "创业板指",
}
# 早期初始化曾用无交易所前缀的代码写入失败记录；更新时应清理。
LEGACY_BENCHMARK_KEYS = ("000300", "000905", "000852", "000016", "399006")

# 快照缓存路径
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "storage" / "parquet" / "cache"
STATE_SNAPSHOT = SNAPSHOT_DIR / "state_snapshot.parquet"


def invalidate_snapshots():
    """清除快照缓存，让 Dashboard 下次加载时重新计算"""
    deleted = []
    for snap in [STATE_SNAPSHOT]:
        if snap.exists():
            snap.unlink()
            deleted.append(snap.name)
            logger.info(f"已删除快照缓存: {snap}")
    if not deleted:
        logger.info("无快照缓存需清除")
    return deleted


def update_all_sectors(source: AkShareSource, parquet: ParquetStore,
                       freshness: DataFreshness, dry_run: bool = False):
    """
    增量更新所有申万二级板块的行情数据。

    逻辑：
    1. 从 AkShare 拉取全量历史数据
    2. 与本地已有数据合并（按日期去重）
    3. 如果无新数据则跳过该板块

    参数:
        dry_run: 只检查不写入
    """
    codes = list(SW_LEVEL2_MAP.keys())
    total = len(codes)
    updated = 0
    skipped = 0
    errors = 0

    logger.info(f"=" * 60)
    logger.info(f"开始增量更新 {total} 个板块（{'预览模式' if dry_run else '正式模式'}）")
    logger.info(f"=" * 60)

    for i, code in enumerate(codes):
        name = SW_LEVEL2_MAP[code][0]
        prefix = f"[{i+1}/{total}]"

        try:
            # 1. 从 AkShare 拉取全量历史
            df_new = source.get_sw_index_hist(symbol=code, period="day")
            if df_new is None or df_new.empty:
                logger.warning(f"{prefix} {code} {name}: AkShare 返回空数据")
                errors += 1
                continue

            # 标准化列名为小写（a kshare_source 已处理，此处防御）
            df_new.columns = [str(col).lower() for col in df_new.columns]

            # 统一日期列名
            date_col = None
            for col in ["date", "日期"]:
                if col in df_new.columns:
                    date_col = col
                    break
            if date_col is None:
                logger.warning(f"{prefix} {code} {name}: 无日期列，跳过")
                errors += 1
                continue

            # 2. 读取本地已有数据
            new_dates = set()
            if date_col in df_new.columns:
                try:
                    new_dates = set(pd.to_datetime(df_new[date_col]).dt.strftime("%Y-%m-%d"))
                except Exception:
                    new_dates = set(df_new[date_col].astype(str))

            existing_dates = set()
            existing_df = parquet.load_index_hist(code)
            if existing_df is not None and not existing_df.empty:
                # 找到已有数据的日期列
                ex_date_col = None
                for col in ["date", "日期"]:
                    if col in existing_df.columns:
                        ex_date_col = col
                        break
                if ex_date_col:
                    try:
                        existing_dates = set(pd.to_datetime(existing_df[ex_date_col]).dt.strftime("%Y-%m-%d"))
                    except Exception:
                        existing_dates = set(existing_df[ex_date_col].astype(str))

            # 3. 检查是否有新数据
            only_new = new_dates - existing_dates
            if not only_new:
                logger.debug(f"{prefix} {code} {name}: 无新数据（最新: {max(existing_dates) if existing_dates else 'N/A'}）")
                skipped += 1
            else:
                newest = sorted(only_new)[-1]
                logger.info(f"{prefix} {code} {name}: 发现 {len(only_new)} 天新数据, 最新: {newest}")

                if not dry_run:
                    # 4. 合并并保存（使用新数据为准，覆盖已有日期）
                    # 统一列名以便合并
                    df_new_clean = df_new.copy()
                    if "日期" in df_new_clean.columns and date_col != "日期":
                        df_new_clean.rename(columns={"日期": date_col}, inplace=True)

                    if existing_df is not None and not existing_df.empty:
                        if "日期" in existing_df.columns and date_col != "日期":
                            existing_df = existing_df.rename(columns={"日期": date_col})

                        # 合并：新数据 + 旧数据（排除新数据已有日期的旧数据）
                        combined = pd.concat([
                            df_new_clean,
                            existing_df[~existing_df[date_col].astype(str).isin(
                                df_new_clean[date_col].astype(str)
                            )]
                        ], ignore_index=True)
                    else:
                        combined = df_new_clean

                    # 按日期排序
                    combined[date_col] = pd.to_datetime(combined[date_col])
                    combined = combined.sort_values(date_col).reset_index(drop=True)

                    # 保存
                    parquet.save_index_hist(code, combined)

                # 记录新鲜度
                data_end = str(sorted(only_new)[-1]) if only_new else str(max(existing_dates))
                freshness.record_update(
                    data_type="sector_hist",
                    data_key=code,
                    data_end=data_end,
                    record_count=len(df_new),
                    status="ok",
                )
                updated += 1

        except Exception as e:
            logger.error(f"{prefix} {code} {name}: 异常 - {e}")
            errors += 1

        # 批次间休眠
        if (i + 1) % BATCH_SIZE == 0:
            logger.info(f"已处理 {i+1}/{total}, 休眠 {BATCH_SLEEP}秒...")
            time.sleep(BATCH_SLEEP)

    # 汇总
    logger.info(f"=" * 60)
    logger.info(f"增量更新完成: 更新 {updated}, 跳过 {skipped}, 错误 {errors}")
    logger.info(f"=" * 60)

    return updated, skipped, errors


def update_benchmarks(source: AkShareSource, parquet: ParquetStore,
                      freshness: DataFreshness, dry_run: bool = False) -> tuple:
    """更新 RS 所依赖的基准指数，并同步修复其新鲜度记录。

    返回 `(updated, errors)`。基准文件按 AkShare 规范代码（如 `sh000300`）落盘，
    新鲜度记录使用同一代码，避免历史无前缀失败记录被汇总为异常。
    """
    updated = errors = 0

    # 清理旧初始化流程留下的无交易所前缀错误记录。
    if not dry_run:
        for legacy_key in LEGACY_BENCHMARK_KEYS:
            freshness.store.delete_freshness("benchmark_hist", legacy_key)

    for code, name in BENCHMARKS.items():
        try:
            df = source.get_benchmark_hist(symbol=code)
            if df is None or df.empty or "date" not in df.columns:
                raise ValueError("AkShare 返回空数据或缺少 date 列")

            df = df.copy()
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)

            if not dry_run:
                parquet.save_benchmark_hist(code, df)
                freshness.record_update(
                    data_type="benchmark_hist",
                    data_key=code,
                    data_start=df["date"].iloc[0],
                    data_end=df["date"].iloc[-1],
                    record_count=len(df),
                    status="ok",
                )
            updated += 1
            logger.info(f"基准指数 {name} ({code}) 已更新，最新 {df['date'].iloc[-1]}")
        except Exception as e:
            errors += 1
            logger.error(f"基准指数 {name} ({code}) 更新失败: {e}")
            if not dry_run:
                freshness.record_update(
                    data_type="benchmark_hist", data_key=code, status="error"
                )
    return updated, errors


def run_update(dry_run: bool = False) -> tuple:
    """执行完整的数据更新流程（可被 Streamlit 看板调用）。

    返回:
        (updated: int, skipped: int, errors: int, report: str)
    """
    source = AkShareSource()
    parquet = ParquetStore()
    freshness = DataFreshness()

    # 1. 拉取最新板块行情
    updated, skipped, sector_errors = update_all_sectors(
        source, parquet, freshness, dry_run=dry_run
    )
    # 2. 同步更新 RS 依赖的基准指数及其新鲜度记录
    benchmark_updated, benchmark_errors = update_benchmarks(
        source, parquet, freshness, dry_run=dry_run
    )
    errors = sector_errors + benchmark_errors

    if dry_run:
        return updated, skipped, errors, "预览模式，未写入任何数据"

    if updated > 0 or benchmark_updated > 0:
        # 清除状态快照，Dashboard 下次刷新时自动重新计算
        invalidate_snapshots()
    else:
        logger.info("板块与基准指数均无新增数据，跳过快照清除")

    # 3. 生成数据新鲜度报告
    report = freshness.generate_report()
    logger.info("\n" + report)
    return updated, skipped, errors, report


def main():
    """主流程（CLI 入口）"""
    import argparse
    parser = argparse.ArgumentParser(description="每日增量数据更新")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，只检查不写入")
    args = parser.parse_args()

    run_update(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

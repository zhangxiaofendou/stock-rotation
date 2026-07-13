"""
指标批量计算脚本
================
一次性计算所有131个板块的RS指标和价格趋势，保存到Parquet。

用法:
    cd /workspace/stock-rotation
    PYTHONPATH=. python -m indicators.calc_all

输出目录:
    data/storage/parquet/indicators/
      - rs/            RS指标
      - trend/         价格趋势
      - crowding/      拥挤度
"""

import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from config.logger import get_logger
from config.settings import PARQUET_DIR
from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from indicators.relative_strength import RelativeStrength
from indicators.price_trend import PriceTrend
from indicators.crowding import CrowdingIndicator

logger = get_logger(__name__)

# 输出目录
OUTPUT_DIR = PARQUET_DIR / "indicators"
RS_DIR = OUTPUT_DIR / "rs"
TREND_DIR = OUTPUT_DIR / "trend"
CROWDING_DIR = OUTPUT_DIR / "crowding"


def ensure_output_dirs():
    """确保输出目录存在"""
    for d in [OUTPUT_DIR, RS_DIR, TREND_DIR, CROWDING_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def calc_all_rs_indicators(parquet_store, sqlite_store):
    """
    批量计算所有板块的RS指标

    参数:
        parquet_store: ParquetStore 实例
        sqlite_store: SQLiteStore 实例
    """
    logger.info("=" * 60)
    logger.info("开始批量计算RS指标")
    logger.info("=" * 60)

    rs = RelativeStrength(parquet_store, sqlite_store)
    results = rs.calc_all_sectors_rs(window=250, lookback=5)

    success_count = 0
    for sector_code, df in results.items():
        try:
            safe_code = sector_code.replace(".", "_")
            output_path = RS_DIR / f"{safe_code}.parquet"
            df.to_parquet(output_path, index=False)
            success_count += 1
            logger.debug(f"保存RS指标: {sector_code} -> {output_path}")
        except Exception as e:
            logger.error(f"保存RS指标失败 {sector_code}: {e}")

    logger.info(f"RS指标批量计算完成: 成功保存 {success_count}/{len(results)} 个板块")
    return results


def calc_all_trend_indicators(parquet_store, sqlite_store):
    """
    批量计算所有板块的价格趋势

    参数:
        parquet_store: ParquetStore 实例
        sqlite_store: SQLiteStore 实例
    """
    logger.info("=" * 60)
    logger.info("开始批量计算价格趋势")
    logger.info("=" * 60)

    trend = PriceTrend(parquet_store, sqlite_store)
    benchmark_map = sqlite_store.get_benchmark_map()

    if benchmark_map.empty:
        logger.error("未找到板块-基准映射数据")
        return

    success_count = 0
    fail_count = 0
    skip_count = 0

    for _, row in benchmark_map.iterrows():
        sector_code = row["sector_code"]
        try:
            if not parquet_store.index_hist_exists(sector_code):
                logger.debug(f"板块 {sector_code} 数据不存在，跳过")
                skip_count += 1
                continue

            df = trend.calc_trend_series(sector_code)
            if df is not None and not df.empty:
                safe_code = sector_code.replace(".", "_")
                output_path = TREND_DIR / f"{safe_code}.parquet"
                df.to_parquet(output_path, index=False)
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            logger.error(f"计算板块 {sector_code} 趋势异常: {e}")
            fail_count += 1

    logger.info(f"价格趋势批量计算完成: 成功={success_count}, 失败={fail_count}, 跳过={skip_count}")


def calc_all_crowding_indicators(parquet_store, sqlite_store):
    """
    批量计算所有板块的拥挤度指标

    参数:
        parquet_store: ParquetStore 实例
        sqlite_store: SQLiteStore 实例
    """
    logger.info("=" * 60)
    logger.info("开始批量计算拥挤度指标")
    logger.info("=" * 60)

    crowding = CrowdingIndicator(parquet_store, sqlite_store)
    benchmark_map = sqlite_store.get_benchmark_map()

    if benchmark_map.empty:
        logger.error("未找到板块-基准映射数据")
        return

    success_count = 0
    fail_count = 0
    skip_count = 0

    for _, row in benchmark_map.iterrows():
        sector_code = row["sector_code"]
        try:
            if not parquet_store.index_hist_exists(sector_code):
                logger.debug(f"板块 {sector_code} 数据不存在，跳过")
                skip_count += 1
                continue

            df = crowding.calc_crowding_score(sector_code)
            if df is not None and not df.empty:
                safe_code = sector_code.replace(".", "_")
                output_path = CROWDING_DIR / f"{safe_code}.parquet"
                df.to_parquet(output_path, index=False)
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            logger.error(f"计算板块 {sector_code} 拥挤度异常: {e}")
            fail_count += 1

    logger.info(f"拥挤度批量计算完成: 成功={success_count}, 失败={fail_count}, 跳过={skip_count}")


def main():
    """主入口"""
    logger.info("=" * 60)
    logger.info("P1 指标批量计算开始")
    logger.info("=" * 60)

    # 初始化存储引擎
    parquet_store = ParquetStore()
    sqlite_store = SQLiteStore()

    # 确保输出目录存在
    ensure_output_dirs()

    # 1. 计算RS指标
    calc_all_rs_indicators(parquet_store, sqlite_store)

    # 2. 计算价格趋势
    calc_all_trend_indicators(parquet_store, sqlite_store)

    # 3. 计算拥挤度
    calc_all_crowding_indicators(parquet_store, sqlite_store)

    logger.info("=" * 60)
    logger.info("P1 指标批量计算全部完成！")
    logger.info(f"输出目录: {OUTPUT_DIR}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

"""
数据初始化脚本
==============
一键初始化数据层：
1. 获取申万一级/二级分类，存入 SQLite
2. 获取交易日历，存入 SQLite
3. 批量拉取 131 个二级板块指数历史数据，存入 Parquet
4. 获取基准指数（沪深300/中证500/中证1000）历史数据
5. 记录数据新鲜度

用法:
    cd /workspace/stock-rotation
    python -m data.init_data
"""

import sys
import time
from pathlib import Path

# 将项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from config.logger import get_logger
from config.settings import (
    BACKTEST_CONFIG, BENCHMARK_INDEXES, BATCH_SIZE, BATCH_SLEEP,
)
from config.sector_map import (
    SW_LEVEL1_MAP, SW_LEVEL2_MAP, SECTOR_GROUPS,
    SW_LEVEL1_BENCHMARK, SW_LEVEL2_BENCHMARK,
)
from data.sources.akshare_source import AkShareSource
from data.storage.sqlite_store import SQLiteStore
from data.storage.parquet_store import ParquetStore
from data.freshness import DataFreshness
from data.calendar import TradeCalendar

logger = get_logger(__name__)


def step1_init_sectors(source: AkShareSource, store: SQLiteStore):
    """
    步骤1：初始化申万行业分类
    - 从 AkShare 获取一级行业分类
    - 从配置文件获取二级行业映射
    - 存入 SQLite
    """
    logger.info("=" * 60)
    logger.info("步骤1：初始化申万行业分类")
    logger.info("=" * 60)

    # 1.1 申万一级行业（31个）
    logger.info("1.1 初始化申万一级行业...")
    level1_data = []
    for code, name in SW_LEVEL1_MAP.items():
        level1_data.append((code, name, 1, None, None, None))

    store.insert_sectors_batch(level1_data)
    logger.info(f"已写入 {len(level1_data)} 个申万一级行业")

    # 1.2 申万二级行业（131个）
    logger.info("1.2 初始化申万二级行业...")
    level2_data = []
    for code, (name, parent_code, parent_name) in SW_LEVEL2_MAP.items():
        level2_data.append((code, name, 2, parent_code, parent_name, None))

    store.insert_sectors_batch(level2_data)
    logger.info(f"已写入 {len(level2_data)} 个申万二级行业")

    # 1.3 板块分组（6组）
    logger.info("1.3 初始化板块分组...")
    group_data = []
    for group_name, group_info in SECTOR_GROUPS.items():
        for code in group_info["level2_codes"]:
            group_data.append((group_name, code, group_info["description"]))

    store.insert_sector_groups_batch(group_data)
    logger.info(f"已写入 {len(group_data)} 条板块分组")

    # 1.4 基准映射
    logger.info("1.4 初始化基准映射...")
    benchmark_data = []
    # 一级行业基准
    for code, benchmark_code in SW_LEVEL1_BENCHMARK.items():
        benchmark_name = BENCHMARK_INDEXES.get(benchmark_code, benchmark_code)
        benchmark_data.append((code, benchmark_code, benchmark_name))

    # 二级行业基准
    for code, benchmark_code in SW_LEVEL2_BENCHMARK.items():
        benchmark_name = BENCHMARK_INDEXES.get(benchmark_code, benchmark_code)
        benchmark_data.append((code, benchmark_code, benchmark_name))

    store.insert_benchmark_map_batch(benchmark_data)
    logger.info(f"已写入 {len(benchmark_data)} 条基准映射")


def step2_init_calendar(source: AkShareSource, store: SQLiteStore):
    """
    步骤2：初始化交易日历
    """
    logger.info("=" * 60)
    logger.info("步骤2：初始化交易日历")
    logger.info("=" * 60)

    calendar = TradeCalendar(source=source, store=store)
    success = calendar.fetch_and_store(
        start_year=2000,
        end_year=2030,
    )
    if success:
        trade_dates = store.get_trade_dates()
        logger.info(f"交易日历初始化完成，共 {len(trade_dates)} 个交易日")
    else:
        logger.error("交易日历初始化失败")


def step3_fetch_index_hist(source: AkShareSource, parquet: ParquetStore,
                           freshness: DataFreshness):
    """
    步骤3：批量拉取申万二级板块指数历史数据
    """
    logger.info("=" * 60)
    logger.info("步骤3：批量拉取板块指数历史数据")
    logger.info("=" * 60)

    level2_codes = list(SW_LEVEL2_MAP.keys())
    total = len(level2_codes)
    success_count = 0
    fail_count = 0
    failed_codes = []

    logger.info(f"共需拉取 {total} 个二级板块")

    for i, code in enumerate(level2_codes):
        name = SW_LEVEL2_MAP[code][0]
        logger.info(f"[{i+1}/{total}] 拉取 {code} {name}...")

        try:
            df = source.get_sw_index_hist(symbol=code, period="day")
            if df is not None and not df.empty:
                parquet.save_index_hist(code, df)

                # 记录数据范围
                date_col = None
                for col in ["date", "日期", "trade_date"]:
                    if col in df.columns:
                        date_col = col
                        break

                data_start = str(df[date_col].min()) if date_col else None
                data_end = str(df[date_col].max()) if date_col else None

                freshness.record_update(
                    data_type="sector_hist",
                    data_key=code,
                    data_start=data_start,
                    data_end=data_end,
                    record_count=len(df),
                    status="ok",
                )
                success_count += 1
            else:
                logger.warning(f"板块 {code} 返回空数据")
                fail_count += 1
                failed_codes.append(code)
                freshness.record_update(
                    data_type="sector_hist",
                    data_key=code,
                    status="error",
                )
        except Exception as e:
            logger.error(f"板块 {code} 拉取失败: {e}")
            fail_count += 1
            failed_codes.append(code)
            freshness.record_update(
                data_type="sector_hist",
                data_key=code,
                status="error",
            )

        # 批次间休眠
        if (i + 1) % BATCH_SIZE == 0:
            logger.info(f"已处理 {i+1}/{total}, 批次间休眠 {BATCH_SLEEP}秒...")
            time.sleep(BATCH_SLEEP)

    logger.info(f"板块历史数据拉取完成: 成功 {success_count}, 失败 {fail_count}")
    if failed_codes:
        logger.warning(f"失败的板块: {failed_codes}")


def step4_fetch_benchmarks(source: AkShareSource, parquet: ParquetStore,
                           freshness: DataFreshness):
    """
    步骤4：获取基准指数历史数据
    - 沪深300 (000300)
    - 中证500 (000905)
    - 中证1000 (000852)
    - 上证50 (000016)
    - 创业板指 (399006)
    """
    logger.info("=" * 60)
    logger.info("步骤4：获取基准指数历史数据")
    logger.info("=" * 60)

    # 基准指数代码（需带sh/sz前缀用于AkShare stock_zh_index_daily接口）
    benchmarks = {
        "sh000300": "沪深300",
        "sh000905": "中证500",
        "sh000852": "中证1000",
        "sh000016": "上证50",
        "sz399006": "创业板指",
    }

    for code, name in benchmarks.items():
        logger.info(f"拉取基准指数: {name} ({code})...")
        try:
            df = source.get_benchmark_hist(symbol=code)
            if df is not None and not df.empty:
                parquet.save_benchmark_hist(code, df)

                date_col = None
                for col in ["date", "日期"]:
                    if col in df.columns:
                        date_col = col
                        break

                data_start = str(df[date_col].min()) if date_col else None
                data_end = str(df[date_col].max()) if date_col else None

                freshness.record_update(
                    data_type="benchmark_hist",
                    data_key=code,
                    data_start=data_start,
                    data_end=data_end,
                    record_count=len(df),
                    status="ok",
                )
                logger.info(f"基准指数 {name} 拉取成功, 共 {len(df)} 条")
            else:
                logger.warning(f"基准指数 {name} 返回空数据")
                freshness.record_update(
                    data_type="benchmark_hist",
                    data_key=code,
                    status="error",
                )
        except Exception as e:
            logger.error(f"基准指数 {name} 拉取失败: {e}")
            freshness.record_update(
                data_type="benchmark_hist",
                data_key=code,
                status="error",
            )

        time.sleep(1)  # 避免频率限制


def main():
    """主初始化流程"""
    logger.info("=" * 60)
    logger.info("A股板块轮动分析系统 - P0数据层初始化")
    logger.info("=" * 60)
    logger.info(f"项目根目录: {PROJECT_ROOT}")
    logger.info(f"回测起始日期: {BACKTEST_CONFIG['start_date']}")

    # 初始化各组件
    source = AkShareSource()
    store = SQLiteStore()
    parquet = ParquetStore()
    freshness = DataFreshness(store=store)

    try:
        # 步骤1：行业分类
        step1_init_sectors(source, store)

        # 步骤2：交易日历
        step2_init_calendar(source, store)

        # 步骤3：板块指数历史数据
        step3_fetch_index_hist(source, parquet, freshness)

        # 步骤4：基准指数历史数据
        step4_fetch_benchmarks(source, parquet, freshness)

        # 生成数据新鲜度报告
        logger.info("=" * 60)
        logger.info("生成数据新鲜度报告...")
        report = freshness.generate_report()
        logger.info("\n" + report)

        logger.info("=" * 60)
        logger.info("P0数据层初始化完成！")
        logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.warning("用户中断初始化")
    except Exception as e:
        logger.error(f"初始化过程异常: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()

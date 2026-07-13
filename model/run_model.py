"""
运行模型层，计算所有板块的当前状态、镜像对、评分
==================================================
用法: PYTHONPATH=. python -m model.run_model
"""

import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from config.logger import get_logger
from config.sector_map import get_sector_name
from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore

from model.state_machine import StateMachine
from model.transition import TransitionRules
from model.priority import PriorityRules
from model.mirror_pair import MirrorPair
from model.scoring import SectorScoring
from model.arbitrate import SignalArbitrator
from model.circuit_breaker import CircuitBreaker

logger = get_logger(__name__)


def run_model():
    """运行模型层主流程"""
    logger.info("=" * 60)
    logger.info("开始运行P2模型层")
    logger.info("=" * 60)

    # 初始化存储引擎
    parquet_store = ParquetStore()
    sqlite_store = SQLiteStore()

    # ============================================================
    # 1. 初始化各模型模块
    # ============================================================
    state_machine = StateMachine(
        parquet_store=parquet_store,
        sqlite_store=sqlite_store,
    )
    transition_rules = TransitionRules()
    priority_rules = PriorityRules()
    mirror_pair = MirrorPair(
        sqlite_store=sqlite_store,
        state_machine=state_machine,
    )
    scoring = SectorScoring(
        parquet_store=parquet_store,
        sqlite_store=sqlite_store,
        state_machine=state_machine,
    )
    arbitrator = SignalArbitrator()
    circuit_breaker = CircuitBreaker(
        parquet_store=parquet_store,
        state_machine=state_machine,
    )

    # ============================================================
    # 2. 市场环境检查（熔断机制）
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("市场环境检查（熔断机制）")
    logger.info("=" * 60)

    market_status = circuit_breaker.check_market_status()
    logger.info(f"市场模式: {market_status['mode']}")
    logger.info(f"原因: {market_status['reason']}")
    logger.info(f"下跌板块占比: {market_status['down_ratio']:.1%}")
    logger.info(f"⑦持续杀跌占比: {market_status['kill_ratio']:.1%}")

    if market_status["mode"] == "defense":
        logger.warning("市场处于防御模式，以下分析仅供参考")

    # ============================================================
    # 3. 所有板块状态分布
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("板块状态分布")
    logger.info("=" * 60)

    distribution = state_machine.get_state_distribution()
    for state, codes in distribution.items():
        names = [f"{get_sector_name(c)}({c})" for c in codes[:5]]
        if len(codes) > 5:
            names.append(f"...共{len(codes)}个")
        logger.info(f"  {state}: {', '.join(names)}")

    # ============================================================
    # 4. 镜像对识别
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("镜像对识别")
    logger.info("=" * 60)

    mirror_pairs = mirror_pair.find_mirror_pairs()
    if mirror_pairs:
        for mp in mirror_pairs:
            logger.info(
                f"  [{mp['pair_type']}] {mp['group']}: "
                f"{mp['strong_name']}({mp['strong_state']}) ↔ "
                f"{mp['weak_name']}({mp['weak_state']}), "
                f"置信度={mp['confidence']:.2f}"
            )
    else:
        logger.info("  未发现镜像对")

    # ============================================================
    # 5. 板块综合评分 Top 10
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("板块综合评分 Top 10")
    logger.info("=" * 60)

    top_sectors = scoring.get_top_sectors(n=10)
    if top_sectors is not None:
        for _, row in top_sectors.iterrows():
            logger.info(
                f"  #{int(row['rank'])} {row['sector_name']}({row['sector_code']}) "
                f"评分={row['score']:.1f} 状态={row['state']}"
            )

    # ============================================================
    # 6. 买入/卖出信号
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("状态转换信号")
    logger.info("=" * 60)

    buy_signals = transition_rules.get_buy_signals()
    logger.info(f"买入信号路径共 {len(buy_signals)} 条:")
    for s in buy_signals:
        logger.info(f"  {s['from']} → {s['to']}: {s['action']} ({s['logic']})")

    sell_signals = transition_rules.get_sell_signals()
    logger.info(f"\n卖出信号路径共 {len(sell_signals)} 条:")
    for s in sell_signals:
        logger.info(f"  {s['from']} → {s['to']}: {s['action']} ({s['logic']})")

    # ============================================================
    # 7. 所有72条转换路径验证
    # ============================================================
    logger.info("\n" + "=" * 60)
    logger.info("72条转换路径完整性验证")
    logger.info("=" * 60)

    all_transitions = transition_rules.get_all_transitions()
    logger.info(f"转换路径总数: {len(all_transitions)}")

    # 验证9x8=72条（不包括自身到自身）
    expected = 9 * 8
    if len(all_transitions) == expected:
        logger.info("72条转换路径完整")
    else:
        logger.warning(f"路径数量异常: 期望{expected}, 实际{len(all_transitions)}")

    logger.info("\n" + "=" * 60)
    logger.info("P2模型层运行完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    run_model()

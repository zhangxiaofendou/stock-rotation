"""
回测引擎包
==========
事件驱动回测框架，严格遵循 PRD §6.1 原则：
  - T+1 撮合：信号日盘后决策，次日开盘价执行，不用信号日收盘价（无未来函数）
  - 双边交易成本：默认 0.15%
  - 板块指数层回测（阶段一），与真实持仓账本完全隔离
  - 决策只用当时及之前的数据

子模块：
  - engine.py      事件驱动框架（核心）
  - strategies/    策略1动量 / 策略2九宫格 / 策略3镜像对
  - metrics.py     评估指标
  - experiments.py 实验记录（JSON）
  - replay.py      信号回放（只读历史视图）
"""

__all__ = ["engine", "metrics", "experiments", "replay", "strategies"]

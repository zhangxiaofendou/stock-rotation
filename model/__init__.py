"""
P2 模型层
========
板块轮动分析模型层，包含：
  - StateMachine: 九宫格状态机
  - TransitionRules: 72条转换路径与动作
  - PriorityRules: 状态机优先级规则
  - MirrorPair: 板块对镜像识别
  - SectorScoring: 板块综合评分
  - SignalArbitrator: 信号仲裁机制
  - CircuitBreaker: 市场环境熔断机制
"""

from .state_machine import StateMachine
from .transition import TransitionRules
from .priority import PriorityRules
from .mirror_pair import MirrorPair
from .scoring import SectorScoring
from .arbitrate import SignalArbitrator
from .circuit_breaker import CircuitBreaker

__all__ = [
    "StateMachine",
    "TransitionRules",
    "PriorityRules",
    "MirrorPair",
    "SectorScoring",
    "SignalArbitrator",
    "CircuitBreaker",
]

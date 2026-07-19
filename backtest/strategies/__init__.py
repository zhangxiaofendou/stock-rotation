"""回测策略集合。

每个策略暴露 ``build(engine, **params) -> Dict[Timestamp, Dict[str, float]]``，
返回"决策日 → 目标权重"映射；引擎负责 T+1 开盘执行，保证无未来函数。
"""

from .momentum import MomentumStrategy
from .state_machine import NineGridStrategy
from .mirror_pair import MirrorPairStrategy

__all__ = ["MomentumStrategy", "NineGridStrategy", "MirrorPairStrategy"]

"""
东财行业宇宙加载器
==================
负责把「东方财富行业板块清单」喂给 config.sector_map，构建系统板块宇宙。

获取顺序（确保云端/离线都可用）：
  1. 本地缓存 JSON（config/em_industry_universe.json）—— 首次成功拉取后写入，
     之后离线或东财接口抖动时直接复用，避免空宇宙。
  2. 数据源实时拉取（EastMoneyLiveSource.get_em_industry_list）—— 云端可用，
     返回 {BKxxxx: 名称}。
  3. 兜底：若以上皆空，保留缓存（或空），并打日志警示。

调用时机（任一入口首步）：
  - 看板启动（dashboard/app.py）
  - 每日管线（data/daily_pipeline.py / data/daily_update.py）
  - 一键初始化（data/init_data.py）
"""
import json
import os
import logging

logger = logging.getLogger("stock_rotation")

# config/em_industry_universe.json 相对本文件的位置
CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "em_industry_universe.json",
)


def _load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return data
        except Exception as e:
            logger.warning("读取东财行业缓存失败: %s", e)
    return {}


def _save_cache(em_map: dict):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(em_map, f, ensure_ascii=False, indent=2)
        logger.info("东财行业清单已缓存 (%d 个): %s", len(em_map), CACHE_PATH)
    except Exception as e:
        logger.warning("缓存东财行业清单失败: %s", e)


def ensure_em_industry_map(source, force: bool = False) -> dict:
    """确保板块宇宙已就绪：拉取清单并填充 config.sector_map。

    参数:
        source: 任意 BaseDataSource 实例（需实现 get_em_industry_list；
                东方财富源直接拉，回退 AkShare）。
        force: 为 True 时忽略缓存，强制实时拉取。
    返回:
        东财行业清单 {BKxxxx: 名称}（可能为空 dict，表示拉取失败）。
    """
    from config import sector_map

    cached = _load_cache() if not force else {}
    em_map = None

    # 缓存为空或明显不完整（行业数过少）时，优先实时拉取，避免被残缺种子卡死。
    if not cached or len(cached) < 50:
        try:
            em_map = source.get_em_industry_list()
        except Exception as e:
            logger.warning("东财行业清单实时拉取异常: %s", e)
            em_map = None
        if em_map:
            _save_cache(em_map)
    if not em_map:
        # 实时失败/跳过 → 退回缓存（即便 force 也用缓存兜底）
        em_map = cached

    if not em_map:
        logger.error(
            "东财行业清单为空：无法构建板块宇宙。请检查网络（云端）或缓存文件 %s",
            CACHE_PATH,
        )
    else:
        logger.info("板块宇宙就绪：%d 个东财行业板块", len(em_map))

    sector_map.refresh_em_universe(em_map)
    return em_map

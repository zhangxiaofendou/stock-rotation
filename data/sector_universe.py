"""
同花顺行业宇宙加载器
==================
负责把「同花顺行业板块清单」喂给 config.sector_map，构建系统板块宇宙。

获取顺序（确保云端/离线都可用）：
  1. 本地缓存 JSON（config/em_industry_universe.json）—— 首次成功拉取后写入，
     之后离线或同花顺接口抖动时直接复用，避免空宇宙。
  2. 数据源实时拉取（THSDataSource.get_em_industry_list）—— 云端可用，
     返回 {881xxx: 名称}。
  3. 兜底：若以上皆空，启用静态兜底映射（90 个 881xxx 行业），保证宇宙不空。

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
            logger.warning("读取行业清单缓存失败: %s", e)
    return {}


def _save_cache(em_map: dict):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(em_map, f, ensure_ascii=False, indent=2)
        logger.info("行业清单已缓存 (%d 个): %s", len(em_map), CACHE_PATH)
    except Exception as e:
        logger.warning("缓存行业清单失败: %s", e)


def _is_cache_compatible(source, cached: dict) -> bool:
    """检查缓存代码格式是否与当前数据源匹配，防止切源后旧缓存误导宇宙。"""
    if not cached:
        return True
    cls_name = source.__class__.__name__
    sample_keys = list(cached.keys())[:10]
    if cls_name == "THSDataSource":
        # 同花顺行业代码为 6 位数字 881xxx
        return all(len(k) == 6 and k.isdigit() for k in sample_keys)
    if cls_name == "EastMoneyLiveSource":
        # 东方财富行业代码为 BKxxxx
        return all(k.startswith("BK") for k in sample_keys)
    if cls_name == "AkShareSource":
        # AkShare 申万代码通常为 801xxx.SI / 801xxx
        return all((k.startswith("801") and k.endswith(".SI")) or k.startswith("801") for k in sample_keys)
    return True


def ensure_em_industry_map(source, force: bool = False) -> dict:
    """确保同花顺板块宇宙已就绪：拉取清单并填充 config.sector_map。

    参数:
        source: 任意 BaseDataSource 实例（需实现 get_em_industry_list）。
        force: 为 True 时忽略缓存，强制实时拉取。
    返回:
        行业清单 {code: 名称}（可能为空 dict，表示拉取失败）。
    """
    from config import sector_map

    cached = _load_cache() if not force else {}
    if cached and not _is_cache_compatible(source, cached):
        logger.warning(
            "检测到缓存代码格式与当前数据源 %s 不匹配（样例 %s），忽略旧缓存",
            source.__class__.__name__, list(cached.keys())[:5]
        )
        cached = {}

    em_map = None

    # 缓存为空或明显不完整（行业数过少）时，优先实时拉取，避免被残缺种子卡死。
    if not cached or len(cached) < 50:
        try:
            em_map = source.get_em_industry_list()
        except Exception as e:
            logger.warning("行业清单实时拉取异常: %s", e)
            em_map = None
        if em_map:
            _save_cache(em_map)
    if not em_map:
        # 实时失败/跳过 → 退回缓存（即便 force 也用缓存兜底）
        em_map = cached

    if not em_map:
        logger.error(
            "同花顺行业清单为空：无法构建板块宇宙。请检查网络（云端）或缓存文件 %s",
            CACHE_PATH,
        )
    else:
        logger.info("板块宇宙就绪：%d 个行业板块", len(em_map))

    sector_map.refresh_em_universe(em_map)

    # 清理旧数据源留下的 sector_hist 新鲜度记录，避免 dashboard 汇总仍显示 7.30
    try:
        from data.storage.sqlite_store import SQLiteStore
        store = SQLiteStore()
        fresh_df = store.get_freshness_report()
        if not fresh_df.empty:
            stale_keys = fresh_df.loc[
                (fresh_df["data_type"] == "sector_hist") &
                (~fresh_df["data_key"].isin(em_map.keys())),
                "data_key"
            ].tolist()
            for key in stale_keys:
                store.delete_freshness("sector_hist", key)
            if stale_keys:
                logger.info("清理旧 sector_hist 新鲜度记录 %d 条", len(stale_keys))
    except Exception as e:  # noqa: BLE001
        logger.warning("清理旧 sector_hist 新鲜度记录失败: %s", e)

    return em_map

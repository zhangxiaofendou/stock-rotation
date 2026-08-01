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


def _purge_legacy_sectors(store: "SQLiteStore", valid_codes: set):
    """删除不在当前板块宇宙中的旧板块（如申万 801xxx）及其子表 orphan 行。

    切源后这些旧代码仍残留在 sectors / signal_events / signal_performance 等表，
    会污染下游指标计算与看板汇总，且会被遍历用来加载 K 线（日志里大量
    `801xxx.SI 历史数据文件不存在` 即源于此）。

    由于外键已开启（PRAGMA foreign_keys=ON），必须先删子表再删父表。
    仅在 valid_codes 非空时执行，避免误删全部（em_map 为空时不应清理）。
    """
    if not valid_codes:
        return
    try:
        conn = store._get_conn()
        try:
            legacy = [r[0] for r in conn.execute(
                "SELECT code FROM sectors WHERE code NOT IN (%s)" % ",".join("?" * len(valid_codes)),
                tuple(valid_codes),
            ).fetchall()]
            if not legacy:
                return
            placeholders = ",".join("?" * len(legacy))
            # 先清子表 orphan 行（外键开启，先子后父）
            for tbl in ("signal_performance", "signal_events", "sector_stocks",
                        "benchmark_map", "sector_groups"):
                try:
                    conn.execute(
                        "DELETE FROM %s WHERE sector_code IN (%s)" % (tbl, placeholders),
                        tuple(legacy),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug("清理旧板块子表 %s 失败(可忽略): %s", tbl, e)
            conn.execute("DELETE FROM sectors WHERE code IN (%s)" % placeholders, tuple(legacy))
            conn.commit()
            logger.info("已清理旧板块残留 %d 个（含申万 801xxx）及其子表 orphan 行", len(legacy))
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("清理旧板块残留失败（非致命）: %s", e)


def _sync_sector_meta_to_sqlite(em_map: dict):
    """把当前板块宇宙同步到 SQLite 元数据表（sectors / benchmark_map / sector_groups）。

    切源后旧代码会残留在这些表里，导致 indicators.calc_all 用错代码生成 parquet，
    进而状态机无法匹配 SECTOR_GROUPS。因此每次宇宙刷新后都同步一次。
    """
    if not em_map:
        return
    try:
        from config.sector_map import (
            SW_LEVEL1_MAP, SW_LEVEL2_MAP, SECTOR_GROUPS,
            SW_LEVEL1_BENCHMARK, SW_LEVEL2_BENCHMARK,
        )
        from config.settings import BENCHMARK_INDEXES
        from data.storage.sqlite_store import SQLiteStore

        store = SQLiteStore()
        # 0) 先清理不在当前板块宇宙中的旧板块（如申万 801xxx）及其子表 orphan 行，
        #    避免残留污染下游指标计算与看板汇总；外键开启，先删子表再删父表。
        valid_codes = set(SW_LEVEL1_MAP.keys()) | set(SW_LEVEL2_MAP.keys())
        _purge_legacy_sectors(store, valid_codes)
        # 1) sectors 表（幂等插入；旧 801xxx.SI 已由上面的清理步骤移除，不再残留）
        rows = []
        for code, name in SW_LEVEL1_MAP.items():
            rows.append((code, name, 1, None, None, None))
        for code, (name, parent_code, parent_name) in SW_LEVEL2_MAP.items():
            rows.append((code, name, 2, parent_code, parent_name, None))
        store.insert_sectors_batch(rows)

        # 2) benchmark_map 表（先清空旧记录，避免 801xxx.SI 残留）
        store.clear_benchmark_map()
        mappings = []
        for code, benchmark_code in SW_LEVEL1_BENCHMARK.items():
            mappings.append((code, benchmark_code, BENCHMARK_INDEXES.get(benchmark_code, benchmark_code)))
        for code, benchmark_code in SW_LEVEL2_BENCHMARK.items():
            mappings.append((code, benchmark_code, BENCHMARK_INDEXES.get(benchmark_code, benchmark_code)))
        store.insert_benchmark_map_batch(mappings)

        # 3) sector_groups 表（先清空旧记录）
        store.clear_sector_groups()
        groups = []
        for group_name, group_info in SECTOR_GROUPS.items():
            for code in group_info["level2_codes"]:
                groups.append((group_name, code, group_info["description"]))
        store.insert_sector_groups_batch(groups)

        logger.info(
            "板块元数据已同步: sectors=%d, benchmark_map=%d, sector_groups=%d",
            len(rows), len(mappings), len(groups)
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("同步板块元数据到 SQLite 失败: %s", e)


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

    # 切源后同步 SQLite 元数据，避免旧 801xxx.SI 记录污染下游指标计算
    _sync_sector_meta_to_sqlite(em_map)

    # 清理旧数据源留下的确认因子记录，避免资金流向/分化度页面显示过期代码
    try:
        from data.storage.sqlite_store import SQLiteStore
        store = SQLiteStore()
        n_ff = store.delete_sector_fund_flow_not_in(list(em_map.keys()))
        n_dv = store.delete_sector_divergence_not_in(list(em_map.keys()))
        if n_ff or n_dv:
            logger.info("清理旧确认因子记录: sector_fund_flow=%d, sector_divergence=%d", n_ff, n_dv)
    except Exception as e:  # noqa: BLE001
        logger.warning("清理旧确认因子记录失败: %s", e)

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

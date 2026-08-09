"""板块派生 parquet 的云端镜像（Storage as DB blob）
=================================================

为什么需要它
------------
Streamlit Cloud 的临时磁盘在每次 Reboot / redeploy 后被清空，所有
``data/storage/parquet/*.parquet``（板块 K 线 / RS / 趋势等派生数据）会丢失。
此前的恢复方式是：自动启动后台全量管线（5–10 分钟）重新拉取 + 重算。
体感上就是「Reboot 后数据退回旧日期，等好几分钟才恢复」。

本模块把最新派生 parquet 作为 ``bytea`` 落地到已连的 Supabase Postgres
（复用 pg_store 的现有连接，无需引入 supabase-py），形成一份「云端快照」：

  - 管线跑完 → ``upload_parquet_mirror()`` 把三个 seed 目录镜像进 ``parquet_mirror`` 表。
  - app 启动 → ``restore_parquet_from_mirror()`` 先把云端快照下载到本地，
    再交给原有的 ``_maybe_auto_refresh_on_stale`` 判缺补漏。

效果：Reboot 后本地磁盘清空，app 启动瞬间从云库拉回最新基数据（约 10 秒），
原自动刷新逻辑只需补「当天缺口」（甚至 mirror 已是最新时直接跳过），
恢复时间从 5–10 分钟降到秒级。

设计约束
--------
  - **复用 pg_store 连接**：未配置 DATABASE_URL（本地开发机）时所有函数为 no-op，
    不影响本地既有行为。
  - **防御式**：任何 DB 异常都仅 warning，绝不抛出阻断页面渲染或管线。
  - **零额外开销（正常运行时）**：restore 只对「本地不存在的文件」下载，
    Reboot 后本地全空 → 全下载；正常运行本地都在 → 0 下载。
  - **仅镜像 seed 目录**：index_hist / indicators/rs / indicators/trend。
    cache/state_snapshot（含会话态）、fund_flow（运行时生成）不镜像，避免状态混乱。
"""

import os
import logging
from pathlib import Path

from config.settings import PARQUET_DIR

logger = logging.getLogger(__name__)

# 需要镜像的 seed 子目录（相对 PARQUET_DIR）
MIRROR_SUBDIRS = ("index_hist", "indicators/rs", "indicators/trend")

# 单文件大小上限（防御：避免异常大文件撑爆云库单行 bytea）
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

_SCHEMA_MIRROR = """
CREATE TABLE IF NOT EXISTS parquet_mirror (
    name       TEXT PRIMARY KEY,        -- 相对 PARQUET_DIR 的路径，如 index_hist/881101.parquet
    data       BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _enabled():
    try:
        from data.storage import pg_store
        return pg_store.is_enabled()
    except Exception:
        return False


def _connect():
    from data.storage import pg_store
    return pg_store.connect()


def ensure_mirror_table(conn):
    """幂等建表（mirror 表）。"""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_MIRROR)
    conn.commit()


def _iter_mirror_files():
    """遍历三个 seed 目录，生成 (rel_path, abs_path) 序列。"""
    for sub in MIRROR_SUBDIRS:
        d = PARQUET_DIR / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.parquet")):
            if f.is_file():
                rel = f.relative_to(PARQUET_DIR).as_posix()
                yield rel, f


def upload_parquet_mirror():
    """把本地最新派生 parquet 镜像进云库。应在每日管线跑完后调用。

    无 DATABASE_URL（本地开发机）时直接返回，不报错。
    任何异常仅 warning，不阻断管线收尾。
    """
    if not _enabled():
        logger.debug("parquet_mirror: 未启用 Postgres，跳过上传。")
        return
    try:
        conn = _connect()
        try:
            ensure_mirror_table(conn)
            uploaded = skipped = 0
            for rel, f in _iter_mirror_files():
                try:
                    size = f.stat().st_size
                    if size == 0 or size > MAX_FILE_BYTES:
                        logger.warning("parquet_mirror: 跳过异常文件 %s (size=%d)", rel, size)
                        skipped += 1
                        continue
                    data = f.read_bytes()
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO parquet_mirror (name, data, updated_at) "
                            "VALUES (%s, %s, now()) "
                            "ON CONFLICT (name) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                            (rel, data),
                        )
                    uploaded += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("parquet_mirror: 上传 %s 失败: %s", rel, e)
            conn.commit()
            logger.info("parquet_mirror: 上传完成（%d 成功, %d 跳过）", uploaded, skipped)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("parquet_mirror: 上传整体失败（不影响主管线）: %s", e)


def restore_parquet_from_mirror():
    """app 启动早期调用：把云库快照下载到本地（仅当本地该文件不存在）。

    正常运行时本地文件都在 → 0 下载；Reboot 后本地全空 → 全下载（秒级）。
    任何异常仅 warning，绝不阻断页面渲染。
    """
    if not _enabled():
        logger.debug("parquet_mirror: 未启用 Postgres，跳过恢复。")
        return
    try:
        conn = _connect()
        try:
            # 1) 列出云库已有的快照名
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM parquet_mirror")
                names = [r[0] for r in cur.fetchall()]
            if not names:
                logger.info("parquet_mirror: 云库无快照，跳过恢复（将走原自动刷新）。")
                return

            # 2) 仅下载本地不存在的文件
            need = []
            for name in names:
                local = PARQUET_DIR / name
                if not local.exists():
                    need.append(name)
            if not need:
                logger.debug("parquet_mirror: 本地文件齐全，无需下载。")
                return

            downloaded = 0
            for name in need:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT data FROM parquet_mirror WHERE name = %s", (name,))
                        row = cur.fetchone()
                    if not row or row[0] is None:
                        continue
                    local = PARQUET_DIR / name
                    local.parent.mkdir(parents=True, exist_ok=True)
                    # psycopg2 返回 buffer/memoryview，psycopg3 返回 bytes；统一取 bytes
                    local.write_bytes(bytes(row[0]))
                    downloaded += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("parquet_mirror: 下载 %s 失败: %s", name, e)
            logger.info("parquet_mirror: 从云库恢复基数据 %d 个文件", downloaded)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("parquet_mirror: 恢复整体失败（走原自动刷新逻辑）: %s", e)

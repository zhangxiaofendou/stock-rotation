"""parquet_mirror 自检：覆盖 no-op / 写入 / 选择性下载 / 异常吞掉。

本地开发机通常未配置 DATABASE_URL，本测试用 monkeypatch 模拟连接，
无需真连 Supabase 即可验证逻辑分支正确。
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import data.storage.parquet_mirror as pm


def _fake_conn(cur):
    """构造一个 cursor() 返回 cur 的假连接（支持 with 语法）。

    MagicMock 的 __enter__ 默认返回新 mock，会让 `with conn.cursor() as cur`
    拿到的 cur 与设置 call_args 的不是同一个对象，故显式让 __enter__ 返回自身。
    """
    conn = MagicMock()
    conn.cursor.return_value = cur
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = None
    return conn


def test_noop_without_pg():
    """无 DATABASE_URL 时 upload/restore 都是 no-op 且不抛异常。"""
    with patch.object(pm, "_enabled", return_value=False):
        pm.upload_parquet_mirror()
        pm.restore_parquet_from_mirror()


def test_upload_writes_bytea():
    """有连接时 upload 把文件写入 bytea 表，且带入文件字节。"""
    tmp = Path(tempfile.mkdtemp())
    d = tmp / "index_hist"
    d.mkdir()
    (d / "881101.parquet").write_bytes(b"fake-parquet-bytes-1234567890")

    cur = MagicMock()
    conn = _fake_conn(cur)
    with patch.object(pm, "PARQUET_DIR", tmp), \
         patch.object(pm, "_enabled", return_value=True), \
         patch.object(pm, "_connect", return_value=conn):
        pm.upload_parquet_mirror()

    inserts = [
        c for c in cur.execute.call_args_list
        if c.args and "INSERT INTO parquet_mirror" in str(c.args[0])
    ]
    assert inserts, "应有 INSERT 写入 mirror 表"
    # INSERT 参数形如 (sql, (name, data_bytes, ...))，文件字节在第二个位置
    assert inserts[0].args[1][1] == b"fake-parquet-bytes-1234567890", "参数应含文件字节"


def test_restore_downloads_missing_only():
    """restore 只下载本地不存在的文件，已存在的绝不覆盖。"""
    tmp = Path(tempfile.mkdtemp())
    d = tmp / "index_hist"
    d.mkdir()
    existing = d / "881101.parquet"
    existing.write_bytes(b"local-exists")   # 881101 本地已存在
    # 881102 本地不存在 → 应从 mirror 下载

    cur = MagicMock()
    cur.fetchall.return_value = [
        ("index_hist/881101.parquet",),
        ("index_hist/881102.parquet",),
    ]
    # 循环里只会对缺失的 881102 调一次 SELECT data
    cur.fetchone.side_effect = [(b"mirror-881102",)]

    conn = _fake_conn(cur)
    written = {}
    with patch.object(pm, "PARQUET_DIR", tmp), \
         patch.object(pm, "_enabled", return_value=True), \
         patch.object(pm, "_connect", return_value=conn), \
         patch.object(Path, "write_bytes",
                      lambda self, data: written.__setitem__(self, data)):
        pm.restore_parquet_from_mirror()

    target = tmp / "index_hist" / "881102.parquet"
    assert target in written, "本地缺失的 881102 应被下载"
    assert written[target] == b"mirror-881102"
    # 已存在的 881101 绝不被覆盖
    assert existing.read_bytes() == b"local-exists"


def test_errors_are_swallowed():
    """连接异常必须被捕获，不向上抛出。"""
    with patch.object(pm, "_enabled", return_value=True), \
         patch.object(pm, "_connect", side_effect=RuntimeError("boom")):
        pm.upload_parquet_mirror()
        pm.restore_parquet_from_mirror()


if __name__ == "__main__":
    test_noop_without_pg()
    test_upload_writes_bytea()
    test_restore_downloads_missing_only()
    test_errors_are_swallowed()
    print("OK: parquet_mirror selfcheck passed (4/4)")

"""存储后端真实状态自检。

防止「假云库」误导：配了 DATABASE_URL 但连不上时，界面必须暴露「数据不安全」，
而不是显示一个虚假的「云数据库已启用」绿勾。覆盖三种状态：
  - 未配置 DATABASE_URL        → sqlite / 不安全
  - 配置但云库连不上           → sqlite-fallback / 不安全（必须含回退提示）
  - 配置且连通                 → postgres / 安全
"""

import importlib
import os
import sys
import unittest.mock as mock

sys.path.insert(0, ".")

from data.storage import pg_store  # noqa: E402

results = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond)))
    if not cond:
        print(f"  FAIL  {name}  {detail}")


os.environ.pop("DATABASE_URL", None)
importlib.reload(pg_store)

# 1. 未配置 DATABASE_URL
check("未配置 → backend=sqlite", pg_store.storage_status()["backend"] == "sqlite")
check("未配置 → safe=False", pg_store.storage_status()["safe"] is False)

# 2. 配置了 URL 但云库连不上 → 必须暴露为「sqlite-fallback / 不安全」
os.environ["DATABASE_URL"] = "postgresql://u:p@localhost:5432/db"
importlib.reload(pg_store)
with mock.patch.object(pg_store, "_DRIVER", "psycopg3"), \
     mock.patch.object(pg_store, "healthcheck", return_value=(False, "连接失败：network is unreachable")):
    st = pg_store.storage_status()
    check("配错 → backend=sqlite-fallback", st["backend"] == "sqlite-fallback", st)
    check("配错 → safe=False", st["safe"] is False, st)
    check("配错 → 提示含「回退」", "回退" in st["detail"], st["detail"])

# 3. 配置了 URL 且连通 → postgres / 安全
with mock.patch.object(pg_store, "_DRIVER", "psycopg3"), \
     mock.patch.object(pg_store, "healthcheck", return_value=(True, "Postgres 已连通（驱动 psycopg3）")):
    st = pg_store.storage_status()
    check("连通 → backend=postgres", st["backend"] == "postgres", st)
    check("连通 → safe=True", st["safe"] is True, st)

# 4. _default_store 在「配错」时记录回退原因（不静默吞掉）
import importlib
import portfolio.holdings as holdings  # noqa: E402

os.environ["DATABASE_URL"] = "postgresql://u:p@localhost:5432/db"
importlib.reload(pg_store)
importlib.reload(holdings)
with mock.patch.object(pg_store, "_DRIVER", "psycopg3"), \
     mock.patch.object(pg_store, "PGStore", side_effect=RuntimeError("连接被拒")):
    store = holdings._default_store()
    from data.storage.sqlite_store import SQLiteStore
    check("_default_store 回退到 SQLite", isinstance(store, SQLiteStore))
    check("_default_store 记录了回退原因", pg_store._pg_fallback_reason is not None, repr(pg_store._pg_fallback_reason))

# 5. _default_store 在未配置 URL 时直接用 SQLite（不报错）
os.environ.pop("DATABASE_URL", None)
importlib.reload(pg_store)
importlib.reload(holdings)
store2 = holdings._default_store()
check("未配置 → _default_store 用 SQLite", isinstance(store2, SQLiteStore))

# 6. count_cloud_rows：未配置/连不上返回全 0（不抛异常），连通返回真实行数
class _FakeCur:
    def __init__(self, vals):
        self._vals = list(vals)
    def execute(self, sql, *a):
        pass
    def fetchone(self):
        return (self._vals.pop(0),)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

class _FakeConn:
    def __init__(self, vals):
        self._vals = vals
    def cursor(self):
        return _FakeCur(self._vals)
    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

os.environ.pop("DATABASE_URL", None)
importlib.reload(pg_store)
c0 = pg_store.count_cloud_rows()
check("未配置 → count_cloud_rows 全 0", c0 == {"users": 0, "positions": 0, "transactions": 0}, c0)

os.environ["DATABASE_URL"] = "postgresql://u:p@localhost:5432/db"
importlib.reload(pg_store)
with mock.patch.object(pg_store, "_DRIVER", "psycopg3"), \
     mock.patch.object(pg_store, "connect", return_value=_FakeConn([3, 2, 5])):
    c1 = pg_store.count_cloud_rows()
    check("连通 → count_cloud_rows 返回真实行数", c1 == {"users": 3, "positions": 2, "transactions": 5}, c1)

with mock.patch.object(pg_store, "_DRIVER", "psycopg3"), \
     mock.patch.object(pg_store, "connect", side_effect=RuntimeError("连不上")):
    c2 = pg_store.count_cloud_rows()
    check("连不上 → count_cloud_rows 返回全 0 且不抛异常", c2 == {"users": 0, "positions": 0, "transactions": 0}, c2)

passed = sum(1 for _, c in results if c)
total = len(results)
print(f"\n=== 自检汇总  结果：{passed} 通过 / {total - passed} 失败 ===")
if passed != total:
    sys.exit(1)

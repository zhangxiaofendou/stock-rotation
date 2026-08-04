"""K线/板块数据 Reboot 后自动刷新：落后则后台触发管线，最新则不触发。

避免回归：之前 parquet 被 git seed 覆盖，Reboot 后 K线/板块数据退回旧日期。
_maybe_auto_refresh_on_stale 必须在「本地落后于数据源」时自动跑管线，
在「已最新 / 管线运行中 / 数据源查询失败」时不重复或强行触发。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
from unittest import mock
import dashboard.app as app

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'✅' if ok else '❌'}] {name}" + (f" — {detail}" if detail else ""))


class _FakeThread:
    def __init__(self, target=None, daemon=None):
        self.target = target

    def start(self):
        if self.target is not None:
            self.target()


def _run(stale_local, src="2026-08-05", running=False):
    """构造环境执行 _maybe_auto_refresh_on_stale，返回后台管线被调用次数。"""
    calls = {"n": 0}

    def fake_run():
        calls["n"] += 1

    progress = {"status": "running"} if running else {}
    fake_ss = {}
    with mock.patch.object(app, "get_latest_source_date", return_value=src), \
         mock.patch.object(app, "get_local_data_status", return_value=(stale_local, None)), \
         mock.patch.object(app, "_load_progress", return_value=progress), \
         mock.patch.object(app, "_background_pipeline_run", side_effect=fake_run), \
         mock.patch.object(app, "threading") as mt, \
         mock.patch.object(app, "st") as mst:
        mt.Thread.side_effect = lambda target=None, daemon=None: _FakeThread(target, daemon)
        mst.session_state = fake_ss
        app._maybe_auto_refresh_on_stale()
    return calls["n"]


# 1) 本地落后 → 触发一次
n = _run("2026-08-04")
check("本地落后数据源 → 自动触发一次管线", n == 1, f"calls={n}")

# 2) 本地已最新 → 不触发
n = _run("2026-08-05")
check("本地已最新 → 不触发", n == 0, f"calls={n}")

# 3) 本地为空（Reboot 后 SQLite 清空）→ 触发
n = _run("")
check("本地为空 → 自动触发", n == 1, f"calls={n}")

# 4) 管线正在 running → 不重复触发
n = _run("2026-08-04", running=True)
check("管线运行中 → 不重复触发", n == 0, f"calls={n}")

# 5) 数据源查询失败(空) → 不强行触发（避免无谓跑全量管线）
calls = {"n": 0}


def fake_run():
    calls["n"] += 1


fake_ss = {}
with mock.patch.object(app, "get_latest_source_date", return_value=""), \
     mock.patch.object(app, "get_local_data_status", return_value=("2026-08-04", None)), \
     mock.patch.object(app, "_load_progress", return_value={}), \
     mock.patch.object(app, "_background_pipeline_run", side_effect=fake_run), \
     mock.patch.object(app, "threading") as mt, \
     mock.patch.object(app, "st") as mst:
    mt.Thread.side_effect = lambda target=None, daemon=None: _FakeThread(target, daemon)
    mst.session_state = fake_ss
    app._maybe_auto_refresh_on_stale()
check("数据源查询失败 → 不强行触发", calls["n"] == 0, f"calls={calls['n']}")


passed = sum(1 for _, ok, _ in results if ok)
print(f"\n=== 自动刷新自检：{passed}/{len(results)} 通过 ===")
sys.exit(0 if passed == len(results) else 1)

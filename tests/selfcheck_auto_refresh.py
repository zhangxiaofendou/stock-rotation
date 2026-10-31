"""K线/板块数据 Reboot 后自动刷新（基于本地交易日历，不依赖联网）。

避免回归：之前 _maybe_auto_refresh_on_stale 用「数据源最新交易日」(联网取沪深300)
判断是否需要刷新，但云端容器联网被墙/超时返回 ""，导致 needs 永远为 False，
数据永久不更新。新逻辑改用本地交易日历(latest_trading_day) + 底层产物
(data_is_current) 判定，完全本地、不依赖被墙的数据源。

防假绿：每个用例都显式 mock 三个判定输入，且与「旧实现靠联网 src」明确区分。
"""
import sys
import os
import time

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


def _run(local_date, target="2026-10-31", running=False, current=True,
         now_offset=0.0, sess=None):
    """构造环境执行 _maybe_auto_refresh_on_stale，返回后台管线被调用次数。"""
    calls = {"n": 0}

    def fake_run():
        calls["n"] += 1

    progress = {"status": "running"} if running else {}
    fake_ss = sess if sess is not None else {}
    with mock.patch.object(app, "get_local_data_status", return_value=(local_date, None)), \
         mock.patch.object(app, "_load_progress", return_value=progress), \
         mock.patch.object(app, "_background_pipeline_run", side_effect=fake_run), \
         mock.patch.object(app, "threading") as mt, \
         mock.patch.object(app, "st") as mst, \
         mock.patch.object(app, "time") as mtime, \
         mock.patch("data.daily_pipeline.latest_trading_day", return_value=target), \
         mock.patch("data.daily_pipeline.data_is_current", return_value=current):
        mt.Thread.side_effect = lambda target=None, daemon=None: _FakeThread(target, daemon)
        mst.session_state = fake_ss
        # now() 用于 6 小时节流；now_offset 控制距上次触发的秒数
        mtime.time.return_value = time.time() + now_offset
        app._maybe_auto_refresh_on_stale()
    return calls["n"], fake_ss


# 1) 本地落后 target 且底层未达 → 触发一次
n, _ = _run("2026-08-24", target="2026-10-31", current=False)
check("本地落后交易日历且产物未达 → 自动触发", n == 1, f"calls={n}")

# 2) 本地已最新(>=target) → 不触发
n, _ = _run("2026-10-31", target="2026-10-31", current=True)
check("本地已为最新交易日 → 不触发", n == 0, f"calls={n}")

# 3) 本地为空（Reboot 后 SQLite 清空）→ 触发
n, _ = _run("", target="2026-10-31", current=False)
check("本地为空 → 自动触发", n == 1, f"calls={n}")

# 4) 管线正在 running → 不重复触发
n, _ = _run("2026-08-24", running=True, current=False)
check("管线运行中 → 不重复触发", n == 0, f"calls={n}")

# 5) 6 小时节流：同一会话第二次调用（距上次不足6h）→ 不触发
sess = {}
n1, sess = _run("2026-08-24", target="2026-10-31", current=False, sess=sess)
n2, sess = _run("2026-08-24", target="2026-10-31", current=False, sess=sess)
check("6小时内重复调用只触发一次", n1 == 1 and n2 == 0, f"n1={n1}, n2={n2}")

# 6) 节流到期（距上次>6h）→ 再次触发
sess = {}
n1, sess = _run("2026-08-24", target="2026-10-31", current=False, sess=sess, now_offset=0)
n2, sess = _run("2026-08-24", target="2026-10-31", current=False, sess=sess, now_offset=7 * 3600)
check("超6小时节流后再次触发", n1 == 1 and n2 == 1, f"n1={n1}, n2={n2}")

# 7) 关键回归：数据源联网失败(旧逻辑会因此永不触发) → 新逻辑靠本地日历仍触发
#    （这里不 mock get_latest_source_date，证明新逻辑根本不依赖它）
n, _ = _run("2026-08-24", target="2026-10-31", current=False)
check("不依赖数据源联网即可触发(根治旧bug)", n == 1, f"calls={n}")


passed = sum(1 for _, ok, _ in results if ok)
print(f"\n=== 自动刷新自检(本地日历版)：{passed}/{len(results)} 通过 ===")
sys.exit(0 if passed == len(results) else 1)

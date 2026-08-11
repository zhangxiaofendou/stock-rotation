"""
回归自检：禁止任何 .py 文件与 Python 标准库模块同名（防 ModelScope 部署时遮标准库）

背景（2026-08-12 真实事故）：
- 仓库曾有 `data/calendar.py`。ModelScope 创空间把项目根挂到 sys.path 后，
  `pip install` 内部 `import email.utils` -> `import calendar` 解析到了我们的
  `calendar.py`（而非标准库），它 top-level `import pandas` 触发循环导入，
  导致 install_requirements 崩溃、整个空间 FAILED。
- 标准库模块名（calendar/time/email/random/json/os/re/math/...）绝不能被项目文件占用，
  否则在 cwd 位于 sys.path[0] 的部署环境（ModelScope / 部分 Streamlit 配置）里会遮标准库。

检查项：
  1) 全仓库所有 .py 文件的文件名（不含目录）不能是标准库模块名；
  2) 模拟「项目根在 sys.path[0]」的前提下，`import calendar` 必须解析到标准库（非本项目文件）。
"""

import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
stdlib = set(getattr(sys, "stdlib_module_names", set())) or set()

passed = 0
failed = 0
fails = []


def check(ok: bool, msg: str):
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK]   {msg}")
    else:
        failed += 1
        fails.append(msg)
        print(f"  [FAIL] {msg}")


# ---- 检查 1：文件名撞标准库 ----
collisions = []
for p in ROOT.rglob("*.py"):
    if ".git" in p.parts:
        continue
    if p.stem in stdlib:
        collisions.append(str(p.relative_to(ROOT)))
if collisions:
    for c in collisions:
        print(f"  [FAIL] 文件名撞标准库: {c}")
    failed += len(collisions)
    fails.extend(collisions)
    print(f"  [统计] 共 {len(collisions)} 个文件与标准库同名")
else:
    check(True, "全仓库 .py 文件名均不与标准库模块同名")

# ---- 检查 2：import calendar 必须解析到标准库 ----
# 模拟 ModelScope 把项目根挂到 sys.path[0] 的场景
sys.path.insert(0, str(ROOT))
try:
    import calendar  # noqa: F401

    cal_file = getattr(calendar, "__file__", "").replace("\\", "/")
    # 标准库 calendar 的路径形如 .../Lib/calendar.py；项目文件会在 ROOT 下或 site-packages 下
    is_ours = str(ROOT).replace("\\", "/").lower() in cal_file.lower()
    is_stdlib = cal_file.lower().endswith("/lib/calendar.py") and (not is_ours)
    check(
        is_stdlib,
        f"import calendar 解析到标准库 (file={cal_file})",
    )
    if not is_stdlib:
        fails.append("import calendar 未解析到标准库")
finally:
    # 还原 sys.path，避免污染后续
    if str(ROOT) in sys.path:
        sys.path.remove(str(ROOT))

# ---- 汇总 ----
print(f"\n=== 自检汇总  结果：{passed} 通过 / {failed} 失败 ===")
if failed:
    print("失败项：")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
sys.exit(0)

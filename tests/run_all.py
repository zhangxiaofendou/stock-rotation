"""一键跑完全部自检。

交付前的统一验证入口：编译检查 + 全部 selfcheck 脚本。
任何一项失败即以非零码退出，避免把「可能没问题」的版本交出去。

    python tests/run_all.py
"""

import os
import py_compile
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

COMPILE_TARGETS = [
    "auth.py",
    "diagnostics.py",
    "dashboard/app.py",
    "portfolio/holdings.py",
    "portfolio/stock_lookup.py",
    "report/generator.py",
    "data/storage/pg_store.py",
    "data/storage/sqlite_store.py",
    "dashboard/pages/portfolio.py",
    "dashboard/pages/reports.py",
]

SUITES = [
    ("认证逻辑", "tests/selfcheck_auth.py"),
    ("Postgres 存储层", "tests/selfcheck_pg.py"),
    ("诊断器有效性", "tests/selfcheck_diagnostics.py"),
    ("页面渲染冒烟", "tests/selfcheck_ui_smoke.py"),
    ("行情自动补全", "tests/selfcheck_lookup.py"),
    ("持仓分析逻辑", "tests/selfcheck_portfolio_analysis.py"),
]


def main() -> int:
    failures = []

    print("=" * 62)
    print("  交付前全量自检")
    print("=" * 62)

    print("\n[1/2] 语法编译")
    for rel in COMPILE_TARGETS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f"  [SKIP] {rel}（文件不存在）")
            continue
        try:
            py_compile.compile(path, doraise=True)
            print(f"  [OK]   {rel}")
        except Exception as e:
            print(f"  [FAIL] {rel} -> {e}")
            failures.append(f"编译 {rel}")

    print("\n[2/2] 自检套件")
    for label, rel in SUITES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f"  [SKIP] {label}（{rel} 不存在）")
            continue
        proc = subprocess.run([PY, path], cwd=ROOT, capture_output=True, text=True)
        tail = [ln for ln in (proc.stdout or "").splitlines() if "结果：" in ln]
        summary = tail[-1].strip() if tail else "(无摘要)"
        if proc.returncode == 0:
            print(f"  [OK]   {label:<16} {summary}")
        else:
            print(f"  [FAIL] {label:<16} {summary}")
            failures.append(label)
            for ln in (proc.stdout or "").splitlines():
                if "[FAIL]" in ln:
                    print(f"         {ln.strip()}")
            if proc.stderr.strip():
                print(f"         stderr: {proc.stderr.strip()[:400]}")

    print("\n" + "=" * 62)
    if failures:
        print(f"  未通过：{', '.join(failures)}")
        print("=" * 62)
        return 1
    print("  全部通过，可以交付")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

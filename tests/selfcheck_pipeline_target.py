"""管线目标交易日（latest_trading_day）回归自检。

背景 bug：早盘 07:30 兜底自动化运行时，latest_trading_day 直接返回「今天」
（周一~周五都是交易日），但当天尚未收盘、行情根本不存在，于是幂等守卫
data_is_current(target) 恒为 False —— 每天早上都会白跑一次全量重算。

修复：只把「收盘数据已可获取」的交易日当作目标（当天 15:30 之前顺延到上一交易日）。
"""

import sys
from datetime import datetime

sys.path.insert(0, ".")

from data.daily_pipeline import latest_trading_day, close_data_available  # noqa: E402

checks = []


def check(name, condition):
    checks.append((name, bool(condition)))


def dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


# ---- close_data_available ----
check("当天 07:30 收盘数据不可得",
      close_data_available("2026-08-03", dt("2026-08-03 07:30")) is False)
check("当天 15:29 收盘数据仍不可得",
      close_data_available("2026-08-03", dt("2026-08-03 15:29")) is False)
check("当天 15:30 收盘数据可得",
      close_data_available("2026-08-03", dt("2026-08-03 15:30")) is True)
check("当天 22:00 收盘数据可得",
      close_data_available("2026-08-03", dt("2026-08-03 22:00")) is True)
check("历史日期恒可得",
      close_data_available("2026-07-31", dt("2026-08-03 07:30")) is True)

# ---- latest_trading_day ----
# 核心回归：周一早盘 07:30，目标必须是上周五（07-31），不能是当天（08-03）。
mon_morning = latest_trading_day("2026-08-03", dt("2026-08-03 07:30"))
check(f"周一 07:30 目标=上周五 07-31（实得 {mon_morning}）",
      mon_morning == "2026-07-31")

# 22:00 主自动化：当天已收盘，目标就是当天，行为不变。
mon_night = latest_trading_day("2026-08-03", dt("2026-08-03 22:00"))
check(f"周一 22:00 目标=当天 08-03（实得 {mon_night}）",
      mon_night == "2026-08-03")

# 周二早盘：目标是周一（前一交易日），不是当天。
tue_morning = latest_trading_day("2026-08-04", dt("2026-08-04 07:30"))
check(f"周二 07:30 目标=周一 08-03（实得 {tue_morning}）",
      tue_morning == "2026-08-03")

# 周六任意时刻：目标都是周五，与时间无关。
sat = latest_trading_day("2026-08-08", dt("2026-08-08 07:30"))
check(f"周六 07:30 目标=周五 08-07（实得 {sat}）", sat == "2026-08-07")

# 周日早盘：目标仍是周五。
sun = latest_trading_day("2026-08-09", dt("2026-08-09 07:30"))
check(f"周日 07:30 目标=周五 08-07（实得 {sun}）", sun == "2026-08-07")

# 目标永远不晚于当天。
check("目标不晚于当天", mon_morning <= "2026-08-03" and tue_morning <= "2026-08-04")

passed = sum(ok for _, ok in checks)
for name, ok in checks:
    print(f"[{'OK' if ok else 'FAIL'}] {name}")
print(f"结果：{passed}/{len(checks)} 通过")
sys.exit(1 if passed != len(checks) else 0)

import sys
sys.path.insert(0, ".")
from portfolio.fees import estimate_trade_fee

checks = []
def check(name, condition):
    checks.append((name, bool(condition)))

buy = estimate_trade_fee("600519", "BUY", 100, 10)
sell = estimate_trade_fee("600519", "SELL", 1000, 10)
etf = estimate_trade_fee("510300", "SELL", 1000, 4)
adjust = estimate_trade_fee("600519", "ADJUST", 100, 10)
check("买入含最低佣金和过户费", buy.total == 5.01 and buy.stamp_tax == 0)
check("卖出含印花税", sell.stamp_tax == 5.0 and sell.total == 10.10)
check("ETF不收印花税和过户费", etf.stamp_tax == 0 and etf.transfer_fee == 0)
check("调账不收费用", adjust.total == 0)
passed = sum(ok for _, ok in checks)
for name, ok in checks:
    print(f"[{'OK' if ok else 'FAIL'}] {name}")
print(f"结果：{passed}/{len(checks)} 通过")
sys.exit(1 if passed != len(checks) else 0)

import sys
from unittest import mock
sys.path.insert(0, ".")
from portfolio.stock_lookup import clear_cache, is_etf_code, lookup_stock_info

checks = []
def check(name, cond): checks.append((name, bool(cond)))
check("159766识别为ETF", is_etf_code("159766"))
check("股票不误判ETF", not is_etf_code("600519"))
clear_cache()
body = '{"data":{"f43":5650,"f57":"159766","f58":"旅游ETF富国","f127":""}}'.encode("utf-8")
resp = mock.MagicMock(); resp.read.return_value = body; resp.__enter__.return_value = resp
with mock.patch("urllib.request.urlopen", return_value=resp):
    info = lookup_stock_info("159766")
check("ETF自动关联旅游板块", info and info["sector_name"] == "旅游")
check("ETF类型写入补全结果", info and info["asset_type"] == "etf")
passed = sum(ok for _, ok in checks)
for name, ok in checks: print(f"[{'OK' if ok else 'FAIL'}] {name}")
print(f"结果：{passed}/{len(checks)} 通过")
sys.exit(1 if passed != len(checks) else 0)

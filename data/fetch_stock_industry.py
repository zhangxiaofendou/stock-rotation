# -*- coding: utf-8 -*-
"""一次性抓取全 A 股 代码 -> 东财行业名（行业板块成分股法），存离线 JSON。

东财「行业板块」(m:90+t:2) 的板块名即个股所属行业名（与个股 f127 一致），
如 航空机场/水泥/铁路公路/半导体/影视院线。
逐板块拉成分股 -> 建立 代码->行业名 映射，随仓库发布，云端离线可用。

用法:
  python fetch_stock_industry.py            # 抓全量写 config/stock_industry_em.json
  python fetch_stock_industry.py --probe    # 只验前几板块
"""
import sys
import os
import json
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "config", "stock_industry_em.json")
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    return json.loads(urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore"))


def _pages(url_template, fmt):
    """按 pz=100 分页拉取 clist，返回 list of 行(dict)。fmt 为格式化参数元组。"""
    out = []
    pn = 1
    while True:
        if fmt:
            u = url_template % (tuple([pn]) + tuple(fmt))
        else:
            u = url_template % pn
        d = _get(u).get("data") or {}
        diff = d.get("diff")
        rows = list(diff.values()) if isinstance(diff, dict) else (diff or [])
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 100:
            break
        pn += 1
        time.sleep(0.12)
    return out


def _boards():
    t = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100"
         "&fs=m:90+t:2&fields=f12,f14")
    return _pages(t, ())


def _cons(bk):
    t = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=6000"
         "&fs=b:%s&fields=f12")
    return _pages(t, (bk,))


def main():
    probe = "--probe" in sys.argv
    print("拉取行业板块列表...")
    boards = _boards()
    print("板块数:", len(boards))
    if probe:
        for b in boards[:4]:
            cons = _cons(b["f12"])
            print(b["f14"], "->", len(cons), "只, 样本:", [c.get("f12") for c in cons[:3]])
            time.sleep(0.3)
        return

    out = {}
    miss = 0
    for b in boards:
        bk, name = b.get("f12"), str(b.get("f14") or "").strip()
        if not name:
            continue
        try:
            cons = _cons(bk)
        except Exception as e:
            print("板块 %s 失败: %s" % (bk, e))
            miss += 1
            continue
        for c in cons:
            code = str(c.get("f12") or "").strip()
            if code and code not in out:
                out[code] = name
        time.sleep(0.2)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("已写入 %s: %d 只股票映射到行业名 (%d 板块失败)" % (OUT, len(out), miss))


if __name__ == "__main__":
    main()

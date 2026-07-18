# -*- coding: utf-8 -*-
"""生成「行业状态验证」数据：全板块 趋势/RS分位/横截面/九宫格状态 + 250天K线截面。
用新逻辑现算（重算 trend parquet → 清 state 快照 → 汇总状态）。"""
import os
import sys
import json
import numpy as np
import pandas as pd
from collections import Counter

sys.path.insert(0, ".")

from config.settings import PARQUET_DIR
from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from indicators.price_trend import PriceTrend
from model.state_machine import StateMachine

ps = ParquetStore()
ss = SQLiteStore()
pt = PriceTrend(ps, ss)
sm = StateMachine(ps, ss)

RS_DIR = os.path.join(str(PARQUET_DIR), "indicators", "rs")
TREND_DIR = os.path.join(str(PARQUET_DIR), "indicators", "trend")
os.makedirs(TREND_DIR, exist_ok=True)

# 板块清单：从 RS 目录枚举
codes = []
for f in os.listdir(RS_DIR):
    if f.endswith(".parquet"):
        c = f.replace(".parquet", "").replace("_", ".", 1)
        if not c.endswith(".SI"):
            c = c.replace("_SI", ".SI")
        codes.append(c)
codes = sorted(set(codes))
print(f"板块数: {len(codes)}")

# 1) 用新 price_trend 逻辑重算 trend parquet（保证磁盘与新代码一致）
recalc = 0
for code in codes:
    try:
        ts = pt.calc_trend_series(code)
        if ts is not None and not ts.empty:
            safe = code.replace(".", "_")
            ts.to_parquet(os.path.join(TREND_DIR, f"{safe}.parquet"), index=False)
            recalc += 1
    except Exception as e:
        print(f"  重算trend失败 {code}: {e}")
print(f"重算 trend parquet: {recalc}/{len(codes)}")

# 2) 清 state 快照，强制用新逻辑重算
snap = os.path.join(str(PARQUET_DIR), "cache", "state_snapshot.parquet")
if os.path.exists(snap):
    os.remove(snap)
    print("已删除 state 快照")

# 3) 汇总最新日截面
state_df = sm.calc_all_sectors_state()
if state_df is None or state_df.empty:
    print("ERROR: 无状态数据")
    sys.exit(1)
data_date = str(state_df["date"].iloc[0])[:10]
print(f"截面日期: {data_date}, 板块数: {len(state_df)}")

# 板块名映射
name_map = {}
for code in codes:
    try:
        info = ss.get_sector_by_code(code)
        name_map[code] = info.get("name", code) if info else code
    except Exception:
        name_map[code] = code

# 4) 每个板块最近 250 天 K 线 + 均线
COL = {"日期": "date", "收盘": "close", "开盘": "open", "最高": "high", "最低": "low"}
kline = {}
for code in state_df["sector_code"].tolist():
    df = ps.load_index_hist(code)
    if df is None or df.empty:
        continue
    df = df.rename(columns={k: v for k, v in COL.items() if k in df.columns})
    if "date" not in df.columns or "close" not in df.columns:
        continue
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"]
    df["ma5"] = c.rolling(5, min_periods=1).mean()
    df["ma20"] = c.rolling(20, min_periods=1).mean()
    df["ma60"] = c.rolling(60, min_periods=1).mean()
    tail = df.tail(250)
    has = lambda k: k in tail.columns
    kline[code] = {
        "d": [x.strftime("%Y-%m-%d") for x in tail["date"]],
        "o": [round(float(v), 2) for v in tail["open"]] if has("open") else [],
        "h": [round(float(v), 2) for v in tail["high"]] if has("high") else [],
        "l": [round(float(v), 2) for v in tail["low"]] if has("low") else [],
        "c": [round(float(v), 2) for v in tail["close"]],
        "ma5": [round(float(v), 2) for v in tail["ma5"]],
        "ma20": [round(float(v), 2) for v in tail["ma20"]],
        "ma60": [round(float(v), 2) for v in tail["ma60"]],
    }

# 5) 组装表格行
def fnum(v, nd=1):
    if v is None:
        return None
    try:
        f = float(v)
        if np.isnan(f):
            return None
        return round(f, nd)
    except Exception:
        return None

rows = []
for _, r in state_df.iterrows():
    code = r["sector_code"]
    if code not in kline:
        continue
    # 横盘角标：从趋势 parquet 末行读取 trend_badge
    #   负=下穿20日线(死叉)天数(绿)；正=上穿20日线(金叉)天数(红)；0=无角标
    badge = 0
    safe = code.replace(".", "_")
    tpath = os.path.join(TREND_DIR, f"{safe}.parquet")
    if os.path.exists(tpath):
        try:
            tdf = pd.read_parquet(tpath)
            if "trend_badge" in tdf.columns and not tdf.empty:
                badge = int(tdf["trend_badge"].iloc[-1])
        except Exception:
            pass
    rows.append({
        "code": code,
        "name": name_map.get(code, code),
        "trend": r["trend"],
        "trend_badge": badge,
        "state": r["state"],
        "rs_pct": fnum(r.get("rs_percentile")),
        "rs_mom_pct": fnum(r.get("rs_momentum_percentile")),
        "cross": fnum(r.get("rs_momentum_cross_pct")),
    })

# 状态排序：③②①⑥⑨⑤④⑧⑦
STATE_ORDER = {"③": 0, "②": 1, "①": 2, "⑥": 3, "⑨": 4, "⑤": 5, "④": 6, "⑧": 7, "⑦": 8}
rows.sort(key=lambda x: (STATE_ORDER.get(x["state"][0], 9), -(x["rs_mom_pct"] or 0)))

payload = {"date": data_date, "rows": rows, "kline": kline}

dist = Counter(x["state"] for x in rows)
print("状态分布:", dict(sorted(dist.items())))

out_json = json.dumps(payload, ensure_ascii=False)
with open("_report_data.json", "w", encoding="utf-8") as fp:
    fp.write(out_json)
print(f"数据已写出: _report_data.json ({len(out_json)//1024} KB), 有K线板块 {len(kline)}")

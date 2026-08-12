"""每日盘后报告生成器。

设计原则（PRD 5.5 / 5.6）：
- 只汇总已计算结果，不在报告中重算状态 / 评分 / 绩效 / 风险；
- 每个区块独立 try/except，单个数据源缺失不阻断整份报告；
- 归档为自包含 HTML（内嵌样式，红涨绿跌）+ JSON 元数据，便于历史回看与推送。

入口：``generate_report(as_of_date=None)`` 会生成并归档当日报告，返回
``{"as_of_date", "html_path", "meta_path", "html", "report"}``。
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from config.logger import get_logger
from config.settings import PARQUET_DIR
from data.freshness import DataFreshness
from data.storage.parquet_store import ParquetStore
from data.storage.sqlite_store import SQLiteStore
from indicators.crowding import CrowdingIndicator
from model.circuit_breaker import CircuitBreaker
from model.mirror_pair import MirrorPair
from model.scoring import SectorScoring
from model.state_machine import StateMachine
from portfolio.advisor import PortfolioAdvisor
from portfolio.holdings import PortfolioHoldings
from signal_tracker import performance as P

logger = get_logger(__name__)

REPORT_DIR = os.path.join(os.path.dirname(str(PARQUET_DIR)), "reports")
os.makedirs(REPORT_DIR, exist_ok=True)

# 拥挤度风险阈值（crowding_score 越高越拥挤，>=0.90 视为拥挤超标）
CROWDED_THRESHOLD = 0.90
# 近距离状态（拥挤风险重点关注这些"热"状态）
HOT_STATES = {"②稳健上行", "③加速冲顶", "⑥弱转强", "⑨底背离"}

UP_COLOR = "#d32f2f"      # 红 = 涨 / 买入（中国市场约定）
DOWN_COLOR = "#2e7d32"    # 绿 = 跌 / 卖出
NEU_COLOR = "#757575"
WARN_COLOR = "#e65100"
DANGER_COLOR = "#b71c1c"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _git_hash() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _safe(v: Any, default: str = "—") -> str:
    if v is None:
        return default
    try:
        if isinstance(v, float) and pd.isna(v):
            return default
    except TypeError:
        pass
    return str(v)


def _pct(v: Optional[float], nd: int = 1) -> str:
    if v is None:
        return "—"
    try:
        if pd.isna(v):
            return "—"
    except TypeError:
        pass
    return f"{v * 100:.{nd}f}%"


def _color_for_return(v: Optional[float]) -> str:
    if v is None:
        return NEU_COLOR
    try:
        if pd.isna(v):
            return NEU_COLOR
    except TypeError:
        pass
    if v > 0:
        return UP_COLOR
    if v < 0:
        return DOWN_COLOR
    return NEU_COLOR


def _df_to_html(df: pd.DataFrame, columns: List[Dict[str, Any]]) -> str:
    """通用 DataFrame -> HTML 表格。columns: [{key, label, fmt, color_key}]。"""
    if df is None or df.empty:
        return '<p class="muted">（无数据）</p>'
    head = "".join(f"<th>{c['label']}</th>" for c in columns)
    rows = []
    for _, row in df.iterrows():
        tds = []
        for c in columns:
            val = row.get(c["key"])
            text = c.get("fmt", _safe)(val) if callable(c.get("fmt")) else _safe(val)
            style = ""
            if c.get("color_key"):
                style = f' style="color:{_color_for_return(row.get(c["color_key"]))}"'
            elif c.get("color_map"):
                style = f' style="color:{c["color_map"].get(_safe(val), NEU_COLOR)}"'
            tds.append(f"<td{style}>{text}</td>")
        rows.append(f"<tr>{''.join(tds)}</tr>")
    return (
        f'<table class="data"><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


# ---------------------------------------------------------------------------
# 各区块采集（均独立容错）
# ---------------------------------------------------------------------------
def _gather_meta(as_of_date: str) -> Dict[str, Any]:
    today = datetime.now().date()
    data_date = None
    try:
        data_date = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    except Exception:
        pass
    lag_days = (today - data_date).days if data_date else None
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "as_of_date": as_of_date,
        "git_hash": _git_hash(),
        "data_lag_days": lag_days,
    }


def _gather_market(sm: StateMachine, cb: CircuitBreaker, sc: SectorScoring, as_of_date: str,
                   state_df: Optional[pd.DataFrame] = None,
                   market_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False}
    try:
        if state_df is None:
            state_df = sm.calc_all_sectors_state(date=as_of_date)
        if state_df is None or state_df.empty:
            return out
        status = market_status if market_status is not None else cb.check_market_status(date=as_of_date)
        up = int((state_df["trend"] == "上涨").sum())
        down = int((state_df["trend"] == "下跌").sum())
        flat = int((state_df["trend"] == "横盘").sum())
        total = len(state_df)
        # 状态分布计数（直接由已算好的 state_df 统计，避免二次全量重算）
        dist_counts = {}
        if "state" in state_df.columns:
            dist_counts = state_df["state"].value_counts().to_dict()
        # 风格判断
        if total:
            down_ratio = down / total
            if down_ratio >= 0.6:
                style = "普跌（防御）"
            elif down_ratio <= 0.4 and up >= down:
                style = "偏强（进攻）"
            else:
                style = "分化震荡"
        else:
            style = "—"
        # 强势板块
        top = sc.get_top_sectors(n=5, date=as_of_date)
        top_list = []
        if top is not None and not top.empty:
            for _, r in top.head(5).iterrows():
                top_list.append({
                    "name": _safe(r.get("sector_name")),
                    "score": _safe(r.get("score")),
                    "state": _safe(r.get("state")),
                })
        out.update({
            "ok": True,
            "mode": status.get("mode", "normal"),
            "reason": status.get("reason", ""),
            "down_ratio": status.get("down_ratio", 0.0),
            "kill_ratio": status.get("kill_ratio", 0.0),
            "hs300_drop": status.get("hs300_drop", 0.0),
            "consecutive_days": status.get("consecutive_days", 0),
            "up": up, "down": down, "flat": flat, "total": total,
            "dist_counts": dist_counts,
            "style": style,
            "top": top_list,
        })
    except Exception as e:
        logger.warning("采集市场概览失败: %s", e)
    return out


def _gather_holdings(sm: StateMachine, as_of_date: str,
                     state_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "position_count": 0}
    try:
        holdings = PortfolioHoldings()
        summary = holdings.summary()
        positions = holdings.positions()
        # 行业状态映射
        if state_df is None:
            state_df = sm.calc_all_sectors_state(date=as_of_date)
        state_map = {}
        if state_df is not None and not state_df.empty:
            for _, r in state_df.iterrows():
                state_map[str(r["sector_code"])] = _safe(r.get("state"))
        pos_rows = []
        ps = ParquetStore()
        for _, r in positions.iterrows():
            code = _safe(r.get("security_code"))
            cost_amt = float(r.get("cost_amount", 0.0) or 0.0)
            sc_code = r.get("sector_code")
            st = state_map.get(_safe(sc_code), "—") if pd.notna(sc_code) and sc_code else "—"
            # 参考市值（仅当标的自身有指数行情时估算，明确标注"参考"）
            ref_value = None
            ref_pnl = None
            try:
                h = ps.load_index_hist(code, limit=1)
                if h is not None and not h.empty and "close" in h.columns:
                    close = float(h["close"].iloc[-1])
                    qty = float(r.get("quantity", 0.0) or 0.0)
                    avg = float(r.get("avg_cost", 0.0) or 0.0)
                    if qty > 0 and avg > 0:
                        ref_value = qty * close
                        ref_pnl = (close - avg) / avg
            except Exception:
                pass
            pos_rows.append({
                "name": _safe(r.get("security_name")),
                "sector": _safe(r.get("sector_name")) or "—",
                "state": st,
                "cost": cost_amt,
                "ref_value": ref_value,
                "ref_pnl": ref_pnl,
            })
        out.update({
            "ok": True,
            "summary": summary,
            "positions": pos_rows,
        })
    except Exception as e:
        logger.warning("采集持仓状态失败: %s", e)
    return out


def _gather_transitions(sqlite: SQLiteStore, as_of_date: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        df = sqlite.get_signal_events(start=as_of_date, end=as_of_date)
        if df is None or df.empty:
            return rows
        for _, r in df.iterrows():
            frm = _safe(r.get("from_state"))
            to = _safe(r.get("to_state"))
            if frm == to:
                continue
            rows.append({
                "name": _safe(r.get("sector_name")),
                "from": frm,
                "to": to,
                "action": _safe(r.get("action")),
                "signal": _safe(r.get("to_signal")),
            })
    except Exception as e:
        logger.warning("采集状态切换失败: %s", e)
    return rows


def _gather_mirrors(mp: MirrorPair, as_of_date: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "pairs": [], "new_pairs": []}
    try:
        pairs = mp.find_mirror_pairs(date=as_of_date)
        if not pairs:
            return out
        # 尝试识别"新增"：与上一交易日比对（失败则全部视为当前）
        prev_new = set()
        try:
            from data.market_calendar import TradeCalendar
            tc = TradeCalendar()
            prev_days = tc.get_last_n_trading_days(2, before=as_of_date)
            if len(prev_days) >= 2:
                prev_date = prev_days[-1]
                prev = mp.find_mirror_pairs(date=prev_date)
                prev_keys = {(p["strong_sector"], p["weak_sector"]) for p in prev}
                prev_new = {(p["strong_sector"], p["weak_sector"]) for p in pairs} - prev_keys
        except Exception:
            pass
        for p in pairs:
            key = (p["strong_sector"], p["weak_sector"])
            out["pairs"].append({
                "strong_name": _safe(p.get("strong_name")),
                "strong_state": _safe(p.get("strong_state")),
                "weak_name": _safe(p.get("weak_name")),
                "weak_state": _safe(p.get("weak_state")),
                "group": _safe(p.get("group")),
                "pair_type": _safe(p.get("pair_type")),
                "confidence": float(p.get("confidence", 0.0) or 0.0),
                "is_new": key in prev_new,
            })
        out["new_pairs"] = [p for p in out["pairs"] if p["is_new"]]
        out["ok"] = True
    except Exception as e:
        logger.warning("采集镜像对失败: %s", e)
    return out


def _gather_advice(sm: StateMachine, as_of_date: str,
                   state_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "items": [], "count": 0}
    try:
        advisor = PortfolioAdvisor()
        if state_df is None:
            state_df = sm.calc_all_sectors_state(date=as_of_date)
        items = advisor.build_pending_items(sector_states=state_df)
        if items is not None and not items.empty:
            out["items"] = items.to_dict("records")
            out["count"] = len(items)
        out["ok"] = True
    except Exception as e:
        logger.warning("采集操作建议失败: %s", e)
    return out


def _gather_arbitration(state_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """采集信号仲裁概览（九宫格 × 资金流 × 研报 三路仲裁计数）。"""
    out: Dict[str, Any] = {"ok": False, "counts": {}, "n": 0}
    try:
        from model.confirmation import arbitrate_all
        if state_df is None or state_df.empty:
            return out
        res = arbitrate_all(state_df)
        out["counts"] = res.get("counts", {})
        out["n"] = res.get("n", 0)
        out["ok"] = True
    except Exception as e:
        logger.warning("采集信号仲裁概览失败: %s", e)
    return out


def _gather_performance(sqlite: SQLiteStore) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False}
    try:
        ov = P.aggregate_overview(window_days=30, sqlite=sqlite)
        alerts = P.failure_alerts(
            config=P.PerformanceConfig(min_samples=30, fail_threshold=0.40),
            sqlite=sqlite, window_days=90,
        )
        out["overview"] = ov
        out["alerts"] = alerts.get("alerts")
        out["ok"] = True
    except Exception as e:
        logger.warning("采集信号绩效失败: %s", e)
    return out


def _gather_risks(sm: StateMachine, cb: CircuitBreaker, as_of_date: str,
                  meta: Dict[str, Any],
                  state_df: Optional[pd.DataFrame] = None,
                  market_status: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    risks: List[Dict[str, Any]] = []
    try:
        # 1) 熔断 / 防御模式
        status = market_status if market_status is not None else cb.check_market_status(date=as_of_date)
        if status.get("mode") == "defense":
            risks.append({
                "level": "高", "category": "市场熔断",
                "text": f"市场进入防御模式：{status.get('reason', '')}",
            })
        elif status.get("kill_ratio", 0) >= 0.15:
            risks.append({
                "level": "中", "category": "系统性风险",
                "text": f"⑦持续杀跌板块占比 {_pct(status.get('kill_ratio'))}，需关注系统性风险。",
            })
        # 2) 拥挤度超标（只关注真正建仓/买入状态 ⑥⑨，取拥挤度最高的少量，控制开销与噪声）
        try:
            if state_df is None:
                state_df = sm.calc_all_sectors_state(date=as_of_date)
            ci = CrowdingIndicator(ParquetStore(), SQLiteStore())
            if state_df is not None and not state_df.empty:
                buy_hot = state_df[state_df["state"].isin({"⑥弱转强", "⑨底背离"})]
                crowded = []
                for _, r in buy_hot.iterrows():
                    code = _safe(r["sector_code"])
                    try:
                        cs = ci.calc_crowding_score(code)
                        if cs is None or cs.empty:
                            continue
                        s = cs["crowding_score"].dropna()
                        if s.empty:
                            continue
                        score = float(s.iloc[-1])
                        if score >= CROWDED_THRESHOLD:
                            crowded.append((score, _safe(r.get("sector_name"))))
                    except Exception:
                        continue
                # 只保留最拥挤的前 10 个，避免刷屏
                crowded.sort(reverse=True)
                for score, name in crowded[:10]:
                    risks.append({
                        "level": "中", "category": "拥挤度",
                        "text": f"{name} 拥挤度 {score:.2f} 超标（≥{CROWDED_THRESHOLD}），注意追高风险。",
                    })
        except Exception as e:
            logger.warning("拥挤度风险采集失败: %s", e)
        # 3) 数据新鲜度（合并为单条摘要，避免逐板块刷屏）
        try:
            stale = DataFreshness().check_stale()
            if stale is not None and not stale.empty:
                n = len(stale)
                oldest = stale.get("age_days")
                oldest_v = ""
                if oldest is not None and not oldest.empty:
                    try:
                        mx = pd.to_numeric(oldest, errors="coerce").max()
                        if pd.notna(mx):
                            oldest_v = f"，最长已 {int(mx)} 天"
                    except Exception:
                        pass
                risks.append({
                    "level": "中", "category": "数据新鲜度",
                    "text": f"检测到 {n} 项数据更新滞后{oldest_v}，请检查每日管线运行状态（侧边栏「数据状态」）。",
                })
        except Exception:
            pass
        # 4) 数据滞后
        lag = meta.get("data_lag_days")
        if lag is not None and lag >= 2:
            risks.append({
                "level": "低", "category": "数据滞后",
                "text": f"报告数据截止 {meta.get('as_of_date')}，距今日已 {lag} 天，可能跨周末/假期或非交易日。",
            })
    except Exception as e:
        logger.warning("采集风险预警失败: %s", e)
    return risks


# ---------------------------------------------------------------------------
# HTML 组装
# ---------------------------------------------------------------------------
_CSS = """
<style>
.report-wrap { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  color:#212121; max-width:980px; margin:0 auto; padding:8px 4px; }
.report-wrap h1 { font-size:22px; margin:0 0 4px; }
.report-wrap h2 { font-size:16px; margin:18px 0 8px; padding-left:8px;
  border-left:4px solid #1565c0; }
.report-wrap .meta { color:#757575; font-size:12px; margin-bottom:6px; }
.report-wrap .badge { display:inline-block; padding:2px 10px; border-radius:10px;
  font-size:13px; font-weight:600; color:#fff; }
.report-wrap .metrics { display:flex; flex-wrap:wrap; gap:10px; margin:8px 0; }
.report-wrap .metric { background:#f5f5f5; border-radius:8px; padding:8px 14px; min-width:90px; }
.report-wrap .metric .v { font-size:20px; font-weight:700; }
.report-wrap .metric .l { font-size:12px; color:#616161; }
.report-wrap table.data { border-collapse:collapse; width:100%; font-size:13px; margin:6px 0; }
.report-wrap table.data th { background:#eeeeee; text-align:left; padding:6px 8px; border-bottom:2px solid #e0e0e0; }
.report-wrap table.data td { padding:5px 8px; border-bottom:1px solid #eeeeee; }
.report-wrap .muted { color:#9e9e9e; font-size:13px; }
.report-wrap .risk { border-radius:6px; padding:6px 10px; margin:5px 0; font-size:13px; }
.report-wrap .risk-高 { background:#ffebee; border-left:4px solid #c62828; }
.report-wrap .risk-中 { background:#fff3e0; border-left:4px solid #ef6c00; }
.report-wrap .risk-低 { background:#f5f5f5; border-left:4px solid #9e9e9e; }
.report-wrap .note { color:#757575; font-size:12px; margin-top:4px; }
</style>
"""


def _metrics_html(items: List[Dict[str, str]]) -> str:
    cells = "".join(
        f'<div class="metric"><div class="v" style="color:{c.get("color","#212121")}">{c["v"]}</div>'
        f'<div class="l">{c["l"]}</div></div>'
        for c in items
    )
    return f'<div class="metrics">{cells}</div>'


def _build_html(r: Dict[str, Any]) -> str:
    meta = r["meta"]
    mkt = r.get("market", {})
    hld = r.get("holdings", {})
    trans = r.get("transitions", [])
    mir = r.get("mirrors", {})
    adv = r.get("advice", {})
    perf = r.get("performance", {})
    risks = r.get("risks", [])

    parts: List[str] = [f'<div class="report-wrap">{_CSS}']
    # 头部
    mode = mkt.get("mode", "normal")
    mode_label = "防御模式" if mode == "defense" else "正常"
    mode_color = DANGER_COLOR if mode == "defense" else "#2e7d32"
    lag = meta.get("data_lag_days")
    lag_str = f" ｜ 数据滞后 {lag} 天" if lag is not None and lag >= 2 else ""
    parts.append(
        f'<h1>每日盘后报告</h1>'
        f'<div class="meta">数据截止 <b>{meta.get("as_of_date")}</b> ｜ '
        f'生成于 {meta.get("generated_at")} ｜ 代码版本 {meta.get("git_hash")}{lag_str}</div>'
        f'<div><span class="badge" style="background:{mode_color}">市场环境：{mode_label}</span></div>'
    )

    # 1) 市场概述
    parts.append('<h2>一、市场概述</h2>')
    if mkt.get("ok"):
        parts.append(f'<p class="note">{mkt.get("reason") or "市场环境正常。"}</p>')
        parts.append(_metrics_html([
            {"v": f'{mkt.get("up",0)}', "l": "上涨板块", "color": UP_COLOR},
            {"v": f'{mkt.get("flat",0)}', "l": "横盘板块", "color": NEU_COLOR},
            {"v": f'{mkt.get("down",0)}', "l": "下跌板块", "color": DOWN_COLOR},
            {"v": mkt.get("style", "—"), "l": "风格判断"},
            {"v": _pct(mkt.get("down_ratio")), "l": "下跌占比"},
            {"v": _pct(mkt.get("kill_ratio")), "l": "杀跌占比"},
        ]))
        # 九宫格状态分布
        dist = mkt.get("dist_counts", {})
        if dist:
            dist_rows = "".join(
                f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in
                sorted(dist.items(), key=lambda x: -x[1])
            )
            parts.append('<details open><summary>九宫格状态分布</summary>'
                         f'<table class="data"><thead><tr><th>状态</th><th>板块数</th></tr></thead>'
                         f'<tbody>{dist_rows}</tbody></table></details>')
        top = mkt.get("top", [])
        if top:
            top_rows = "".join(
                f'<tr><td>{t["name"]}</td><td>{t["score"]}</td><td>{t["state"]}</td></tr>'
                for t in top
            )
            parts.append('<details><summary>强势板块 Top5（综合评分）</summary>'
                         f'<table class="data"><thead><tr><th>板块</th><th>评分</th><th>状态</th></tr></thead>'
                         f'<tbody>{top_rows}</tbody></table></details>')
    else:
        parts.append('<p class="muted">（市场数据暂不可用，请先完成数据更新。）</p>')

    # 2) 持仓状态
    parts.append('<h2>二、持仓状态</h2>')
    if hld.get("ok") and hld.get("positions"):
        s = hld.get("summary", {})
        parts.append(_metrics_html([
            {"v": str(s.get("position_count", 0)), "l": "持仓标的"},
            {"v": f'¥{float(s.get("total_cost",0)):,.0f}', "l": "总成本"},
            {"v": str(s.get("sector_count", 0)), "l": "涉及行业"},
        ]))
        cols = [
            {"key": "name", "label": "标的"},
            {"key": "sector", "label": "行业"},
            {"key": "state", "label": "行业状态"},
            {"key": "cost", "label": "成本", "fmt": lambda v: f"¥{float(v):,.0f}" if v else "—"},
            {"key": "ref_value", "label": "参考市值*", "fmt": lambda v: f"¥{float(v):,.0f}" if v else "—"},
            {"key": "ref_pnl", "label": "参考浮盈亏*", "color_key": "ref_pnl",
             "fmt": lambda v: _pct(v) if v is not None else "—"},
        ]
        parts.append(_df_to_html(pd.DataFrame(hld["positions"]), cols))
        parts.append('<p class="note">* 参考市值/浮盈亏：仅当标的自身有指数行情时，用最新收盘价估算；个股仅供参考，未接入实时个股价。</p>')
    else:
        parts.append('<p class="muted">（当前无持仓记录。）</p>')

    # 3) 状态变化
    parts.append('<h2>三、今日状态切换</h2>')
    if trans:
        df = pd.DataFrame(trans)
        cols = [
            {"key": "name", "label": "板块"},
            {"key": "from", "label": "原状态"},
            {"key": "to", "label": "新状态"},
            {"key": "action", "label": "动作"},
            {"key": "signal", "label": "通用信号"},
        ]
        parts.append(_df_to_html(df, cols))
        parts.append(f'<p class="note">共 {len(trans)} 个板块发生状态切换。</p>')
    else:
        parts.append('<p class="muted">（当日无状态切换记录。）</p>')

    # 4) 镜像信号
    parts.append('<h2>四、镜像信号</h2>')
    if mir.get("ok") and mir.get("pairs"):
        df = pd.DataFrame(mir["pairs"])
        cols = [
            {"key": "strong_name", "label": "强势板块"},
            {"key": "strong_state", "label": "状态"},
            {"key": "weak_name", "label": "弱势板块"},
            {"key": "weak_state", "label": "状态"},
            {"key": "group", "label": "关联组"},
            {"key": "confidence", "label": "置信度", "fmt": lambda v: f"{float(v):.2f}"},
            {"key": "is_new", "label": "新增", "color_map": {True: UP_COLOR, False: NEU_COLOR},
             "fmt": lambda v: "●新增" if v else "—"},
        ]
        parts.append(_df_to_html(df, cols))
        new_cnt = len(mir.get("new_pairs", []))
        new_str = f"，其中 {new_cnt} 对为当日新增" if new_cnt else ""
        parts.append(f'<p class="note">共识别 {len(mir["pairs"])} 对镜像对{new_str}。'
                     f'资金迁移方向：由弱势板块流向其镜像强势板块。</p>')
    else:
        parts.append('<p class="muted">（当前无镜像对。）</p>')

    # 5) 操作建议
    parts.append('<h2>五、操作建议（持仓待处理）</h2>')
    if adv.get("ok") and adv.get("items"):
        df = pd.DataFrame(adv["items"])
        cols = [
            {"key": "优先级", "label": "优先级", "color_map": {"高": DANGER_COLOR, "中": WARN_COLOR, "低": NEU_COLOR}},
            {"key": "类别", "label": "类别"},
            {"key": "名称", "label": "标的"},
            {"key": "行业状态", "label": "行业状态"},
            {"key": "待处理事项", "label": "待处理事项"},
            {"key": "依据", "label": "依据"},
        ]
        parts.append(_df_to_html(df, cols))
    else:
        parts.append('<p class="muted">（无持仓待处理事项；或无持仓记录。）</p>')
    parts.append('<p class="note">以上为可追溯的复核事项，不构成买卖指令。</p>')

    # 6) 信号绩效
    parts.append('<h2>六、信号绩效摘要（近30日）</h2>')
    if perf.get("ok") and perf.get("overview"):
        ov = perf["overview"]
        d = ov.get("direction")
        if d is not None and not d.empty:
            cols = [
                {"key": "signal_direction", "label": "信号方向"},
                {"key": "samples", "label": "样本"},
                {"key": "win_rate", "label": "胜率", "color_key": "win_rate",
                 "fmt": lambda v: _pct(v) if v is not None else "—"},
                {"key": "avg_return_t20", "label": "平均20日收益", "color_key": "avg_return_t20",
                 "fmt": lambda v: _pct(v) if v is not None else "—"},
            ]
            parts.append(_df_to_html(d, cols))
        alerts = perf.get("alerts")
        if alerts is not None and not alerts.empty:
            parts.append('<p style="color:#c62828;font-weight:600;">⚠ 失效预警（近90日，样本≥30 且胜率<40%）：</p>')
            cols = [
                {"key": "to_state", "label": "信号类型"},
                {"key": "samples", "label": "样本"},
                {"key": "win_rate", "label": "胜率", "color_key": "win_rate",
                 "fmt": lambda v: _pct(v) if v is not None else "—"},
            ]
            parts.append(_df_to_html(alerts, cols))
        else:
            parts.append('<p class="muted">（近90日无触发失效预警的信号类型。）</p>')
    else:
        parts.append('<p class="muted">（信号绩效数据暂不可用，请先运行信号回补。）</p>')

    # 7) 风险预警
    parts.append('<h2>七、风险预警</h2>')
    if risks:
        shown = risks[:25]
        for rk in shown:
            parts.append(
                f'<div class="risk risk-{rk.get("level","低")}">'
                f'<b>[{rk.get("level")}] {rk.get("category")}</b> {rk.get("text")}</div>'
            )
        if len(risks) > len(shown):
            parts.append(f'<p class="note">（共 {len(risks)} 条，已显示最严重的 {len(shown)} 条。）</p>')
    else:
        parts.append('<p class="muted">（未检测到显著风险预警。）</p>')

    # 8) 信号仲裁概览
    arb = r.get("arbitration", {}) or {}
    if arb.get("ok"):
        c = arb.get("counts", {})
        parts.append('<h2>八、信号仲裁概览</h2>')
        parts.append(
            f'<p class="note">九宫格 × 资金流 × 研报 三路信号交叉验证：'
            f'强确认 <b>{c.get("强确认", 0)}</b> ／ 弱确认 <b>{c.get("弱确认", 0)}</b> ／ '
            f'否决 <b>{c.get("否决", 0)}</b>（共 {arb.get("n", 0)} 板块）。'
            f'仅做确认/降级/否决，不改变九宫格状态或综合评分。</p>'
        )

    parts.append('</div>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------
def generate_report(as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """生成并归档当日盘后报告。

    返回 dict：as_of_date / html_path / meta_path / html / report。
    """
    sm = StateMachine(ParquetStore(), SQLiteStore())
    sqlite = SQLiteStore()
    cb = CircuitBreaker(ParquetStore(), sm)
    sc = SectorScoring(ParquetStore(), SQLiteStore(), sm)
    mp = MirrorPair(sqlite, sm)

    # 解析 as_of_date（默认取最新状态数据日期）
    if not as_of_date:
        try:
            sdf = sm.calc_all_sectors_state()
            if sdf is not None and not sdf.empty and "date" in sdf.columns:
                as_of_date = str(sdf["date"].max())[:10]
        except Exception:
            pass
    if not as_of_date:
        as_of_date = datetime.now().strftime("%Y-%m-%d")

    meta = _gather_meta(as_of_date)
    # 只全量计算一次状态序列与市场状态，向下传递，避免各区块重复重算
    state_df = None
    try:
        state_df = sm.calc_all_sectors_state(date=as_of_date)
    except Exception as e:
        logger.warning("计算全市场状态失败: %s", e)
    market_status = None
    try:
        market_status = cb.check_market_status(date=as_of_date)
    except Exception as e:
        logger.warning("检查市场环境失败: %s", e)

    report = {
        "meta": meta,
        "market": _gather_market(sm, cb, sc, as_of_date, state_df=state_df, market_status=market_status),
        "holdings": _gather_holdings(sm, as_of_date, state_df=state_df),
        "transitions": _gather_transitions(sqlite, as_of_date),
        "mirrors": _gather_mirrors(mp, as_of_date),
        "advice": _gather_advice(sm, as_of_date, state_df=state_df),
        "arbitration": _gather_arbitration(state_df=state_df),
        "performance": _gather_performance(sqlite),
        "risks": _gather_risks(sm, cb, as_of_date, meta, state_df=state_df, market_status=market_status),
    }
    html = _build_html(report)

    html_path = os.path.join(REPORT_DIR, f"report_{as_of_date}.html")
    meta_path = os.path.join(REPORT_DIR, f"report_{as_of_date}.json")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({
            "as_of_date": as_of_date,
            "generated_at": meta["generated_at"],
            "git_hash": meta["git_hash"],
            "data_lag_days": meta["data_lag_days"],
            "has_holdings": bool(report["holdings"].get("positions")),
            "n_transitions": len(report["transitions"]),
            "n_mirrors": len(report["mirrors"].get("pairs", [])),
            "n_risks": len(report["risks"]),
        }, f, ensure_ascii=False, indent=2)

    logger.info("盘后报告已生成：%s（%d 切换 / %d 镜像 / %d 风险）",
                html_path, len(report["transitions"]),
                len(report["mirrors"].get("pairs", [])), len(report["risks"]))
    return {"as_of_date": as_of_date, "html_path": html_path,
            "meta_path": meta_path, "html": html, "report": report}


def _list_report_files() -> List[str]:
    if not os.path.isdir(REPORT_DIR):
        return []
    files = [f for f in os.listdir(REPORT_DIR) if f.startswith("report_") and f.endswith(".html")]
    files.sort(reverse=True)
    return files


def load_report(as_of_date: Optional[str] = None) -> Optional[str]:
    """读取已归档的报告 HTML 内容；as_of_date 省略时取最近一份。"""
    files = _list_report_files()
    if not files:
        return None
    fname = None
    if as_of_date:
        target = f"report_{as_of_date}.html"
        if target in files:
            fname = target
    else:
        fname = files[0]
    if not fname:
        return None
    path = os.path.join(REPORT_DIR, fname)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def list_reports() -> List[Dict[str, Any]]:
    """列出已归档报告（按日期倒序），含元数据。"""
    out = []
    for f in _list_report_files():
        date_str = f.replace("report_", "").replace(".html", "")
        meta_path = os.path.join(REPORT_DIR, f.replace(".html", ".json"))
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as fh:
                    meta = json.load(fh)
            except Exception:
                pass
        out.append({
            "date": date_str,
            "html_path": os.path.join(REPORT_DIR, f),
            "meta": meta,
        })
    return out


if __name__ == "__main__":
    res = generate_report()
    print("generated:", res["as_of_date"], "->", res["html_path"])

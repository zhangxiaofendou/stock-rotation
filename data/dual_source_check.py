"""
双源校验（AkShare / 第二数据源）
===============================
PRD §5.6.2「双源校验」：对关键行情（如沪深300指数收盘）分别从 AkShare 与
第二数据源取数比对，差异超阈值则告警，及早发现单源接口异常/数据错乱。

第二数据源（按优先级自动选择）：
  1. Tushare Pro：配置了 TUSHARE_TOKEN 时使用（历史日线完整，盘后 T+1）。
  2. 东方财富公开接口（EastMoneyLiveSource）：无需 token，行情到最新交易日收盘，
     作为「零配置」的兜底第二源，让运行保障面板从「未配置」变为「已配置」。

设计原则：
  - 任一第二源可用即执行比对；两者都不可用时跳过（status=skipped），不依赖、不报错。
  - 单源取数失败只告警不阻断。
  - 返回结构化结果，供管线运行日志与侧栏展示复用。
"""

from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from config.logger import get_logger

logger = get_logger(__name__)

# 校验标的：沪深300 指数（最能代表全市场，单源错乱时影响最大）
CHECK_SYMBOLS = [
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
]

# 收盘价差异告警阈值（相对值）；超过则视为潜在数据异常
DIFF_WARN_PCT = 0.005  # 0.5%


def _get_tushare_token() -> Optional[str]:
    """从环境变量或 config 读取 Tushare token（不强制依赖 tushare 包）。"""
    import os
    token = os.environ.get("TUSHARE_TOKEN")
    if token:
        return token
    try:
        import json
        from config.settings import PROJECT_ROOT
        cfg = os.path.join(str(PROJECT_ROOT), "config", "tushare.json")
        if os.path.exists(cfg):
            with open(cfg, "r", encoding="utf-8") as f:
                return json.load(f).get("token")
    except Exception:
        pass
    return None


def _get_second_source() -> Optional[Tuple[str, object]]:
    """选择第二数据源。优先 Tushare（需 token），否则回退东方财富（无需 token）。

    返回 (kind, source) 或 None。kind ∈ {"tushare", "eastmoney"}。
    """
    # 1. Tushare（若已配置 token）
    token = _get_tushare_token()
    if token:
        try:
            import tushare as ts
            logger.info("双源校验：使用 Tushare 作为第二数据源。")
            return ("tushare", ts.pro_api(token))
        except Exception as e:
            logger.warning("Tushare 不可用，回退东方财富：%s", e)

    # 2. 东方财富公开接口（无需 token）
    try:
        from data.sources.eastmoney_source import EastMoneyLiveSource
        logger.info("双源校验：使用东方财富作为第二数据源（无需 token）。")
        return ("eastmoney", EastMoneyLiveSource())
    except Exception as e:
        logger.warning("东方财富第二源不可用：%s", e)

    return None


def run_dual_source_check() -> Dict:
    """执行双源校验。

    返回:
        {
            "configured": bool,        # 是否配置了第二数据源
            "source": str,             # 实际使用的第二源类型 "tushare"/"eastmoney"/None
            "status": "skipped" | "ok" | "warn" | "error",
            "max_diff_pct": float,     # 最大差异（相对值，百分比）
            "details": [...],          # 每标的比对明细
            "checked_at": str,
        }
    """
    second = _get_second_source()
    result: Dict = {
        "configured": bool(second),
        "source": second[0] if second else None,
        "status": "skipped",
        "max_diff_pct": 0.0,
        "details": [],
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not second:
        result["status"] = "skipped"
        logger.info("双源校验：未配置任何第二数据源（Tushare/EastMoney 均不可用），跳过。")
        return result

    kind, src = second
    try:
        from data.sources.akshare_source import AkShareSource
        ak = AkShareSource()
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        max_diff = 0.0
        details = []
        warn = False

        for code, name in CHECK_SYMBOLS:
            try:
                # 第二源取值
                if kind == "tushare":
                    df_2 = src.index_daily(ts_code=code, start_date=start, end_date=end)
                    if df_2 is None or df_2.empty:
                        details.append({"name": name, "diff_pct": None, "note": "第二源(Tushare)取数为空"})
                        continue
                    last_2 = float(df_2.sort_values("trade_date")["close"].iloc[-1])
                    src_label = "tushare_close"
                else:  # eastmoney
                    df_2 = src.get_benchmark_hist(symbol="sh" + code.split(".")[0])
                    if df_2 is None or df_2.empty:
                        details.append({"name": name, "diff_pct": None, "note": "第二源(东方财富)取数为空"})
                        continue
                    last_2 = float(df_2.sort_values("date")["close"].iloc[-1])
                    src_label = "eastmoney_close"

                # 第一源（AkShare）取值
                ak_symbol = "sh" + code.split(".")[0]
                df_ak = ak.get_benchmark_hist(symbol=ak_symbol)
                if df_ak is None or df_ak.empty:
                    details.append({"name": name, "diff_pct": None, "note": "AkShare 取数为空"})
                    continue
                last_ak = float(df_ak.sort_values("date")["close"].iloc[-1])

                diff = abs(last_2 - last_ak) / last_ak if last_ak else 0.0
                max_diff = max(max_diff, diff)
                if diff > DIFF_WARN_PCT:
                    warn = True
                details.append({
                    "name": name,
                    "akshare_close": round(last_ak, 3),
                    src_label: round(last_2, 3),
                    "diff_pct": round(diff * 100, 3),
                })
            except Exception as e:
                details.append({"name": name, "diff_pct": None, "note": f"比对失败: {e}"})

        result["max_diff_pct"] = round(max_diff * 100, 3)
        result["details"] = details
        result["status"] = "warn" if warn else "ok"
        logger.info("双源校验完成（第二源=%s）：最大差异 %.3f%%（status=%s）", kind, max_diff * 100, result["status"])
    except Exception as e:
        result["status"] = "error"
        result["details"].append(f"校验异常: {e}")
        logger.warning("双源校验异常: %s", e)

    return result

"""
双源校验（AkShare / Tushare）
===========================
PRD §5.6.2「双源校验」：对关键行情（如沪深300指数收盘）分别从 AkShare 与
Tushare 取数比对，差异超阈值则告警，及早发现单源接口异常/数据错乱。

设计：
  - Tushare 为可选第二数据源：未配置 TUSHARE_TOKEN 时直接跳过（status=skipped），
    不依赖、不报错，保证单源环境也能跑。
  - 仅在配置了 token 时真正发起比对；取数失败也只告警不阻断。
  - 返回结构化结果，供管线运行日志与侧栏展示复用。
"""

from datetime import datetime, timedelta
from typing import Dict, Optional

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


def run_dual_source_check() -> Dict:
    """执行双源校验。

    返回:
        {
            "configured": bool,    # 是否配置了第二数据源
            "status": "skipped" | "ok" | "warn" | "error",
            "max_diff_pct": float, # 最大差异（相对值，百分比）
            "details": [...],      # 每标的比对明细
            "checked_at": str,
        }
    """
    token = _get_tushare_token()
    result: Dict = {
        "configured": bool(token),
        "status": "skipped",
        "max_diff_pct": 0.0,
        "details": [],
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not token:
        result["status"] = "skipped"
        logger.info("双源校验：未配置 TUSHARE_TOKEN，跳过（单源运行）。")
        return result

    try:
        import tushare as ts
    except ImportError:
        result["status"] = "error"
        result["details"].append("tushare 包未安装，无法执行双源校验")
        logger.warning("双源校验：tushare 未安装。")
        return result

    try:
        from data.sources.akshare_source import AkShareSource
        pro = ts.pro_api(token)
        ak = AkShareSource()
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        max_diff = 0.0
        details = []
        warn = False

        for code, name in CHECK_SYMBOLS:
            try:
                df_ts = pro.index_daily(ts_code=code, start_date=start, end_date=end)
                ak_symbol = "sh" + code.split(".")[0]
                df_ak = ak.get_benchmark_hist(symbol=ak_symbol)
                if df_ts is None or df_ts.empty or df_ak is None or df_ak.empty:
                    details.append({"name": name, "diff_pct": None, "note": "一侧取数为空"})
                    continue
                last_ts = float(df_ts.sort_values("trade_date")["close"].iloc[-1])
                last_ak = float(df_ak.sort_values("date")["close"].iloc[-1])
                diff = abs(last_ts - last_ak) / last_ts if last_ts else 0.0
                max_diff = max(max_diff, diff)
                if diff > DIFF_WARN_PCT:
                    warn = True
                details.append({
                    "name": name, "tushare_close": last_ts,
                    "akshare_close": last_ak, "diff_pct": round(diff * 100, 3),
                })
            except Exception as e:
                details.append({"name": name, "diff_pct": None, "note": f"比对失败: {e}"})

        result["max_diff_pct"] = round(max_diff * 100, 3)
        result["details"] = details
        result["status"] = "warn" if warn else "ok"
        logger.info("双源校验完成：最大差异 %.3f%%（status=%s）", max_diff * 100, result["status"])
    except Exception as e:
        result["status"] = "error"
        result["details"].append(f"校验异常: {e}")
        logger.warning("双源校验异常: %s", e)

    return result

"""
个股多因子特征工程
====================
从本地个股行情（stock_hist：open/high/low/close/volume）计算多因子。
所有因子均为「数值越大越好」的口径（已在 multi_factor 中统一 z-score 合成），
便于透明合成与可解释排序。

因子清单（PRD §7.3 P6：ML多因子选股 + 特征工程）：
  - momentum_20d    近20日收益率（动量）
  - recent_strength 近5日收益率（短期强度）
  - volume_trend    近5日均量 / 前20日均量（资金关注度的粗略代理）
  - low_vol         -近20日收益波动率（越低越好 → 取负）
  - low_drawdown    -近20日最大回撤（越小越好 → 取负）
  - rs_sector       个股20日收益 - 所属板块指数20日收益（相对板块强弱，可选）
"""

from typing import Optional, Dict
import numpy as np
import pandas as pd


def _safe_ret(series: pd.Series) -> float:
    """区间收益率（末/首 - 1），数据不足返回 NaN。"""
    s = series.dropna()
    if len(s) < 2:
        return float("nan")
    return float(s.iloc[-1] / s.iloc[0] - 1.0)


def _safe_vol(series: pd.Series) -> float:
    """日收益波动率（标准差，年化近似），数据不足返回 NaN。"""
    s = series.dropna()
    if len(s) < 3:
        return float("nan")
    r = s.pct_change().dropna()
    if len(r) < 2:
        return float("nan")
    return float(r.std() * np.sqrt(252))


def _max_drawdown(series: pd.Series) -> float:
    """最大回撤（负值，0 表示无回撤），数据不足返回 0。"""
    s = series.dropna()
    if len(s) < 2:
        return 0.0
    roll_max = s.cummax()
    dd = s / roll_max - 1.0
    return float(dd.min())


def engineer_stock_features(
    df: pd.DataFrame,
    sector_ret_20d: Optional[float] = None,
) -> Dict[str, float]:
    """从个股行情 DataFrame 计算因子字典。

    df 需含 date/close/volume 列（close 必填，volume 可选）。
    返回因子名 -> 数值（NaN 表示不可计算）。
    """
    feats: Dict[str, float] = {
        "momentum_20d": float("nan"),
        "recent_strength": float("nan"),
        "volume_trend": float("nan"),
        "low_vol": float("nan"),
        "low_drawdown": float("nan"),
        "rs_sector": float("nan"),
    }
    if df is None or df.empty or "close" not in df.columns:
        return feats

    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"]

    feats["momentum_20d"] = _safe_ret(close.tail(20))
    feats["recent_strength"] = _safe_ret(close.tail(5))
    feats["low_vol"] = -_safe_vol(close.tail(20))
    feats["low_drawdown"] = _max_drawdown(close.tail(20))

    if "volume" in df.columns:
        vol = df["volume"].dropna()
        if len(vol) >= 25:
            recent_v = vol.tail(5).mean()
            prior_v = vol.tail(25).head(20).mean()
            if prior_v and prior_v > 0:
                feats["volume_trend"] = float(recent_v / prior_v - 1.0)

    if sector_ret_20d is not None and not (isinstance(sector_ret_20d, float) and np.isnan(sector_ret_20d)):
        m = feats["momentum_20d"]
        if not np.isnan(m):
            feats["rs_sector"] = float(m - sector_ret_20d)

    return feats


# 因子方向说明（用于卡片展示）：均为「越大越好」。
FACTOR_LABELS = {
    "momentum_20d": "20日动量",
    "recent_strength": "5日强度",
    "volume_trend": "量能趋势",
    "low_vol": "低波动(反向)",
    "low_drawdown": "低回撤(反向)",
    "rs_sector": "相对板块强弱",
}

# 默认因子权重（横截面 z-score 合成；rs_sector 缺失时按比例缩放）。
BASE_WEIGHTS = {
    "momentum_20d": 0.35,
    "recent_strength": 0.15,
    "volume_trend": 0.10,
    "low_vol": 0.20,
    "low_drawdown": 0.20,
}
RS_WEIGHT = 0.15  # rs_sector 可用时从 BASE 中匀出 0.15

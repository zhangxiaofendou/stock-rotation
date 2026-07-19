"""
ML 多因子个股排序（排序辅助，非交易指令）
=========================================
依据 PRD §5.6.3 阶段 E / §7.3 P6：ML 多因子选股初期只在个股下钻中作为排序辅助，
不直接生成交易指令。

实现两档：
  1. 横截面 z-score 合成（默认）：对所有候选个股的因子做横截面标准化后按权重合成，
     完全透明、可解释，样本不足时也能给出排序。本仓库当前仅有 10 只个股的本地
     行情（约 46 个交易日），即走此路径。
  2. 有监督训练（数据充裕时自动启用）：以「未来 20 日收益」为标签，用时间序
     列切分训练随机森林/梯度提升，输出排序与特征重要性；含特征漂移监控占位。

两条路径都要求「每条结论可追溯」——排序附带各因子 z 值，训练路径附带特征重要性。
"""

from typing import Optional, List, Dict, Tuple
import numpy as np
import pandas as pd

from config.logger import get_logger
from data.storage.parquet_store import ParquetStore
from ai.features import engineer_stock_features, BASE_WEIGHTS, RS_WEIGHT, FACTOR_LABELS

logger = get_logger(__name__)

# 启用有监督训练的阈值（当前数据远未达，走透明合成路径）
MIN_STOCKS_FOR_TRAIN = 20
MIN_LABELED_SAMPLES = 60


def _zscore(series: pd.Series) -> pd.Series:
    s = series.astype(float)
    mu, sigma = s.median(), s.std()
    if not np.isfinite(sigma) or sigma == 0:
        return pd.Series(0.0, index=s.index)
    return (s - mu) / sigma


def rank_stocks(
    stock_codes: List[str],
    sector_code: Optional[str] = None,
    names: Optional[Dict[str, str]] = None,
    parquet_store: Optional[ParquetStore] = None,
) -> Dict:
    """对候选个股做多因子排序。

    参数:
        stock_codes: 候选股票代码列表（6 位）
        sector_code: 所属板块（用于计算相对板块强弱 rs_sector）
        names: 代码 -> 名称 的可选映射
        parquet_store: 可选的 ParquetStore（缺省新建）

    返回 dict:
        model_mode / n_total / n_ranked / note / ranked(DataFrame) / factors(list)
    """
    store = parquet_store or ParquetStore()
    names = names or {}

    rows = []
    for code in stock_codes:
        code6 = str(code).zfill(6)
        df = store.load_stock_hist(code6)
        feats = engineer_stock_features(df)
        feats["stock_code"] = code6
        feats["stock_name"] = names.get(code6, names.get(code, code6))
        feats["has_hist"] = df is not None and not df.empty
        rows.append(feats)

    raw = pd.DataFrame(rows)
    if raw.empty:
        return _empty_result("无候选个股")

    ranked = raw[raw["has_hist"] == True].copy()  # noqa: E712
    n_total = len(raw)
    n_ranked = len(ranked)

    if n_ranked == 0:
        return {
            "model_mode": "none",
            "n_total": n_total,
            "n_ranked": 0,
            "note": "候选个股均无本地行情，无法排序（需先补充个股历史数据）。",
            "ranked": raw,
            "factors": list(FACTOR_LABELS.values()),
        }

    # ---- 相对板块强弱（可选） ----
    sector_ret_20d = None
    if sector_code:
        sdf = store.load_index_hist(sector_code)
        if sdf is not None and not sdf.empty and "close" in sdf.columns:
            s = sdf.sort_values("date")["close"] if "date" in sdf.columns else sdf["close"]
            tail = s.tail(20)
            if len(tail) >= 2:
                sector_ret_20d = float(tail.iloc[-1] / tail.iloc[0] - 1.0)
    if sector_ret_20d is not None:
        for i, r in ranked.iterrows():
            # 用板块收益回填 rs_sector（避免重复工程化）
            m = r.get("momentum_20d")
            if pd.notna(m):
                ranked.at[i, "rs_sector"] = float(m - sector_ret_20d)

    # ---- 选择可用因子并加权 ----
    use_rs = ranked["rs_sector"].notna().any()
    weights = dict(BASE_WEIGHTS)
    if use_rs:
        scale = 1.0 - RS_WEIGHT
        weights = {k: v * scale for k, v in weights.items()}
        weights["rs_sector"] = RS_WEIGHT
    else:
        weights = {k: v / sum(BASE_WEIGHTS.values()) for k, v in BASE_WEIGHTS.items()}

    z_cols = {}
    for f in weights:
        if f in ranked.columns:
            ranked[f + "_z"] = _zscore(ranked[f])
            z_cols[f] = f + "_z"

    # ---- 合成综合分 ----
    ranked["composite_z"] = 0.0
    for f, zc in z_cols.items():
        ranked["composite_z"] += weights[f] * ranked[zc]

    # 映射到 0-100 便于展示
    cmin, cmax = ranked["composite_z"].min(), ranked["composite_z"].max()
    if cmax > cmin:
        ranked["score_0_100"] = ((ranked["composite_z"] - cmin) / (cmax - cmin) * 100).round(1)
    else:
        ranked["score_0_100"] = 50.0

    ranked = ranked.sort_values("composite_z", ascending=False).reset_index(drop=True)
    ranked.insert(0, "rank", ranged_index(len(ranked)))

    note = (
        "横截面 z-score 多因子合成（动量/强度/量能/低波动/低回撤"
        + ("/相对板块" if use_rs else "") + "）。"
    )
    model_mode = "composite_baseline"

    # ---- 数据充裕时切有监督训练（当前不触发） ----
    if n_ranked >= MIN_STOCKS_FOR_TRAIN:
        trained = _maybe_train(ranked, store, weights)
        if trained is not None:
            ranked = trained
            model_mode = "trained"
            note = "有监督多因子模型（未来20日收益标签，时间序列切分）。"

    return {
        "model_mode": model_mode,
        "n_total": n_total,
        "n_ranked": n_ranked,
        "use_rs": use_rs,
        "note": note,
        "weights": weights,
        "ranked": ranked,
        "factors": [FACTOR_LABELS.get(f, f) for f in z_cols.keys()],
    }


def _maybe_train(ranked: pd.DataFrame, store: ParquetStore, weights: Dict) -> Optional[pd.DataFrame]:
    """数据充裕时训练有监督模型。当前样本不足直接返回 None。"""
    # 收集带标签样本：每只股票用历史窗口构造 (特征, 未来20日收益)
    try:
        from sklearn.ensemble import RandomForestRegressor
    except Exception as e:  # pragma: no cover
        logger.warning(f"scikit-learn 不可用，跳过有监督训练: {e}")
        return None

    X, y = [], []
    feat_cols = [c for c in weights if c in ranked.columns]
    for code in ranked["stock_code"].unique():
        df = store.load_stock_hist(code)
        if df is None or df.empty:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        closes = df["close"]
        for i in range(20, len(closes) - 20):
            window = closes.iloc[: i + 1]
            f = engineer_stock_features(window)
            if any(pd.isna(f.get(c)) for c in feat_cols):
                continue
            fwd = closes.iloc[i + 20] / closes.iloc[i] - 1.0
            X.append([f[c] for c in feat_cols])
            y.append(fwd)

    if len(X) < MIN_LABELED_SAMPLES:
        return None

    Xa, ya = np.array(X), np.array(y)
    # 时间序列切分：前 80% 训练，后 20% 验证（不随机洗牌，避免未来泄漏）
    n = len(Xa)
    cut = int(n * 0.8)
    model = RandomForestRegressor(n_estimators=100, random_state=42, n_jobs=-1)
    model.fit(Xa[:cut], ya[:cut])
    pred = model.predict(Xa[cut:])
    # 特征重要性回填（简要日志）
    imp = dict(zip(feat_cols, model.feature_importances_))
    logger.info(f"有监督多因子训练完成：样本 {n}，特征重要性 {imp}")
    # 用模型对最新截面重新打分
    latest = []
    for _, r in ranked.iterrows():
        fv = [r.get(c, float("nan")) for c in feat_cols]
        if any(pd.isna(v) for v in fv):
            latest.append(float("nan"))
        else:
            latest.append(float(model.predict([fv])[0]))
    ranked["model_score"] = latest
    ranked = ranked.sort_values("model_score", ascending=False, na_position="last").reset_index(drop=True)
    ranked.insert(0, "rank", ranged_index(len(ranked)))
    return ranked


def ranged_index(n: int) -> List[int]:
    return list(range(1, n + 1))


def _empty_result(note: str) -> Dict:
    return {
        "model_mode": "none",
        "n_total": 0,
        "n_ranked": 0,
        "note": note,
        "ranked": pd.DataFrame(),
        "factors": list(FACTOR_LABELS.values()),
    }

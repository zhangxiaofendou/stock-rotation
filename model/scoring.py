"""
板块综合评分（0-100）
====================
基于 RS（相对强度）的多维度加权评分，核心引入「横截面」维度。

评分维度（加权合计 100%）：
  - RS横截面      30%：当天全市场 RS 排名分位（横向选强）
  - 动量横截面    30%：当天全市场 RS动量 排名分位（横向选加速者）
  - RS时序分位    20%：RS 在自身历史中的位置（过滤绝对弱势）
  - 动量时序分位  20%：RS动量 在自身历史中的位置（确认方向）

设计原则：
  - 横截面为主（60%）：轮动的本质是横向比较，必须看「今天全市场里谁最强」。
  - 时序为辅（40%）：自身历史位置用于过滤与确认，避免把「最强里的矮子」选进来。
  - 趋势/拥挤度/资金流不计入评分：趋势与 RS 高度重叠；拥挤度改为风险提示
    角标（追强不该因拥挤就压分）；资金流暂未接入批量数据（恒为中性）。
"""

from typing import Optional, List, Dict
import numpy as np
import pandas as pd
import os

from config.logger import get_logger
from config.settings import PARQUET_DIR

logger = get_logger(__name__)

# 综合评分权重（合计 = 1.0）
WEIGHTS = {
    "rs_cross": 0.30,       # RS横截面
    "mom_cross": 0.30,      # 动量横截面
    "rs_position": 0.20,    # RS时序分位
    "mom_position": 0.20,   # 动量时序分位
}

# 评分结果列（同时作为旧缓存兼容性判据：缺这些列即视为旧快照）
SCORE_COLUMNS = [
    "sector_code", "sector_name", "score", "state", "rank",
    "rs_position_score", "rs_momentum_score", "rs_cross_score",
    "mom_cross_score", "crowding_score",
]


class SectorScoring:
    """板块综合评分（0-100）"""

    def __init__(self, parquet_store=None, sqlite_store=None, state_machine=None):
        """
        初始化评分计算器

        参数:
            parquet_store: ParquetStore 实例
            sqlite_store: SQLiteStore 实例
            state_machine: StateMachine 实例
        """
        self.parquet_store = parquet_store
        self.sqlite_store = sqlite_store
        self.state_machine = state_machine

    # ============================================================
    # 数据加载
    # ============================================================
    def _load_rs_data(self, sector_code: str) -> Optional[pd.DataFrame]:
        """加载RS指标数据"""
        rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "rs")
        safe_code = sector_code.replace(".", "_")
        rs_path = os.path.join(rs_dir, f"{safe_code}.parquet")
        if not os.path.exists(rs_path):
            return None
        df = pd.read_parquet(rs_path)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    def _load_trend_data(self, sector_code: str) -> Optional[pd.DataFrame]:
        """加载趋势数据"""
        trend_dir = os.path.join(str(PARQUET_DIR), "indicators", "trend")
        safe_code = sector_code.replace(".", "_")
        trend_path = os.path.join(trend_dir, f"{safe_code}.parquet")
        if not os.path.exists(trend_path):
            return None
        df = pd.read_parquet(trend_path)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    def _load_crowding_data(self, sector_code: str) -> Optional[pd.DataFrame]:
        """加载拥挤度数据"""
        crowd_dir = os.path.join(str(PARQUET_DIR), "indicators", "crowding")
        safe_code = sector_code.replace(".", "_")
        crowd_path = os.path.join(crowd_dir, f"{safe_code}.parquet")
        if not os.path.exists(crowd_path):
            return None
        df = pd.read_parquet(crowd_path)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    # ============================================================
    # 批量评分（主路径，含横截面）
    # ============================================================
    def _safe_float(self, val, default=np.nan):
        """安全转 float，缺失/NaN 返回 default"""
        try:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return default
            return float(val)
        except (TypeError, ValueError):
            return default

    def _gather_latest(self, date: str = None) -> Optional[pd.DataFrame]:
        """
        汇总所有板块的最新一行指标，返回一个 DataFrame。

        列：sector_code, rs, rs_percentile, rs_momentum, rs_momentum_percentile,
            trend, crowding_score
        """
        rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "rs")
        if not os.path.exists(rs_dir):
            logger.error(f"RS指标目录不存在: {rs_dir}")
            return None

        rows = []
        for f in os.listdir(rs_dir):
            if not f.endswith(".parquet"):
                continue
            code = f.replace(".parquet", "").replace("_", ".", 1)
            if not code.endswith(".SI"):
                code = code.replace("_SI", ".SI")

            rs_df = self._load_rs_data(code)
            if rs_df is None or rs_df.empty:
                continue
            rs_row = rs_df.iloc[-1]

            rec = {
                "sector_code": code,
                "rs": self._safe_float(rs_row["rs"]) if "rs" in rs_row else np.nan,
                "rs_percentile": self._safe_float(rs_row["rs_percentile"]) if "rs_percentile" in rs_row else np.nan,
                "rs_momentum": self._safe_float(rs_row["rs_momentum"]) if "rs_momentum" in rs_row else np.nan,
                "rs_momentum_percentile": self._safe_float(rs_row["rs_momentum_percentile"]) if "rs_momentum_percentile" in rs_row else np.nan,
                "trend": "横盘",
                "crowding_score": np.nan,
            }

            # 趋势（用于九宫格状态判定）
            trend_df = self._load_trend_data(code)
            if trend_df is not None and not trend_df.empty and "trend" in trend_df.columns:
                rec["trend"] = trend_df.iloc[-1]["trend"]

            # 拥挤度（移出评分，保留为风险提示列）
            crowd_df = self._load_crowding_data(code)
            if crowd_df is not None and not crowd_df.empty and "crowding_score" in crowd_df.columns:
                rec["crowding_score"] = self._safe_float(crowd_df.iloc[-1]["crowding_score"])

            rows.append(rec)

        if not rows:
            logger.error("未找到任何板块数据")
            return None

        return pd.DataFrame(rows)

    def calc_all_scores(self, date: str = None) -> Optional[pd.DataFrame]:
        """
        实时计算所有板块评分并排序（无磁盘快照，每次调用即时计算）。

        评分公式（横截面为主、时序为辅）：
            score = 0.30·RS横截面 + 0.30·动量横截面
                  + 0.20·RS时序分位 + 0.20·动量时序分位

        参数:
            date: 目标日期（None=最新）

        返回:
            DataFrame: 见 SCORE_COLUMNS（始终含全部 10 列，数据缺失处以中性值填充）
        """
        # 清理任何历史遗留的评分快照文件：本系统已改为实时计算，不再读写磁盘快照。
        # 这一行确保云端/本地残留的旧 score_snapshot*.parquet 被彻底清除，不留后患。
        _cache_dir = os.path.join(str(PARQUET_DIR), "cache")
        for _f in ["score_snapshot.parquet", "score_snapshot_v2.parquet"]:
            _p = os.path.join(_cache_dir, _f)
            try:
                if os.path.exists(_p):
                    os.remove(_p)
                    logger.info(f"清理历史评分快照: {_p}")
            except OSError:
                pass

        logger.info(f"实时计算所有板块评分（含横截面）, date={date or '最新'}")

        df = self._gather_latest(date)
        if df is None or df.empty:
            return None

        # ---- 横截面：当天全市场排名分位 ----
        df["rs_cross"] = df["rs"].rank(pct=True) * 100
        df["mom_cross"] = df["rs_momentum"].rank(pct=True) * 100

        # ---- 四个组件（裁剪到 0-100，缺失填中性 50）----
        rs_pos = df["rs_percentile"].clip(0, 100).fillna(50)
        rs_mom = df["rs_momentum_percentile"].clip(0, 100).fillna(50)
        rs_cross = df["rs_cross"].clip(0, 100).fillna(50)
        mom_cross = df["mom_cross"].clip(0, 100).fillna(50)

        # ---- 综合评分（加权合计 100%）----
        df["score"] = (
            WEIGHTS["rs_cross"] * rs_cross
            + WEIGHTS["mom_cross"] * mom_cross
            + WEIGHTS["rs_position"] * rs_pos
            + WEIGHTS["mom_position"] * rs_mom
        ).round(1)

        # 组件分（透传，便于核对/后续展示）
        df["rs_position_score"] = rs_pos.round(1)
        df["rs_momentum_score"] = rs_mom.round(1)
        df["rs_cross_score"] = rs_cross.round(1)
        df["mom_cross_score"] = mom_cross.round(1)
        df["crowding_score"] = df["crowding_score"].clip(0, 100).fillna(50).round(1)

        # ---- 九宫格状态（依赖 trend + 动量时序分位）----
        if self.state_machine is not None:
            try:
                def _state(r):
                    mom = r["rs_momentum_percentile"]
                    if mom is None or (isinstance(mom, float) and np.isnan(mom)):
                        mom = 50
                    return self.state_machine.determine_state(r["trend"], mom)
                df["state"] = df.apply(_state, axis=1)
            except Exception as e:
                logger.error(f"状态判定异常: {e}")
                df["state"] = None
        else:
            df["state"] = None

        # ---- 板块名称 ----
        if self.sqlite_store is not None:
            names = {}
            for code in df["sector_code"].unique():
                try:
                    info = self.sqlite_store.get_sector_by_code(code)
                    if info:
                        names[code] = info.get("name", code)
                except Exception:
                    pass
            df["sector_name"] = df["sector_code"].map(names).fillna(df["sector_code"])
        else:
            df["sector_name"] = df["sector_code"]

        # ---- 排序 + 排名 ----
        df = df.sort_values("score", ascending=False).reset_index(drop=True)
        df["rank"] = df.index + 1

        # 仅保留对外列（始终为完整的 SCORE_COLUMNS，实时计算不依赖磁盘快照）
        df = df[SCORE_COLUMNS]

        logger.info(f"所有板块评分计算完成, 共 {len(df)} 个板块")
        return df

    # ============================================================
    # 获取 Top N 板块
    # ============================================================
    def get_top_sectors(self, n: int = 10, date: str = None) -> Optional[pd.DataFrame]:
        """
        获取评分最高的N个板块

        参数:
            n: 返回数量
            date: 目标日期

        返回:
            DataFrame
        """
        all_scores = self.calc_all_scores(date=date)
        if all_scores is None or all_scores.empty:
            return None

        top_n = all_scores.head(n)
        logger.info(f"Top {n} 板块:\n{top_n[['sector_code', 'sector_name', 'score', 'state']].to_string()}")
        return top_n

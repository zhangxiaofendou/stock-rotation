"""
板块综合评分（0-100）
====================
多维度加权评分，综合RS、趋势、拥挤度、资金流等指标。

评分维度：
  - RS分位（位置）权重25%：当前RS在历史中的位置
  - RS动量分位（方向）权重25%：RS的变化方向
  - 价格趋势权重20%：绝对价格趋势方向
  - 拥挤度权重15%：反向指标，拥挤度高扣分
  - 资金流权重15%：资金流方向
"""

from typing import Optional, List, Dict
import numpy as np
import pandas as pd
import os

from config.logger import get_logger
from config.settings import PARQUET_DIR

logger = get_logger(__name__)


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
        import os
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
        import os
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
        import os
        crowd_dir = os.path.join(str(PARQUET_DIR), "indicators", "crowding")
        safe_code = sector_code.replace(".", "_")
        crowd_path = os.path.join(crowd_dir, f"{safe_code}.parquet")
        if not os.path.exists(crowd_path):
            return None
        df = pd.read_parquet(crowd_path)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)

    def _get_latest_row(self, df: pd.DataFrame, date: str = None):
        """获取DataFrame最新行"""
        if df is None or df.empty:
            return None
        if date is not None:
            target = pd.to_datetime(date)
            rows = df[df["date"] == target]
            if not rows.empty:
                return rows.iloc[-1]
        return df.iloc[-1]

    # ============================================================
    # 各维度评分
    # ============================================================
    def _score_rs_position(self, rs_percentile: float) -> float:
        """
        RS分位评分（0-100）
        高位=强势，但>90扣分（过强可能回调）

        参数:
            rs_percentile: RS分位数 (0-100)

        返回:
            评分 (0-100)
        """
        if rs_percentile is None or np.isnan(rs_percentile):
            return 50.0  # 缺失默认中性

        # 基础分 = RS分位本身
        score = rs_percentile

        # >90% 过强，小幅扣分
        if rs_percentile > 90:
            score = 90 - (rs_percentile - 90) * 0.5
        # <10% 过弱，不再额外加分
        elif rs_percentile < 10:
            score = rs_percentile

        return max(0, min(100, score))

    def _score_rs_momentum(self, rs_momentum_percentile: float) -> float:
        """
        RS动量分位评分（0-100）
        动量增强=高分，减弱=低分

        参数:
            rs_momentum_percentile: RS动量分位数 (0-100)

        返回:
            评分 (0-100)
        """
        if rs_momentum_percentile is None or np.isnan(rs_momentum_percentile):
            return 50.0

        return rs_momentum_percentile

    def _score_trend(self, trend: str) -> float:
        """
        价格趋势评分（0-100）
        上涨=高分，下跌=低分

        参数:
            trend: "上涨"/"横盘"/"下跌"

        返回:
            评分 (0-100)
        """
        trend_scores = {
            "上涨": 80,
            "横盘": 50,
            "下跌": 20,
        }
        return trend_scores.get(trend, 50)

    def _score_crowding(self, crowding_score: float) -> float:
        """
        拥挤度评分（0-100，反向指标）
        拥挤度高=低分（过热风险），拥挤度低=高分（安全）

        参数:
            crowding_score: 拥挤度 (0-100)

        返回:
            评分 (0-100)
        """
        if crowding_score is None or np.isnan(crowding_score):
            return 50.0

        # 反向：拥挤度越高，评分越低
        # 拥挤度>80 = 极度拥挤 → 低分
        # 拥挤度<20 = 冷清 → 高分
        if crowding_score > 80:
            score = 100 - crowding_score  # 80拥挤 → 20分
        elif crowding_score < 20:
            score = 100 - crowding_score  # 10拥挤 → 90分
        else:
            score = 100 - crowding_score  # 线性反向

        return max(0, min(100, score))

    def _score_fund_flow(self, fund_flow_signal: Optional[str] = None) -> float:
        """
        资金流评分（0-100）

        参数:
            fund_flow_signal: "正向"/"中性"/"反向"

        返回:
            评分 (0-100)
        """
        flow_scores = {
            "正向": 80,
            "中性": 50,
            "反向": 20,
        }
        return flow_scores.get(fund_flow_signal, 50)

    # ============================================================
    # 综合评分
    # ============================================================
    def calc_score(
        self,
        sector_code: str,
        date: str = None,
        fund_flow_signal: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        计算单个板块综合评分

        参数:
            sector_code: 板块代码
            date: 目标日期，None表示最新
            fund_flow_signal: 资金流信号（可选，由外部提供）

        返回:
            dict: {
                'sector_code': 板块代码,
                'score': 综合评分 (0-100),
                'rs_position_score': RS分位得分,
                'rs_momentum_score': RS动量得分,
                'trend_score': 趋势得分,
                'crowding_score': 拥挤度得分,
                'fund_flow_score': 资金流得分,
                'state': 九宫格状态,
            }
        """
        logger.info(f"计算板块综合评分: {sector_code}")

        # 加载各维度数据
        rs_df = self._load_rs_data(sector_code)
        trend_df = self._load_trend_data(sector_code)
        crowd_df = self._load_crowding_data(sector_code)

        if rs_df is None:
            logger.warning(f"板块 {sector_code} RS数据不存在，无法评分")
            return None

        # 获取最新行数据
        rs_row = self._get_latest_row(rs_df, date)
        trend_row = self._get_latest_row(trend_df, date)
        crowd_row = self._get_latest_row(crowd_df, date)

        # 提取各维度值
        rs_percentile = rs_row["rs_percentile"] if rs_row is not None and "rs_percentile" in rs_row.index else None
        rs_momentum_pct = rs_row["rs_momentum_percentile"] if rs_row is not None and "rs_momentum_percentile" in rs_row.index else None
        trend = trend_row["trend"] if trend_row is not None and "trend" in trend_row.index else "横盘"
        crowding = crowd_row["crowding_score"] if crowd_row is not None and "crowding_score" in crowd_row.index else None

        # 各维度评分
        rs_pos = self._score_rs_position(rs_percentile)
        rs_mom = self._score_rs_momentum(rs_momentum_pct)
        trend_s = self._score_trend(trend)
        crowd_s = self._score_crowding(crowding)
        flow_s = self._score_fund_flow(fund_flow_signal)

        # 加权综合评分
        weights = {
            "rs_position": 0.25,
            "rs_momentum": 0.25,
            "trend": 0.20,
            "crowding": 0.15,
            "fund_flow": 0.15,
        }
        total_score = (
            rs_pos * weights["rs_position"]
            + rs_mom * weights["rs_momentum"]
            + trend_s * weights["trend"]
            + crowd_s * weights["crowding"]
            + flow_s * weights["fund_flow"]
        )

        # 获取九宫格状态
        state = None
        if self.state_machine is not None and trend is not None and rs_momentum_pct is not None:
            state = self.state_machine.determine_state(trend, rs_momentum_pct)

        result = {
            "sector_code": sector_code,
            "score": round(total_score, 1),
            "rs_position_score": round(rs_pos, 1),
            "rs_momentum_score": round(rs_mom, 1),
            "trend_score": round(trend_s, 1),
            "crowding_score": round(crowd_s, 1),
            "fund_flow_score": round(flow_s, 1),
            "state": state,
        }

        logger.info(
            f"板块 {sector_code} 综合评分={total_score:.1f} "
            f"(RS位置={rs_pos:.1f}, RS动量={rs_mom:.1f}, "
            f"趋势={trend_s:.1f}, 拥挤={crowd_s:.1f}, 资金={flow_s:.1f})"
        )
        return result

    # ============================================================
    # 批量评分
    # ============================================================
    def _get_score_snapshot_path(self) -> str:
        """获取评分快照文件路径"""
        cache_dir = os.path.join(str(PARQUET_DIR), "cache")
        return os.path.join(cache_dir, "score_snapshot.parquet")

    def calc_all_scores(self, date: str = None) -> Optional[pd.DataFrame]:
        """
        计算所有板块评分并排序（带快照缓存）

        参数:
            date: 目标日期

        返回:
            DataFrame: sector_code, sector_name, score, state, rank
        """
        snapshot_path = self._get_score_snapshot_path()

        # 当日数据不变时直接用缓存
        if date is None and os.path.exists(snapshot_path):
            import datetime
            snapshot_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(snapshot_path))
            today = datetime.datetime.now().date()
            # 快照是今天生成的 → 直接返回
            if snapshot_mtime.date() == today:
                logger.info("从评分快照缓存加载")
                return pd.read_parquet(snapshot_path)

        logger.info(f"计算所有板块评分, date={date or '最新'}")

        # 获取所有板块代码
        rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "rs")
        if not os.path.exists(rs_dir):
            logger.error(f"RS指标目录不存在: {rs_dir}")
            return None

        sector_codes = []
        for f in os.listdir(rs_dir):
            if f.endswith(".parquet"):
                code = f.replace(".parquet", "").replace("_", ".", 1)
                if not code.endswith(".SI"):
                    code = code.replace("_SI", ".SI")
                sector_codes.append(code)

        if not sector_codes:
            logger.error("未找到任何板块数据")
            return None

        results = []
        for code in sector_codes:
            try:
                score_info = self.calc_score(code, date=date)
                if score_info is None:
                    continue

                # 获取板块名称
                sector_name = code
                if self.sqlite_store is not None:
                    try:
                        sector_info = self.sqlite_store.get_sector_by_code(code)
                        if sector_info:
                            sector_name = sector_info.get("name", code)
                    except Exception:
                        pass

                results.append({
                    "sector_code": code,
                    "sector_name": sector_name,
                    "score": score_info["score"],
                    "state": score_info.get("state"),
                    "rs_position_score": score_info["rs_position_score"],
                    "rs_momentum_score": score_info["rs_momentum_score"],
                    "trend_score": score_info["trend_score"],
                    "crowding_score": score_info["crowding_score"],
                    "fund_flow_score": score_info["fund_flow_score"],
                })
            except Exception as e:
                logger.error(f"计算板块 {code} 评分异常: {e}")

        if not results:
            return None

        df = pd.DataFrame(results)
        df = df.sort_values("score", ascending=False).reset_index(drop=True)
        df["rank"] = df.index + 1

        # 保存快照（仅完整无date参数时）
        if date is None:
            os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
            df.to_parquet(snapshot_path, index=False)
            logger.info(f"评分快照已保存: {snapshot_path}")

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

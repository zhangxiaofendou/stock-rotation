"""
九宫格状态机
============
双维度：绝对价格趋势 × 相对强弱趋势
九状态：①领涨减速 ②稳健上行 ③加速冲顶 ④强转弱 ⑤中性震荡 ⑥弱转强 ⑦持续杀跌 ⑧下跌中继 ⑨底背离

状态判断规则：
  - 绝对价格趋势：上涨 / 横盘 / 下跌（由P1层价格趋势模块计算）
  - RS动量分位数：
      > 70 = 增强
      < 30 = 减弱
      30-70 = 走平
  - 九宫格映射：
      增强 + 上涨 → ③加速冲顶
      走平 + 上涨 → ②稳健上行
      减弱 + 上涨 → ①领涨减速
      增强 + 横盘 → ⑥弱转强
      走平 + 横盘 → ⑤中性震荡
      减弱 + 横盘 → ④强转弱
      增强 + 下跌 → ⑨底背离
      走平 + 下跌 → ⑧下跌中继
      减弱 + 下跌 → ⑦持续杀跌
"""

from typing import Optional, Dict, List
import os
import pandas as pd
import numpy as np

from config.logger import get_logger
from config.settings import PARQUET_DIR

logger = get_logger(__name__)


class StateMachine:
    """九宫格状态机"""

    # ============================================================
    # 状态常量
    # ============================================================
    STATE_1 = "①领涨减速"  # 减弱 + 上涨
    STATE_2 = "②稳健上行"  # 走平 + 上涨
    STATE_3 = "③加速冲顶"  # 增强 + 上涨
    STATE_4 = "④强转弱"    # 减弱 + 横盘
    STATE_5 = "⑤中性震荡"  # 走平 + 横盘
    STATE_6 = "⑥弱转强"    # 增强 + 横盘
    STATE_7 = "⑦持续杀跌"  # 减弱 + 下跌
    STATE_8 = "⑧下跌中继"  # 走平 + 下跌
    STATE_9 = "⑨底背离"    # 增强 + 下跌

    # RS动量分位数阈值
    RS_MOMENTUM_HIGH = 70    # > 70 = 增强
    RS_MOMENTUM_LOW = 30     # < 30 = 减弱

    # 横截面领跑阈值：RS动量全市场排名需进入前 RS_CROSS_LEADER_PCT% 才视为「领跑」
    # 用于拦截「只和自己比」导致的加速冲顶假阳性（如跌250天反弹）
    RS_CROSS_LEADER_PCT = 80

    # 想法2 绝对值斜率门槛（双保险）：③加速冲顶 / ⑥弱转强 要求 RS动量显著为正
    #   rs_momentum > 0 且 rs_momentum > K × rolling_std(rs_momentum)
    # 自适应阈值（K×自身波动），避免银行/半导体斜率量级差异导致固定阈值失灵；
    # 用于挡住「分位高但斜率≈0」的弱反弹（反弹时 rs_momentum 通常只是小幅转正）。
    RS_MOMENTUM_ABS_SIGMA_K = 1.2
    RS_MOMENTUM_ABS_SIGMA_WINDOW = 120  # 滚动窗口（交易日），min_periods=30

    # 状态映射表：{ (RS动量方向, 价格趋势): 状态 }
    STATE_MAP = {
        ("减弱", "上涨"): STATE_1,
        ("走平", "上涨"): STATE_2,
        ("增强", "上涨"): STATE_3,
        ("减弱", "横盘"): STATE_4,
        ("走平", "横盘"): STATE_5,
        ("增强", "横盘"): STATE_6,
        ("减弱", "下跌"): STATE_7,
        ("走平", "下跌"): STATE_8,
        ("增强", "下跌"): STATE_9,
    }

    # ============================================================
    # 交易信号：每个九宫格状态对应的买卖建议
    #   买入：相对强度转强 / 底部背离，可逢低布局（⑥弱转强、⑨底背离）
    #   卖出：动能见顶或转弱，宜减仓止盈（①领涨减速、③加速冲顶、④强转弱）
    #   持有：趋势完好，继续持有（②稳健上行）
    #   观望：方向不明或空头，暂观望/空仓（⑤中性震荡、⑦持续杀跌、⑧下跌中继）
    # ============================================================
    SIGNAL_BUY = "买入"
    SIGNAL_SELL = "卖出"
    SIGNAL_HOLD = "持有"
    SIGNAL_WATCH = "观望"

    STATE_SIGNAL_MAP = {
        STATE_1: SIGNAL_SELL,   # ①领涨减速 → 卖出
        STATE_2: SIGNAL_HOLD,   # ②稳健上行 → 持有
        STATE_3: SIGNAL_SELL,   # ③加速冲顶 → 卖出
        STATE_4: SIGNAL_SELL,   # ④强转弱 → 卖出
        STATE_5: SIGNAL_WATCH,  # ⑤中性震荡 → 观望
        STATE_6: SIGNAL_BUY,    # ⑥弱转强 → 买入
        STATE_7: SIGNAL_WATCH,  # ⑦持续杀跌 → 观望
        STATE_8: SIGNAL_WATCH,  # ⑧下跌中继 → 观望
        STATE_9: SIGNAL_BUY,    # ⑨底背离 → 买入
    }

    # 信号配色（红=买入/做多，绿=卖出，橙=持有，灰=观望）
    SIGNAL_COLORS = {
        SIGNAL_BUY: "#e23c3c",
        SIGNAL_SELL: "#16a34a",
        SIGNAL_HOLD: "#f59e0b",
        SIGNAL_WATCH: "#9e9e9e",
    }

    def __init__(self, parquet_store=None, sqlite_store=None):
        """
        初始化状态机

        参数:
            parquet_store: ParquetStore 实例（用于读取指标数据）
            sqlite_store: SQLiteStore 实例（用于读取板块信息）
        """
        self.parquet_store = parquet_store
        self.sqlite_store = sqlite_store

    # ============================================================
    # RS动量方向判断
    # ============================================================
    def _rs_direction(self, rs_momentum_percentile: float) -> str:
        """
        根据RS动量分位数判断方向

        参数:
            rs_momentum_percentile: RS动量分位数 (0-100)

        返回:
            "增强" / "走平" / "减弱"
        """
        if rs_momentum_percentile is None or np.isnan(rs_momentum_percentile):
            return "走平"
        if rs_momentum_percentile > self.RS_MOMENTUM_HIGH:
            return "增强"
        elif rs_momentum_percentile < self.RS_MOMENTUM_LOW:
            return "减弱"
        else:
            return "走平"

    # ============================================================
    # 交易信号
    # ============================================================
    def get_signal(self, state: str) -> str:
        """
        返回某九宫格状态对应的交易信号

        返回: "买入" / "卖出" / "持有" / "观望"
        """
        return self.STATE_SIGNAL_MAP.get(state, self.SIGNAL_WATCH)

    def get_signal_color(self, state: str) -> str:
        """返回某九宫格状态交易信号的配色"""
        return self.SIGNAL_COLORS.get(self.get_signal(state), "#9e9e9e")

    def get_signal_legend(self) -> List[Dict[str, str]]:
        """
        返回信号图例（按 买入 → 卖出 → 持有 → 观望 顺序）

        每项: {"signal", "color", "desc", "states"}
        """
        order = [self.SIGNAL_BUY, self.SIGNAL_SELL, self.SIGNAL_HOLD, self.SIGNAL_WATCH]
        desc = {
            self.SIGNAL_BUY: "相对强度转强 / 底部背离，可逢低布局",
            self.SIGNAL_SELL: "动能见顶或转弱，宜减仓止盈",
            self.SIGNAL_HOLD: "趋势完好，继续持有",
            self.SIGNAL_WATCH: "方向不明或空头，暂观望/空仓",
        }
        out = []
        for sig in order:
            states = [s for s, v in self.STATE_SIGNAL_MAP.items() if v == sig]
            out.append({
                "signal": sig,
                "color": self.SIGNAL_COLORS.get(sig, "#9e9e9e"),
                "desc": desc.get(sig, ""),
                "states": states,
            })
        return out

    # ============================================================
    # 状态判断
    # ============================================================
    def determine_state(
        self,
        trend: str,
        rs_momentum_percentile: float,
        rs_momentum_cross_pct: float = None,
        rs_momentum: float = None,
        rs_momentum_abs_sigma: float = None,
    ) -> str:
        """
        根据价格趋势和RS动量分位数判断当前状态

        参数:
            trend: 价格趋势 "上涨"/"横盘"/"下跌"
            rs_momentum_percentile: RS动量分位数 (0-100)
            rs_momentum_cross_pct: RS动量全市场横截面百分位 (0-100)，可选。
                提供时，③加速冲顶 需同时是全市场领跑者（>RS_CROSS_LEADER_PCT），
                否则降级为 ②稳健上行。为 None 时退化为原逻辑（向后兼容）。
            rs_momentum: RS动量绝对值（RS的线性回归斜率），可选。
            rs_momentum_abs_sigma: rs_momentum 的滚动标准差，可选。
                二者均提供且有效时，启用【想法2绝对值门槛】双保险：
                ③加速冲顶 / ⑥弱转强 要求 rs_momentum 显著为正
                （>0 且 > RS_MOMENTUM_ABS_SIGMA_K × 滚动std），否则降级为 ②/⑤。
                为 None 时退化为原逻辑（向后兼容）。

        返回:
            状态字符串，如 "⑥弱转强"，数据不足返回 "⑤中性震荡"
        """
        # 标准化趋势值
        if trend not in ("上涨", "横盘", "下跌"):
            trend = "横盘"

        rs_dir = self._rs_direction(rs_momentum_percentile)
        state = self.STATE_MAP.get((rs_dir, trend), self.STATE_5)

        # 横截面闸门：③加速冲顶 必须同时领跑全市场，否则只是「相对自己加速」而非「领先市场」
        if state == self.STATE_3 and rs_momentum_cross_pct is not None:
            if rs_momentum_cross_pct <= self.RS_CROSS_LEADER_PCT or np.isnan(
                rs_momentum_cross_pct
            ):
                state = self.STATE_2

        # 想法2 绝对值斜率门槛（双保险）：③加速冲顶 / ⑥弱转强 要求 RS动量显著为正，
        # 挡住「分位高但斜率≈0」的弱反弹（反弹时 rs_momentum 通常只是小幅转正）。
        if state in (self.STATE_3, self.STATE_6) and rs_momentum is not None \
                and rs_momentum_abs_sigma is not None:
            sig_ok = (
                not np.isnan(rs_momentum)
                and not np.isnan(rs_momentum_abs_sigma)
                and rs_momentum > 0
                and rs_momentum_abs_sigma > 0
                and rs_momentum >= self.RS_MOMENTUM_ABS_SIGMA_K * rs_momentum_abs_sigma
            )
            if not sig_ok:
                state = self.STATE_2 if trend == "上涨" else self.STATE_5

        logger.debug(
            f"状态判断: trend={trend}, rs_momentum_pct={rs_momentum_percentile:.1f}, "
            f"rs_cross={rs_momentum_cross_pct}, rs_dir={rs_dir}, state={state}"
        )
        return state

    def _determine_states_vectorized(self, df: pd.DataFrame) -> pd.Series:
        """
        向量化批量状态判断（比逐行 iterrows 快 50-100x）

        参数:
            df: 含 trend / rs_momentum_percentile 列，
                可选含 rs_momentum_cross_pct（横截面领跑闸门）、
                rs_momentum / rs_momentum_abs_sigma（想法2绝对值门槛）

        返回:
            pd.Series: 状态字符串
        """
        pct = df["rs_momentum_percentile"].values
        trend = df["trend"].values
        cross = (
            df["rs_momentum_cross_pct"].values
            if "rs_momentum_cross_pct" in df.columns
            else None
        )
        rsm = (
            df["rs_momentum"].values
            if "rs_momentum" in df.columns
            else None
        )
        sigma = (
            df["rs_momentum_abs_sigma"].values
            if "rs_momentum_abs_sigma" in df.columns
            else None
        )

        # RS 方向
        rs_dir = np.where(pct > self.RS_MOMENTUM_HIGH, "增强",
                          np.where(pct < self.RS_MOMENTUM_LOW, "减弱", "走平"))

        # 趋势归一化
        valid_trends = {"上涨", "横盘", "下跌"}
        trend_norm = np.where(np.isin(trend, list(valid_trends)), trend, "横盘")

        # 九宫格映射（默认=⑤中性震荡）
        state = np.full(len(df), self.STATE_5, dtype=object)

        # 批量赋值
        mask = (rs_dir == "增强") & (trend_norm == "上涨");  state[mask] = self.STATE_3
        mask = (rs_dir == "增强") & (trend_norm == "横盘");  state[mask] = self.STATE_6
        mask = (rs_dir == "增强") & (trend_norm == "下跌");  state[mask] = self.STATE_9
        mask = (rs_dir == "走平") & (trend_norm == "上涨");  state[mask] = self.STATE_2
        mask = (rs_dir == "走平") & (trend_norm == "下跌");  state[mask] = self.STATE_8
        mask = (rs_dir == "减弱") & (trend_norm == "上涨");  state[mask] = self.STATE_1
        mask = (rs_dir == "减弱") & (trend_norm == "横盘");  state[mask] = self.STATE_4
        mask = (rs_dir == "减弱") & (trend_norm == "下跌");  state[mask] = self.STATE_7

        # 横截面闸门：③加速冲顶 必须同时是全市场 RS 动量领跑者，
        # 否则只是「相对自己加速」而非「领先市场」，降级为 ②稳健上行。
        if cross is not None:
            not_leader = (rs_dir == "增强") & (trend_norm == "上涨") & (
                np.isnan(cross) | (cross <= self.RS_CROSS_LEADER_PCT)
            )
            state[not_leader] = self.STATE_2

        # 想法2 绝对值斜率门槛（双保险）：③加速冲顶 / ⑥弱转强 要求 RS动量显著为正。
        # 弱反弹时 rs_momentum 通常只是小幅转正（< K×自身波动），直接降级为 ②/⑤。
        if rsm is not None and sigma is not None:
            sig_ok = (
                ~np.isnan(rsm) & ~np.isnan(sigma)
                & (rsm > 0) & (sigma > 0)
                & (rsm >= self.RS_MOMENTUM_ABS_SIGMA_K * sigma)
            )
            weak_enh = (
                (rs_dir == "增强") & ~sig_ok
                & ((trend_norm == "上涨") | (trend_norm == "横盘"))
            )
            down = np.where(trend_norm == "上涨", self.STATE_2, self.STATE_5)
            state[weak_enh] = down[weak_enh]

        return pd.Series(state, index=df.index)

    # ============================================================
    # 读取指标数据
    # ============================================================
    def _load_rs_data(self, sector_code: str) -> Optional[pd.DataFrame]:
        """
        加载板块RS指标数据

        参数:
            sector_code: 板块代码，如 "801012.SI"

        返回:
            DataFrame，含 date/rs_momentum_percentile 等列，或 None
        """
        if self.parquet_store is None:
            logger.warning("未配置ParquetStore，无法读取RS数据")
            return None

        # RS指标存储在 data/storage/parquet/indicators/rs/{code}.parquet
        # 例如: 881121 -> 881121.parquet
        import os
        rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "rs")
        safe_code = sector_code.replace(".", "_")
        rs_path = os.path.join(rs_dir, f"{safe_code}.parquet")

        if not os.path.exists(rs_path):
            logger.debug(f"板块 {sector_code} RS指标数据不存在: {rs_path}")
            return None

        try:
            df = pd.read_parquet(rs_path)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            logger.debug(f"加载板块 {sector_code} RS指标数据, 共 {len(df)} 条")
            return df
        except Exception as e:
            logger.error(f"加载板块 {sector_code} RS指标数据失败: {e}")
            return None

    def _load_trend_data(self, sector_code: str) -> Optional[pd.DataFrame]:
        """
        加载板块价格趋势数据

        参数:
            sector_code: 板块代码

        返回:
            DataFrame，含 date/trend 等列，或 None
        """
        if self.parquet_store is None:
            logger.warning("未配置ParquetStore，无法读取趋势数据")
            return None

        # 例如: 881121 -> 881121.parquet
        import os
        trend_dir = os.path.join(str(PARQUET_DIR), "indicators", "trend")
        safe_code = sector_code.replace(".", "_")
        trend_path = os.path.join(trend_dir, f"{safe_code}.parquet")

        if not os.path.exists(trend_path):
            logger.debug(f"板块 {sector_code} 趋势数据不存在: {trend_path}")
            return None

        try:
            df = pd.read_parquet(trend_path)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            logger.debug(f"加载板块 {sector_code} 趋势数据, 共 {len(df)} 条")
            return df
        except Exception as e:
            logger.error(f"加载板块 {sector_code} 趋势数据失败: {e}")
            return None

    # ============================================================
    # 历史状态序列计算
    # ============================================================
    def calc_state_series(self, sector_code: str) -> Optional[pd.DataFrame]:
        """
        计算某板块的历史状态序列
        合并RS指标和价格趋势数据，逐日判断状态

        参数:
            sector_code: 板块代码

        返回:
            DataFrame: date, trend, rs_momentum_percentile, state
            数据不存在返回 None
        """
        logger.info(f"计算板块历史状态序列: {sector_code}")

        # 加载RS和趋势数据
        rs_df = self._load_rs_data(sector_code)
        trend_df = self._load_trend_data(sector_code)

        if rs_df is None:
            logger.warning(f"板块 {sector_code} RS指标数据不存在")
            return None
        if trend_df is None:
            logger.warning(f"板块 {sector_code} 趋势数据不存在")
            return None

        # 合并数据，取日期交集（同时获取 rs_percentile 和 rs_momentum_percentile）
        rs_cols = ["date", "rs_momentum_percentile"]
        if "rs_percentile" in rs_df.columns:
            rs_cols.append("rs_percentile")
        if "rs_momentum_cross_pct" in rs_df.columns:
            rs_cols.append("rs_momentum_cross_pct")
        if "rs_momentum" in rs_df.columns:
            rs_cols.append("rs_momentum")
        merged = pd.merge(
            rs_df[rs_cols],
            trend_df[["date", "trend"]],
            on="date",
            how="inner",
        )
        merged = merged.sort_values("date").reset_index(drop=True)

        if merged.empty:
            logger.warning(f"板块 {sector_code} RS与趋势数据无共同日期")
            return None

        # 过滤掉趋势为"数据不足"的行
        merged = merged[merged["trend"] != "数据不足"].copy()

        if merged.empty:
            logger.warning(f"板块 {sector_code} 有效趋势数据为空")
            return None

        # 计算 RS动量 滚动标准差（想法2绝对值门槛用，自适应阈值）
        if "rs_momentum" in merged.columns:
            merged["rs_momentum_abs_sigma"] = (
                merged["rs_momentum"]
                .rolling(window=self.RS_MOMENTUM_ABS_SIGMA_WINDOW, min_periods=30)
                .std()
            )

        # 向量化状态判断（替代慢速的 iterrows 循环）
        merged["state"] = self._determine_states_vectorized(merged)

        # 过滤掉RS动量分位数为NaN的行
        merged = merged.dropna(subset=["rs_momentum_percentile"])

        logger.info(f"板块 {sector_code} 状态序列计算完成, 共 {len(merged)} 条")
        out_cols = ["date", "trend", "rs_momentum_percentile", "state"]
        if "rs_percentile" in merged.columns:
            out_cols.insert(2, "rs_percentile")
        if "rs_momentum" in merged.columns:
            out_cols.insert(2, "rs_momentum")
        if "rs_momentum_cross_pct" in merged.columns:
            out_cols.append("rs_momentum_cross_pct")
        return merged[out_cols]

    # ============================================================
    # 批量计算所有板块状态
    # ============================================================
    def calc_all_sectors_state(self, date: str = None) -> Optional[pd.DataFrame]:
        """
        计算所有板块在指定日期的状态
        如果date=None，用最新日期

        优化：先从快照缓存加载（单文件读取），仅在缓存失效时全量计算

        参数:
            date: 目标日期，格式 "YYYY-MM-DD"，None表示最新

        返回:
            DataFrame: sector_code, sector_name, state, trend, rs_percentile, rs_momentum_percentile
        """
        logger.info(f"计算所有板块状态, date={date or '最新'}")

        # 快照缓存路径
        cache_dir = os.path.join(str(PARQUET_DIR), "cache")
        os.makedirs(cache_dir, exist_ok=True)
        snapshot_path = os.path.join(cache_dir, "state_snapshot.parquet")

        # 尝试从快照加载（仅当 date=None 即查最新日期时有效）
        # 快照由 daily_update.py 在数据更新后自动清除，所以只要存在就是最新的
        if date is None and os.path.exists(snapshot_path):
            try:
                cached = pd.read_parquet(snapshot_path)
                if not cached.empty and "date" in cached.columns:
                    logger.info(f"从快照缓存加载板块状态, {len(cached)} 个板块, "
                                f"数据日期: {str(cached['date'].max())[:10]}")
                    return cached
            except Exception as e:
                logger.warning(f"快照缓存读取失败: {e}")

        # ——— 全量计算 ———
        # 获取所有可用的板块代码（从RS指标目录）
        rs_dir = os.path.join(str(PARQUET_DIR), "indicators", "rs")
        if not os.path.exists(rs_dir):
            logger.error(f"RS指标目录不存在: {rs_dir}")
            return None

        sector_codes = []
        for f in os.listdir(rs_dir):
            if f.endswith(".parquet"):
                # 文件名保存时把 code 中的 '.' 替换为 '_'，因此还原即可。
                # 同花顺 881121   -> 881121.parquet   -> 881121
                code = f.replace(".parquet", "").replace("_", ".")
                sector_codes.append(code)

        if not sector_codes:
            logger.error("未找到任何板块RS指标数据")
            return None

        results = []
        max_data_date = None  # 追踪实际数据的最新日期
        for code in sector_codes:
            try:
                state_series = self.calc_state_series(code)
                if state_series is None or state_series.empty:
                    continue

                # 取目标日期的数据
                if date is not None:
                    target_date = pd.to_datetime(date)
                    row = state_series[state_series["date"] == target_date]
                    if row.empty:
                        continue
                    row = row.iloc[-1]
                else:
                    # 取最新日期
                    row = state_series.iloc[-1]
                    # 追踪实际数据日期
                    actual_date = row.get("date")
                    if actual_date is not None:
                        actual_date_str = str(pd.Timestamp(actual_date).date())
                        if max_data_date is None or actual_date_str > max_data_date:
                            max_data_date = actual_date_str

                # 获取板块名称
                sector_name = code
                if self.sqlite_store is not None:
                    try:
                        sector_info = self.sqlite_store.get_sector_by_code(code)
                        if sector_info:
                            sector_name = sector_info.get("name", code)
                    except Exception:
                        pass

                # 获取RS分位值（从 state_series 中直接取，避免二次磁盘读取）
                rs_percentile = row.get("rs_percentile") if "rs_percentile" in row.index else None
                rs_cross = row.get("rs_momentum_cross_pct") if "rs_momentum_cross_pct" in row.index else None

                results.append({
                    "sector_code": code,
                    "sector_name": sector_name,
                    "state": row["state"],
                    "trend": row["trend"],
                    "rs_percentile": rs_percentile if rs_percentile is not None and not np.isnan(rs_percentile) else None,
                    "rs_momentum_percentile": row["rs_momentum_percentile"],
                    "rs_momentum_cross_pct": rs_cross if rs_cross is not None and not (isinstance(rs_cross, float) and np.isnan(rs_cross)) else None,
                })

            except Exception as e:
                logger.error(f"计算板块 {code} 状态异常: {e}")

        if not results:
            logger.warning("全量计算未产出任何板块状态，尝试回退读取快照缓存")
            try:
                if os.path.exists(snapshot_path):
                    cached = pd.read_parquet(snapshot_path)
                    if not cached.empty:
                        logger.info(f"已从快照缓存回退, {len(cached)} 个板块")
                        return cached
            except Exception as e:
                logger.warning(f"回退读取快照失败: {e}")
            return None

        result_df = pd.DataFrame(results)

        # 添加日期列用于快照判断
        # 使用实际数据日期（而非计算时的系统日期）
        result_df["date"] = date or (max_data_date or pd.Timestamp.now().strftime("%Y-%m-%d"))

        # 保存快照缓存
        try:
            result_df.to_parquet(snapshot_path, index=False)
            logger.info(f"状态快照已保存: {snapshot_path}")
        except Exception as e:
            logger.warning(f"保存状态快照失败: {e}")

        logger.info(f"所有板块状态计算完成, 共 {len(result_df)} 个板块")
        return result_df

    # ============================================================
    # 状态分布统计
    # ============================================================
    def get_state_distribution(self, date: str = None) -> Dict[str, List[str]]:
        """
        获取某日所有板块的状态分布

        参数:
            date: 目标日期，None表示最新

        返回:
            dict: {状态: [板块代码列表]}
        """
        logger.info(f"获取状态分布, date={date or '最新'}")

        state_df = self.calc_all_sectors_state(date=date)
        if state_df is None or state_df.empty:
            logger.warning("无法获取板块状态")
            return {}

        distribution = {}
        for _, row in state_df.iterrows():
            state = row["state"]
            code = row["sector_code"]
            if state not in distribution:
                distribution[state] = []
            distribution[state].append(code)

        # 按状态编号排序
        sorted_dist = {}
        all_states = [
            self.STATE_1, self.STATE_2, self.STATE_3,
            self.STATE_4, self.STATE_5, self.STATE_6,
            self.STATE_7, self.STATE_8, self.STATE_9,
        ]
        for state in all_states:
            if state in distribution:
                sorted_dist[state] = distribution[state]

        # 日志输出分布情况
        for state, codes in sorted_dist.items():
            logger.info(f"  {state}: {len(codes)}个板块 - {codes[:5]}{'...' if len(codes) > 5 else ''}")

        return sorted_dist

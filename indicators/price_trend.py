"""
绝对价格趋势判断模块
====================
基于均线系统判断板块的绝对价格趋势方向（上涨/横盘/下跌），
并对横盘状态给出"穿越角标"（下穿/上穿 20 日均线的持续天数）。

状态判定（纯均线排列，逐日函数式判定，无持续度/斜率门槛）：
  - 上涨：MA5 > MA20 > MA60（已上穿 60 日线，完整多头排列）
  - 下跌：MA5 < MA20 < MA60（已击穿 60 日线，完整空头排列）
  - 横盘：其余情况，细分带角标：
      * 死叉横盘（MA5<MA20 但未击穿 60）：角标 = -连续死叉天数（绿，空方信号）
      * 金叉横盘（MA5>MA20 但未上穿 60）：角标 = +连续金叉天数（红，多方信号）
      * 中性横盘（MA5≈MA20 粘合）：无角标

角标（trend_badge，int）：
  - 负：下穿 20 日均线（死叉）持续天数，绿底显示
  - 正：上穿 20 日均线（金叉）持续天数，红底显示
  - 0 ：无角标（上涨/下跌/中性横盘）

分界线语义（用户重定义）：
  - 死叉(MA5<MA20) 即退出上涨 -> 横盘（负角标）
  - 击穿60(MA5<MA20<MA60) -> 下跌（角标取消）
  - 金叉(MA5>MA20) 即退出下跌 -> 横盘（正角标）
  - 上穿60(MA5>MA20>MA60) -> 上涨（角标取消）
（早期"斜率 0.2% 门槛 + 近 5 日持续度过滤"已移除，改为纯均线排列即时判定。）
"""

from typing import Optional, List
import numpy as np
import pandas as pd

from config.logger import get_logger

logger = get_logger(__name__)


class PriceTrend:
    """绝对价格趋势判断（均线法）"""

    def __init__(self, parquet_store, sqlite_store):
        """
        初始化价格趋势计算器

        参数:
            parquet_store: ParquetStore 实例
            sqlite_store: SQLiteStore 实例
        """
        self.parquet_store = parquet_store
        self.sqlite_store = sqlite_store

    # ============================================================
    # 趋势分类（纯均线排列）
    # ============================================================
    @staticmethod
    def _classify(ma5, ma20, ma60, consec_death, consec_gold):
        """
        根据当日均线排列 + 连续穿越天数，返回 (趋势, 角标)。
        角标：负=死叉破位天数(绿)，正=金叉破位天数(红)，0=无角标。
        """
        if ma5 > ma20 > ma60:
            return "上涨", 0
        if ma5 < ma20 < ma60:
            return "下跌", 0
        if ma5 < ma20:        # 死叉但未击穿 60（隐含 ma5 > ma60）
            return "横盘", -consec_death
        if ma5 > ma20:        # 金叉但未上穿 60（隐含 ma5 < ma60）
            return "横盘", consec_gold
        return "横盘", 0       # 粘合

    # ============================================================
    # 内部辅助方法
    # ============================================================
    def _load_and_standardize(self, sector_code: str) -> Optional[pd.DataFrame]:
        """
        加载板块指数数据并标准化列名

        参数:
            sector_code: 板块代码

        返回:
            DataFrame，列名为 date/close，或 None
        """
        df = self.parquet_store.load_index_hist(sector_code)
        if df is None or df.empty:
            logger.warning(f"板块 {sector_code} 数据不存在或为空")
            return None

        # 标准化列名
        col_map = {
            "日期": "date",
            "收盘": "close",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df

    # ============================================================
    # 均线计算
    # ============================================================
    def calc_ma(self, prices: pd.Series, periods: List[int] = None) -> pd.DataFrame:
        """
        计算多条移动平均线

        参数:
            prices: 价格序列
            periods: 均线周期列表，默认 [5, 20, 60]

        返回:
            DataFrame，每列为一个周期的均线
        """
        if periods is None:
            periods = [5, 20, 60]

        result = pd.DataFrame()
        for p in periods:
            col_name = f"ma{p}"
            result[col_name] = prices.rolling(window=p, min_periods=1).mean()

        return result

    def calc_ma_slope(self, ma_series: pd.Series, window: int = 5) -> pd.Series:
        """
        计算均线的滚动斜率（%/日）

        参数:
            ma_series: 均线序列
            window: 斜率计算窗口

        返回:
            斜率序列（%/日）
        """
        # 斜率 = (当前值 - window日前值) / window / 当前值 * 100
        slope = (ma_series - ma_series.shift(window)) / window / ma_series * 100
        return slope

    # ============================================================
    # 趋势判断
    # ============================================================
    def judge_trend(self, sector_code: str) -> Optional[str]:
        """
        判断板块当前绝对价格趋势（返回 "上涨"/"横盘"/"下跌" 字符串）

        返回:
            "上涨" / "横盘" / "下跌"，数据不足返回 None
        """
        df = self._load_and_standardize(sector_code)
        if df is None or len(df) < 60:
            return None

        ts = self.calc_trend_series(sector_code)
        if ts is None or ts.empty:
            return None
        t = ts["trend"].iloc[-1]
        return t if t in ("上涨", "横盘", "下跌") else "横盘"

    def calc_trend_series(self, sector_code: str) -> Optional[pd.DataFrame]:
        """
        计算板块历史趋势序列（每天的趋势状态 + 横盘角标）

        返回:
            DataFrame，含 date/close/ma5/ma20/ma60/ma20_slope/trend/trend_badge 列
            trend_badge: 横盘角标（负=死叉破位天数/绿，正=金叉破位天数/红，0=无）
        """
        logger.info(f"计算板块历史趋势序列: {sector_code}")

        df = self._load_and_standardize(sector_code)
        if df is None:
            return None

        close = df["close"]
        dates = df["date"]

        # 计算均线
        mas = self.calc_ma(close, [5, 20, 60])
        ma20_slope = self.calc_ma_slope(mas["ma20"], window=5)

        # 逐日判定（纯均线排列）：
        # 全程累计连续死叉/金叉天数（cd/cg），用于横盘角标；
        # 前 60 天均线未充分稳定，标"数据不足"但不重置计数。
        n = len(df)
        cd = 0   # 连续死叉天数（MA5<MA20）
        cg = 0   # 连续金叉天数（MA5>MA20）
        trends = []
        badges = []
        for i in range(n):
            m5 = mas["ma5"].iloc[i]
            m20 = mas["ma20"].iloc[i]
            m60 = mas["ma60"].iloc[i]
            cd = cd + 1 if m5 < m20 else 0
            cg = cg + 1 if m5 > m20 else 0
            if i < 60:
                trends.append("数据不足")
                badges.append(0)
                continue
            t, b = self._classify(m5, m20, m60, cd, cg)
            trends.append(t)
            badges.append(b)

        result = pd.DataFrame({
            "date": dates,
            "close": close,
            "ma5": mas["ma5"],
            "ma20": mas["ma20"],
            "ma60": mas["ma60"],
            "ma20_slope": ma20_slope,
            "trend": trends,
            "trend_badge": badges,
        })

        logger.info(f"板块 {sector_code} 趋势序列计算完成, 共 {len(result)} 条")
        return result

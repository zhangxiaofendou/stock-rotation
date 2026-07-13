"""
资金流信号计算模块
==================
获取行业资金流数据，计算资金流趋势和方向信号。

核心指标：
  - 行业资金流排名：主力净流入排名
  - 资金流趋势：近N日排名变化方向
  - 资金流信号：正向/中性/反向
  - 北向资金信号：北向资金净流入趋势

数据来源：
  - AkShare 的 stock_sector_fund_flow_rank 接口
  - AkShare 的 stock_hsgt_hist_em 接口
"""

from typing import Optional, Dict, List
import numpy as np
import pandas as pd

from config.logger import get_logger

logger = get_logger(__name__)


class MoneyFlowIndicator:
    """资金流信号计算"""

    def __init__(self, parquet_store, sqlite_store, data_source=None):
        """
        初始化资金流指标计算器

        参数:
            parquet_store: ParquetStore 实例
            sqlite_store: SQLiteStore 实例
            data_source: AkShareSource 实例（用于获取实时资金流数据）
        """
        self.parquet_store = parquet_store
        self.sqlite_store = sqlite_store
        self.data_source = data_source

    # ============================================================
    # 内部辅助方法
    # ============================================================
    def _get_sector_name_from_code(self, sector_code: str) -> Optional[str]:
        """
        将申万板块代码转换为行业名称（用于匹配资金流数据中的名称）

        参数:
            sector_code: 申万板块代码，如 "801080.SI"

        返回:
            行业名称，如 "电子"
        """
        try:
            from config.sector_map import SW_LEVEL1_MAP, SW_LEVEL2_MAP
            if sector_code in SW_LEVEL1_MAP:
                return SW_LEVEL1_MAP[sector_code]
            if sector_code in SW_LEVEL2_MAP:
                return SW_LEVEL2_MAP[sector_code][0]
        except ImportError:
            pass
        return None

    def _load_fund_flow_cache(self, indicator: str = "今日") -> Optional[pd.DataFrame]:
        """
        从缓存加载资金流数据

        参数:
            indicator: 指标类型

        返回:
            DataFrame
        """
        category = f"sector_fund_flow_{indicator}"
        return self.parquet_store.load_fund_flow(category)

    def _save_fund_flow_cache(self, df: pd.DataFrame, indicator: str = "今日"):
        """
        缓存资金流数据

        参数:
            df: 资金流 DataFrame
            indicator: 指标类型
        """
        category = f"sector_fund_flow_{indicator}"
        self.parquet_store.save_fund_flow(df, category)

    # ============================================================
    # 核心指标计算
    # ============================================================
    def calc_sector_fund_flow_rank(self, indicator: str = "今日") -> Optional[pd.DataFrame]:
        """
        获取行业资金流排名

        参数:
            indicator: 指标类型，"今日"/"5日"/"10日"

        返回:
            DataFrame，含行业名称/主力净流入/排名等列
        """
        logger.info(f"获取行业资金流排名, 指标: {indicator}")

        # 先尝试从缓存加载
        cached = self._load_fund_flow_cache(indicator)
        if cached is not None and not cached.empty:
            logger.info(f"从缓存加载资金流排名 {indicator}, 共 {len(cached)} 条")
            return cached

        # 从数据源获取
        if self.data_source is None:
            logger.warning("未配置数据源，无法获取资金流数据")
            return None

        try:
            df = self.data_source.get_sector_fund_flow_rank(indicator=indicator)
            if df is not None and not df.empty:
                # 缓存数据
                self._save_fund_flow_cache(df, indicator)
                logger.info(f"获取资金流排名 {indicator} 成功, 共 {len(df)} 条")
                return df
            else:
                logger.warning(f"资金流排名 {indicator} 数据为空")
                return None
        except Exception as e:
            logger.error(f"获取资金流排名失败 {indicator}: {e}")
            return None

    def calc_fund_flow_trend(self, sector_code: str, days: int = 5) -> Optional[Dict]:
        """
        计算板块资金流趋势

        近N日主力净流入的排名变化方向

        参数:
            sector_code: 板块代码
            days: 回溯天数

        返回:
            dict: {
                "sector_code": 板块代码,
                "current_rank": 当前排名,
                "rank_change": 排名变化（正=变好，负=变差）,
                "trend": "改善"/"恶化"/"稳定"
            }
        """
        logger.info(f"计算资金流趋势: {sector_code}, 天数={days}")

        # 获取行业名称
        sector_name = self._get_sector_name_from_code(sector_code)
        if sector_name is None:
            logger.warning(f"无法获取板块 {sector_code} 的名称")
            return None

        # 获取当前资金流排名
        current_df = self.calc_sector_fund_flow_rank(indicator="今日")
        if current_df is None or current_df.empty:
            return None

        # 尝试匹配行业名称（资金流数据可能使用不同的名称格式）
        # 查找可能的列名
        name_col = None
        for col in ["名称", "行业", "板块", "name", "sector"]:
            if col in current_df.columns:
                name_col = col
                break
        if name_col is None:
            name_col = current_df.columns[0]

        # 查找流入列
        flow_col = None
        for col in ["主力净流入-净额", "主力净流入", "net_inflow", "flow"]:
            if col in current_df.columns:
                flow_col = col
                break
        if flow_col is None and len(current_df.columns) >= 2:
            flow_col = current_df.columns[1]

        # 匹配板块
        matched = current_df[current_df[name_col].str.contains(
            sector_name.replace("Ⅱ", "").replace(" ", ""), na=False
        )]

        if matched.empty:
            # 尝试更宽松的匹配
            logger.debug(f"资金流数据中未精确匹配到 {sector_name}，尝试模糊匹配")
            matched = current_df[current_df[name_col].apply(
                lambda x: sector_name[:2] in str(x) if pd.notna(x) else False
            )]

        if matched.empty:
            logger.warning(f"资金流数据中未找到板块 {sector_code} ({sector_name})")
            return None

        # 计算当前排名（按净流入从大到小排）
        if flow_col:
            current_df = current_df.copy()
            current_df["_rank"] = current_df[flow_col].rank(ascending=False)
            current_rank = int(matched["_rank"].values[0]) if "_rank" in matched.columns else None
        else:
            current_rank = None

        # 获取历史资金流数据以计算排名变化
        rank_change = None
        trend = "稳定"

        if days > 1 and self.data_source is not None:
            try:
                prev_df = self.calc_sector_fund_flow_rank(indicator=f"{days}日")
                if prev_df is not None and not prev_df.empty:
                    if flow_col and flow_col in prev_df.columns:
                        prev_df = prev_df.copy()
                        prev_df["_rank"] = prev_df[flow_col].rank(ascending=False)
                        prev_matched = prev_df[prev_df[name_col].str.contains(
                            sector_name.replace("Ⅱ", "").replace(" ", ""), na=False
                        )]
                        if not prev_matched.empty and current_rank is not None:
                            prev_rank = int(prev_matched["_rank"].values[0])
                            rank_change = prev_rank - current_rank  # 正=改善（排名上升）
            except Exception as e:
                logger.debug(f"获取历史资金流排名失败: {e}")

        # 判断趋势
        if rank_change is not None:
            if rank_change > 2:
                trend = "改善"
            elif rank_change < -2:
                trend = "恶化"

        result = {
            "sector_code": sector_code,
            "sector_name": sector_name,
            "current_rank": current_rank,
            "rank_change": rank_change,
            "trend": trend,
        }

        logger.info(f"板块 {sector_code} 资金流趋势: {trend}, 排名变化={rank_change}")
        return result

    def calc_fund_flow_signal(self, sector_code: str) -> Optional[str]:
        """
        生成资金流信号

        返回:
            "正向" / "中性" / "反向"

        判断逻辑：
            - 当前资金净流入 + 排名改善 → "正向"
            - 当前资金净流出 + 排名恶化 → "反向"
            - 其他 → "中性"
        """
        logger.info(f"生成资金流信号: {sector_code}")

        trend_info = self.calc_fund_flow_trend(sector_code, days=5)
        if trend_info is None:
            return "中性"

        current_rank = trend_info.get("current_rank")
        trend = trend_info.get("trend", "稳定")

        # 获取当前资金流数据判断净流入/流出
        current_df = self.calc_sector_fund_flow_rank(indicator="今日")
        if current_df is not None and not current_df.empty:
            # 查找流入列
            flow_col = None
            for col in ["主力净流入-净额", "主力净流入"]:
                if col in current_df.columns:
                    flow_col = col
                    break

            if flow_col and current_rank is not None:
                # 判断是净流入还是净流出
                total_industries = len(current_df)
                if current_rank <= total_industries * 0.4 and trend == "改善":
                    return "正向"
                elif current_rank > total_industries * 0.6 and trend == "恶化":
                    return "反向"

        return "中性"

    def calc_north_fund_signal(self) -> Optional[Dict]:
        """
        计算北向资金信号

        返回:
            dict: {
                "recent_inflow": 近5日累计净流入,
                "trend": "持续流入"/"持续流出"/"震荡",
                "signal": "积极"/"中性"/"谨慎"
            }
        """
        logger.info("计算北向资金信号")

        if self.data_source is None:
            logger.warning("未配置数据源，无法获取北向资金数据")
            return None

        try:
            # 获取北向资金历史数据
            df = self.data_source.get_north_hist(symbol="北上")
            if df is None or df.empty:
                logger.warning("北向资金数据为空")
                return None

            # 标准化列名
            col_map = {}
            for col in df.columns:
                col_lower = str(col).lower()
                if "日期" in col or "date" in col_lower:
                    col_map[col] = "date"
                elif "净流入" in col or "net" in col_lower:
                    col_map[col] = "net_flow"

            if col_map:
                df = df.rename(columns=col_map)

            if "date" not in df.columns or "net_flow" not in df.columns:
                logger.warning("北向资金数据缺少必要的列")
                return None

            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")

            # 近5日累计净流入
            recent_5d = df.tail(5)
            recent_inflow = recent_5d["net_flow"].sum()

            # 近20日趋势
            recent_20d = df.tail(20)
            if len(recent_20d) >= 10:
                positive_days = (recent_20d["net_flow"] > 0).sum()
                if positive_days >= 14:
                    trend = "持续流入"
                elif positive_days <= 6:
                    trend = "持续流出"
                else:
                    trend = "震荡"
            else:
                trend = "数据不足"

            # 生成信号
            if recent_inflow > 0 and trend == "���续流入":
                signal = "积极"
            elif recent_inflow < 0 and trend == "持续流出":
                signal = "谨慎"
            else:
                signal = "中性"

            result = {
                "recent_inflow": float(recent_inflow),
                "trend": trend,
                "signal": signal,
            }

            logger.info(f"北向资金信号: {signal}, 近5日净流入={recent_inflow:.2f}亿, 趋势={trend}")
            return result

        except Exception as e:
            logger.error(f"计算北向资金信号失败: {e}")
            return None

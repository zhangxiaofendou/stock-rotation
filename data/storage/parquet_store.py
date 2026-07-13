"""
Parquet 存储引擎
================
行情数据的列式存储，按板块代码分区存储，方便增量更新。
"""

import os
from pathlib import Path
from typing import Optional
import pandas as pd

from config.logger import get_logger
from config.settings import PARQUET_DIR

logger = get_logger(__name__)


class ParquetStore:
    """Parquet 行情数据存储引擎"""

    def __init__(self, base_dir: str = None):
        self.base_dir = Path(base_dir or PARQUET_DIR)
        # 子目录
        self.index_hist_dir = self.base_dir / "index_hist"
        self.stock_hist_dir = self.base_dir / "stock_hist"
        self.fund_flow_dir = self.base_dir / "fund_flow"

        # 确保目录存在
        for d in [self.index_hist_dir, self.stock_hist_dir, self.fund_flow_dir]:
            d.mkdir(parents=True, exist_ok=True)

    # ============================================================
    # 板块指数历史
    # ============================================================
    def _get_index_hist_path(self, code: str) -> Path:
        """获取板块指数历史数据文件路径"""
        # 按板块代码分区：如 801010/801010_SI.parquet
        safe_code = code.replace(".", "_")
        return self.index_hist_dir / f"{safe_code}.parquet"

    def save_index_hist(self, code: str, df: pd.DataFrame):
        """
        保存板块指数历史数据

        参数:
            code: 板块代码，如 "801010.SI"
            df: 历史行情 DataFrame
        """
        if df is None or df.empty:
            logger.warning(f"板块 {code} 数据为空，跳过保存")
            return
        path = self._get_index_hist_path(code)
        try:
            # 确保日期列是字符串格式
            df = df.copy()
            if "date" in df.columns:
                df["date"] = df["date"].astype(str)
            df.to_parquet(path, index=False, compression="snappy")
            logger.info(f"保存板块 {code} 历史数据到 {path}, 共 {len(df)} 条")
        except Exception as e:
            logger.error(f"保存板块 {code} 历史数据失败: {e}")

    def load_index_hist(self, code: str, start: str = None, end: str = None) -> Optional[pd.DataFrame]:
        """
        加载板块指数历史数据

        参数:
            code: 板块代码
            start: 起始日期（可选），格式 "YYYY-MM-DD"
            end: 结束日期（可选），格式 "YYYY-MM-DD"

        返回:
            DataFrame，不存在返回 None
        """
        path = self._get_index_hist_path(code)
        if not path.exists():
            logger.debug(f"板块 {code} 历史数据文件不存在: {path}")
            return None
        try:
            df = pd.read_parquet(path)
            if start or end:
                if "date" in df.columns:
                    mask = pd.Series(True, index=df.index)
                    if start:
                        mask &= df["date"] >= start
                    if end:
                        mask &= df["date"] <= end
                    df = df[mask]
            logger.debug(f"加载板块 {code} 历史数据, 共 {len(df)} 条")
            return df
        except Exception as e:
            logger.error(f"加载板块 {code} 历史数据失败: {e}")
            return None

    def index_hist_exists(self, code: str) -> bool:
        """检查板块历史数据是否存在"""
        return self._get_index_hist_path(code).exists()

    def list_index_hist_codes(self) -> list:
        """列出已存储的板块代码"""
        codes = []
        for f in self.index_hist_dir.glob("*.parquet"):
            # 文件名如 "801010_SI.parquet" → "801010.SI"
            code = f.stem.replace("_", ".")
            codes.append(code)
        return codes

    # ============================================================
    # 个股行情
    # ============================================================
    def _get_stock_hist_path(self, code: str) -> Path:
        """获取个股行情数据文件路径"""
        return self.stock_hist_dir / f"{code}.parquet"

    def save_stock_hist(self, code: str, df: pd.DataFrame):
        """
        保存个股行情数据

        参数:
            code: 股票代码
            df: 行情 DataFrame
        """
        if df is None or df.empty:
            logger.warning(f"个股 {code} 数据为空，跳过保存")
            return
        path = self._get_stock_hist_path(code)
        try:
            df = df.copy()
            if "date" in df.columns:
                df["date"] = df["date"].astype(str)
            df.to_parquet(path, index=False, compression="snappy")
            logger.info(f"保存个股 {code} 行情数据, 共 {len(df)} 条")
        except Exception as e:
            logger.error(f"保存个股 {code} 行情数据失败: {e}")

    def load_stock_hist(self, code: str, start: str = None, end: str = None) -> Optional[pd.DataFrame]:
        """
        加载个股行情数据

        参数:
            code: 股票代码
            start: 起始日期
            end: 结束日期

        返回:
            DataFrame
        """
        path = self._get_stock_hist_path(code)
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            if start or end:
                if "date" in df.columns:
                    mask = pd.Series(True, index=df.index)
                    if start:
                        mask &= df["date"] >= start
                    if end:
                        mask &= df["date"] <= end
                    df = df[mask]
            return df
        except Exception as e:
            logger.error(f"加载个股 {code} 行情数据失败: {e}")
            return None

    # ============================================================
    # 资金流
    # ============================================================
    def _get_fund_flow_path(self, category: str) -> Path:
        """获取资金流数据文件路径"""
        return self.fund_flow_dir / f"{category}.parquet"

    def save_fund_flow(self, df: pd.DataFrame, category: str):
        """
        保存资金流数据

        参数:
            df: 资金流 DataFrame
            category: 分���，如 "market", "sector", "concept", "north"
        """
        if df is None or df.empty:
            logger.warning(f"资金流 {category} 数据为空，跳过保存")
            return
        path = self._get_fund_flow_path(category)
        try:
            df = df.copy()
            if "date" in df.columns:
                df["date"] = df["date"].astype(str)
            df.to_parquet(path, index=False, compression="snappy")
            logger.info(f"保存资金流 {category}, 共 {len(df)} 条")
        except Exception as e:
            logger.error(f"保存资金流 {category} 失败: {e}")

    def load_fund_flow(self, category: str, start: str = None, end: str = None) -> Optional[pd.DataFrame]:
        """
        加载资金流数据

        参数:
            category: 分类
            start: 起始日期
            end: 结束日期

        返回:
            DataFrame
        """
        path = self._get_fund_flow_path(category)
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            if start or end:
                if "date" in df.columns:
                    mask = pd.Series(True, index=df.index)
                    if start:
                        mask &= df["date"] >= start
                    if end:
                        mask &= df["date"] <= end
                    df = df[mask]
            return df
        except Exception as e:
            logger.error(f"加载资金流 {category} 失败: {e}")
            return None

    # ============================================================
    # 基准指数
    # ============================================================
    def _get_benchmark_path(self, code: str) -> Path:
        """获取基准指数文件路径"""
        return self.index_hist_dir / f"benchmark_{code}.parquet"

    def save_benchmark_hist(self, code: str, df: pd.DataFrame):
        """保存基准指数历史数据"""
        if df is None or df.empty:
            return
        path = self._get_benchmark_path(code)
        try:
            df = df.copy()
            if "date" in df.columns:
                df["date"] = df["date"].astype(str)
            df.to_parquet(path, index=False, compression="snappy")
            logger.info(f"保存基准指数 {code} 历史数据, 共 {len(df)} 条")
        except Exception as e:
            logger.error(f"保存基准指数 {code} 失败: {e}")

    def load_benchmark_hist(self, code: str) -> Optional[pd.DataFrame]:
        """加载基准指数历史数据"""
        path = self._get_benchmark_path(code)
        if not path.exists():
            return None
        try:
            return pd.read_parquet(path)
        except Exception as e:
            logger.error(f"加载基准指数 {code} 失败: {e}")
            return None

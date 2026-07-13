"""
SQLite 存储引擎
===============
管理元数据存储，包括板块信息、成分股、交易日历、数据新鲜度等。
"""

import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Tuple
import pandas as pd

from config.logger import get_logger
from config.settings import SQLITE_DB_PATH

logger = get_logger(__name__)


# ============================================================
# 建表 SQL
# ============================================================
CREATE_TABLES_SQL = """
-- 板块信息表（申万一级和二级）
CREATE TABLE IF NOT EXISTS sectors (
    code TEXT PRIMARY KEY,              -- 板块代码，如 801010.SI
    name TEXT NOT NULL,                 -- 板块名称
    level INTEGER NOT NULL,             -- 等级：1=一级, 2=二级
    parent_code TEXT,                   -- 上级板块代码（二级板块使用）
    parent_name TEXT,                   -- 上级板块名称
    stock_count INTEGER,                -- 成分股数量
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 成分股表
CREATE TABLE IF NOT EXISTS sector_stocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector_code TEXT NOT NULL,          -- 所属板块代码
    stock_code TEXT NOT NULL,           -- 股票代码
    stock_name TEXT,                    -- 股票名称
    weight REAL,                        -- 权重（如有）
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(sector_code, stock_code),
    FOREIGN KEY (sector_code) REFERENCES sectors(code)
);

-- 基准指数映射表
CREATE TABLE IF NOT EXISTS benchmark_map (
    sector_code TEXT PRIMARY KEY,       -- 板块代码
    benchmark_code TEXT NOT NULL,       -- 基准指数代码
    benchmark_name TEXT,                -- 基准指数名称
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (sector_code) REFERENCES sectors(code)
);

-- 板块分组表
CREATE TABLE IF NOT EXISTS sector_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name TEXT NOT NULL,           -- 分组名称（大金融/新能源/消费/周期/科技/医药）
    sector_code TEXT NOT NULL,          -- 板块代码（二级）
    description TEXT,                   -- 分组描述
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(group_name, sector_code),
    FOREIGN KEY (sector_code) REFERENCES sectors(code)
);

-- 交易日历表
CREATE TABLE IF NOT EXISTS trade_calendar (
    trade_date TEXT PRIMARY KEY,        -- 交易日期，格式 YYYY-MM-DD
    is_trading_day INTEGER DEFAULT 1,   -- 是否交易日：1=是, 0=否
    week_day INTEGER,                   -- 星期几：0=周一, ..., 6=周日
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 数据新鲜度表
CREATE TABLE IF NOT EXISTS data_freshness (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_type TEXT NOT NULL,            -- 数据类型：sector_hist, stock_hist, fund_flow, calendar 等
    data_key TEXT,                      -- 数据标识（如板块代码）
    last_update TEXT NOT NULL,          -- 最后更新时间
    data_start_date TEXT,               -- 数据起始日期
    data_end_date TEXT,                 -- 数据结束日期
    record_count INTEGER,               -- 记录数
    status TEXT DEFAULT 'ok',           -- 状态：ok, stale, error
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(data_type, data_key)
);

-- 股票基本信息表
CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,              -- 股票代码
    name TEXT NOT NULL,                 -- 股票名称
    market TEXT,                        -- 市场：sh=上海, sz=深圳
    industry TEXT,                      -- 所属行业
    list_date TEXT,                     -- 上市日期
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_sector_stocks_sector ON sector_stocks(sector_code);
CREATE INDEX IF NOT EXISTS idx_sector_stocks_stock ON sector_stocks(stock_code);
CREATE INDEX IF NOT EXISTS idx_freshness_type ON data_freshness(data_type);
CREATE INDEX IF NOT EXISTS idx_trade_calendar_date ON trade_calendar(trade_date);
CREATE INDEX IF NOT EXISTS idx_sector_groups_group ON sector_groups(group_name);
"""


class SQLiteStore:
    """SQLite 存储引擎"""

    def __init__(self, db_path: str = None):
        self.db_path = str(db_path or SQLITE_DB_PATH)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """初始化数据库表结构"""
        try:
            conn = self._get_conn()
            conn.executescript(CREATE_TABLES_SQL)
            conn.commit()
            conn.close()
            logger.info(f"SQLite 数据库初始化完成: {self.db_path}")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")
            raise

    # ============================================================
    # 板块信息 CRUD
    # ============================================================
    def insert_sector(self, code: str, name: str, level: int,
                      parent_code: str = None, parent_name: str = None,
                      stock_count: int = None):
        """插入或更新板块信息"""
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO sectors (code, name, level, parent_code, parent_name, stock_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            """, (code, name, level, parent_code, parent_name, stock_count))
            conn.commit()
        except Exception as e:
            logger.error(f"插入板块失败 {code}: {e}")
        finally:
            conn.close()

    def insert_sectors_batch(self, sectors: List[Tuple]):
        """批量插入板块信息"""
        conn = self._get_conn()
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO sectors (code, name, level, parent_code, parent_name, stock_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            """, sectors)
            conn.commit()
            logger.info(f"批量插入 {len(sectors)} 条板块信息")
        except Exception as e:
            logger.error(f"批量插入板块失败: {e}")
        finally:
            conn.close()

    def get_sectors(self, level: int = None) -> pd.DataFrame:
        """查询板块信息"""
        conn = self._get_conn()
        try:
            if level is not None:
                df = pd.read_sql_query(
                    "SELECT * FROM sectors WHERE level = ? ORDER BY code",
                    conn, params=(level,)
                )
            else:
                df = pd.read_sql_query("SELECT * FROM sectors ORDER BY level, code", conn)
            return df
        finally:
            conn.close()

    def get_sector_by_code(self, code: str) -> Optional[Dict]:
        """根据代码查询板块"""
        conn = self._get_conn()
        try:
            cursor = conn.execute("SELECT * FROM sectors WHERE code = ?", (code,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ============================================================
    # 成分股 CRUD
    # ============================================================
    def insert_sector_stocks_batch(self, sector_code: str, stocks: List[Tuple]):
        """批量插入成分股"""
        conn = self._get_conn()
        try:
            # 先删除旧数据
            conn.execute("DELETE FROM sector_stocks WHERE sector_code = ?", (sector_code,))
            # 插入新数据
            for stock in stocks:
                conn.execute("""
                    INSERT OR REPLACE INTO sector_stocks (sector_code, stock_code, stock_name)
                    VALUES (?, ?, ?)
                """, (sector_code, stock[0], stock[1] if len(stock) > 1 else None))
            conn.commit()
            logger.info(f"更新 {sector_code} 成分股 {len(stocks)} 只")
        except Exception as e:
            logger.error(f"批量插入成分股失败 {sector_code}: {e}")
        finally:
            conn.close()

    def get_sector_stocks(self, sector_code: str) -> pd.DataFrame:
        """查询板块成分股"""
        conn = self._get_conn()
        try:
            df = pd.read_sql_query(
                "SELECT * FROM sector_stocks WHERE sector_code = ?",
                conn, params=(sector_code,)
            )
            return df
        finally:
            conn.close()

    # ============================================================
    # 基准映射 CRUD
    # ============================================================
    def insert_benchmark_map_batch(self, mappings: List[Tuple]):
        """批量插入基准映射"""
        conn = self._get_conn()
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO benchmark_map (sector_code, benchmark_code, benchmark_name)
                VALUES (?, ?, ?)
            """, mappings)
            conn.commit()
            logger.info(f"批量插入 {len(mappings)} 条基准映射")
        except Exception as e:
            logger.error(f"批量插入基准映射失败: {e}")
        finally:
            conn.close()

    def get_benchmark_map(self) -> pd.DataFrame:
        """查询所有基准映射"""
        conn = self._get_conn()
        try:
            return pd.read_sql_query("SELECT * FROM benchmark_map", conn)
        finally:
            conn.close()

    # ============================================================
    # 板块分组 CRUD
    # ============================================================
    def insert_sector_groups_batch(self, groups: List[Tuple]):
        """批量插入板块分组"""
        conn = self._get_conn()
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO sector_groups (group_name, sector_code, description)
                VALUES (?, ?, ?)
            """, groups)
            conn.commit()
            logger.info(f"批量插入 {len(groups)} 条板块分组")
        except Exception as e:
            logger.error(f"批量插入板块分组失败: {e}")
        finally:
            conn.close()

    def get_sector_groups(self) -> pd.DataFrame:
        """查询所有板块分组"""
        conn = self._get_conn()
        try:
            return pd.read_sql_query("SELECT * FROM sector_groups", conn)
        finally:
            conn.close()

    # ============================================================
    # 交易日历 CRUD
    # ============================================================
    def insert_trade_calendar_batch(self, dates: List[Tuple]):
        """批量插入交易日历"""
        conn = self._get_conn()
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO trade_calendar (trade_date, is_trading_day, week_day)
                VALUES (?, ?, ?)
            """, dates)
            conn.commit()
            logger.info(f"批量插入 {len(dates)} 条交易日历")
        except Exception as e:
            logger.error(f"批量插入交易日历失败: {e}")
        finally:
            conn.close()

    def get_trade_dates(self, start: str = None, end: str = None) -> List[str]:
        """查询交易日列表"""
        conn = self._get_conn()
        try:
            if start and end:
                cursor = conn.execute(
                    "SELECT trade_date FROM trade_calendar WHERE is_trading_day=1 AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                    (start, end)
                )
            else:
                cursor = conn.execute(
                    "SELECT trade_date FROM trade_calendar WHERE is_trading_day=1 ORDER BY trade_date"
                )
            return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

    def is_trading_day(self, date: str) -> bool:
        """判断是否为交易日"""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT is_trading_day FROM trade_calendar WHERE trade_date = ?",
                (date,)
            )
            row = cursor.fetchone()
            return row is not None and row[0] == 1
        finally:
            conn.close()

    # ============================================================
    # 数据新鲜度
    # ============================================================
    def update_freshness(self, data_type: str, data_key: str = None,
                         data_start: str = None, data_end: str = None,
                         record_count: int = None, status: str = "ok"):
        """更新数据新鲜度"""
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO data_freshness
                (data_type, data_key, last_update, data_start_date, data_end_date, record_count, status)
                VALUES (?, ?, datetime('now', 'localtime'), ?, ?, ?, ?)
            """, (data_type, data_key, data_start, data_end, record_count, status))
            conn.commit()
        except Exception as e:
            logger.error(f"更新数据新鲜度失败 {data_type}/{data_key}: {e}")
        finally:
            conn.close()

    def get_freshness_report(self) -> pd.DataFrame:
        """获取数据新鲜度报告"""
        conn = self._get_conn()
        try:
            df = pd.read_sql_query("""
                SELECT * FROM data_freshness
                ORDER BY data_type, data_key
            """, conn)
            return df
        finally:
            conn.close()

    def get_stale_data(self, stale_hours: int = 24) -> pd.DataFrame:
        """获取过期数据"""
        conn = self._get_conn()
        try:
            df = pd.read_sql_query("""
                SELECT * FROM data_freshness
                WHERE datetime(last_update) < datetime('now', 'localtime', ?)
                ORDER BY data_type, data_key
            """, conn, params=(f'-{stale_hours} hours',))
            return df
        finally:
            conn.close()

    # ============================================================
    # 股票基本信息
    # ============================================================
    def insert_stock(self, code: str, name: str, market: str = None,
                     industry: str = None, list_date: str = None):
        """插入股票信息"""
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO stocks (code, name, market, industry, list_date)
                VALUES (?, ?, ?, ?, ?)
            """, (code, name, market, industry, list_date))
            conn.commit()
        finally:
            conn.close()

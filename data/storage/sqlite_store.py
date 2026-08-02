"""
SQLite 存储引擎
===============
管理元数据存储，包括板块信息、成分股、交易日历、数据新鲜度等。
"""

import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Tuple
import pandas as pd

from config.sector_map import SW_LEVEL1_MAP, SW_LEVEL2_MAP
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

-- 信号事件账本：每条记录代表一个板块在某交易日发生的状态转换。
-- 作为信号绩效、历史回放、盘后报告的共享事实源；不记录状态不变的日子。
CREATE TABLE IF NOT EXISTS signal_events (
    event_date TEXT NOT NULL,           -- 当前状态生效交易日，YYYY-MM-DD
    sector_code TEXT NOT NULL,
    sector_name TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    from_signal TEXT NOT NULL,
    to_signal TEXT NOT NULL,
    action TEXT NOT NULL,               -- 72条路径对应的通用动作
    action_logic TEXT,
    trend TEXT,
    rs_percentile REAL,
    rs_momentum_percentile REAL,
    rs_momentum_cross_pct REAL,
    source_version TEXT NOT NULL,       -- 状态机口径版本，便于后续审计
    synced_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (event_date, sector_code),
    FOREIGN KEY (sector_code) REFERENCES sectors(code)
);

-- 信号后续表现账本：冻结每个状态切换事件在发出后的实际表现。
-- 由 signal_tracker 在管线完成后补全，供信号绩效、失效预警与历史回放复用，
-- 与 signal_events（事实源）分离，重算口径不影响原始事件记录。
CREATE TABLE IF NOT EXISTS signal_performance (
    event_date TEXT NOT NULL,           -- 与 signal_events 对应的事件交易日
    sector_code TEXT NOT NULL,
    sector_name TEXT,
    from_state TEXT NOT NULL,           -- 信号源状态（判定成败的基准）
    to_state TEXT NOT NULL,             -- 进入的目标状态（用户视角的“信号类型”）
    signal_direction TEXT,              -- BUY/SELL/HOLD/AVOID/NEUTRAL（基于 to_state 的通用方向）
    price_t0 REAL,                      -- 信号日收盘价（当时价格）
    base_price REAL,                    -- T+1 开盘价（模拟次日进场基准）
    close_t5 REAL,                      -- T+5 交易日收盘价
    close_t20 REAL,                     -- T+20 交易日收盘价
    state_t5 TEXT,                      -- T+5 交易日九宫格状态
    state_t20 TEXT,                     -- T+20 交易日九宫格状态
    return_t5 REAL,                     -- (close_t5 - base_price) / base_price
    return_t20 REAL,                    -- (close_t20 - base_price) / base_price
    excess_t20 REAL,                    -- return_t20 - 同期沪深300收益（超额收益）
    outcome TEXT,                       -- success / failure / neutral
    evaluated_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (event_date, sector_code),
    FOREIGN KEY (sector_code) REFERENCES sectors(code)
);

-- 用户持仓：仅保存当前仍持有的真实头寸；成交明细保存在 portfolio_transactions。
-- 平均成本由成交服务按买入加权计算，卖出不改变剩余头寸成本。
-- 多用户：user_id 标识持仓所有者，(user_id, security_code) 为唯一持仓。
CREATE TABLE IF NOT EXISTS portfolio_positions (
    user_id TEXT NOT NULL DEFAULT '',
    security_code TEXT NOT NULL,
    security_name TEXT NOT NULL,
    asset_type TEXT NOT NULL DEFAULT 'stock',
    sector_code TEXT,
    sector_name TEXT,
    quantity REAL NOT NULL CHECK(quantity > 0),
    avg_cost REAL NOT NULL CHECK(avg_cost >= 0),
    opened_date TEXT,
    target_weight REAL,
    stop_loss REAL,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (user_id, security_code)
);

-- 用户实际成交/调账日志：不被重算或覆盖，作为持仓事实的审计来源。
-- 多用户：user_id 标识成交所有者，与持仓一一对应。
CREATE TABLE IF NOT EXISTS portfolio_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT '',
    trade_date TEXT NOT NULL,
    security_code TEXT NOT NULL,
    security_name TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL', 'ADJUST')),
    quantity REAL NOT NULL CHECK(quantity > 0),
    price REAL NOT NULL CHECK(price >= 0),
    fee REAL NOT NULL DEFAULT 0 CHECK(fee >= 0),
    note TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_sector_stocks_sector ON sector_stocks(sector_code);
CREATE INDEX IF NOT EXISTS idx_sector_stocks_stock ON sector_stocks(stock_code);
CREATE INDEX IF NOT EXISTS idx_freshness_type ON data_freshness(data_type);
CREATE INDEX IF NOT EXISTS idx_trade_calendar_date ON trade_calendar(trade_date);
CREATE INDEX IF NOT EXISTS idx_sector_groups_group ON sector_groups(group_name);
CREATE INDEX IF NOT EXISTS idx_signal_events_sector_date ON signal_events(sector_code, event_date DESC);
CREATE INDEX IF NOT EXISTS idx_signal_events_path_date ON signal_events(from_state, to_state, event_date DESC);
CREATE INDEX IF NOT EXISTS idx_signal_performance_path_date ON signal_performance(from_state, to_state, event_date DESC);
CREATE INDEX IF NOT EXISTS idx_signal_performance_date ON signal_performance(event_date DESC);
CREATE INDEX IF NOT EXISTS idx_portfolio_transactions_security_date ON portfolio_transactions(security_code, trade_date DESC);

-- 板块资金流（每日管线落盘，来自 AkShare 行业资金流排名）
CREATE TABLE IF NOT EXISTS sector_fund_flow (
    sector_code TEXT NOT NULL,          -- 板块代码
    date        TEXT NOT NULL,          -- 数据日期
    signal      TEXT,                   -- 资金流信号：正向/中性/反向
    rank        INTEGER,                -- 主力净流入全市场排名（越小越强）
    rank_change INTEGER,                -- 相对前序窗口排名变化（正=改善）
    trend       TEXT,                   -- 资金流趋势：改善/恶化/稳定
    main_net_inflow REAL,               -- 真实主力净流入（亿元，来自 AkShare 同花顺行业资金流）
    updated_at  TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (sector_code, date)
);

-- 板块内分化度（一致性指标，每日管线落盘）
CREATE TABLE IF NOT EXISTS sector_divergence (
    sector_code TEXT NOT NULL,          -- 板块代码
    date        TEXT NOT NULL,          -- 数据日期
    divergence  REAL,                   -- 分化度：成分股涨幅标准差 / 指数日内波动率替代
    method      TEXT,                   -- 计算方法：component / intraday_vol
    updated_at  TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (sector_code, date)
);

-- 管线运行日志（运行保障 / 可观测性）
CREATE TABLE IF NOT EXISTS pipeline_run_log (
    run_id      TEXT PRIMARY KEY,       -- 运行标识（ISO 时间戳）
    started_at  TEXT,                   -- 开始时间
    finished_at TEXT,                   -- 结束时间
    status      TEXT,                   -- 状态：success / partial / failed
    target_date TEXT,                   -- 本次目标交易日
    steps       TEXT,                   -- 各步骤摘要（JSON 字符串）
    error       TEXT                    -- 失败原因（status=failed 时填充）
);
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
            # 数据新鲜度去重 + 唯一约束：历史版本用 INSERT（无唯一约束）导致同
            # (data_type, data_key) 重复记录堆积。先按 (data_type, data_key) 保留最新
            # 一条，再建唯一索引，使后续 record_update 的 INSERT OR REPLACE 真正生效。
            conn.execute(
                "DELETE FROM data_freshness WHERE rowid NOT IN ("
                "SELECT MAX(rowid) FROM data_freshness GROUP BY data_type, data_key)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_freshness_type_key "
                "ON data_freshness(data_type, data_key)"
            )
            # 迁移：sector_fund_flow 增加真实主力净流入列（旧库无此列）
            cols = [r[1] for r in conn.execute("PRAGMA table_info(sector_fund_flow)")]
            if "main_net_inflow" not in cols:
                conn.execute(
                    "ALTER TABLE sector_fund_flow ADD COLUMN main_net_inflow REAL"
                )
                logger.info("迁移：sector_fund_flow 已增加 main_net_inflow 列")
            # 迁移：持仓表支持多用户（user_id 列 + 复合主键）
            self._migrate_portfolio_user_scoping(conn)
            conn.commit()
            conn.close()
            logger.info(f"SQLite 数据库初始化完成: {self.db_path}")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")
            raise

    def _migrate_portfolio_user_scoping(self, conn: sqlite3.Connection) -> None:
        """将持仓表从单用户（security_code 主键）迁移到多用户（(user_id, security_code) 主键）。

        旧库已存在时：先补 user_id 列，再把主键改为复合主键（重建表并搬运数据）。
        全新库（CREATE_TABLES_SQL 已是新结构）则跳过。
        """
        # positions 表
        p_cols = [r[1] for r in conn.execute("PRAGMA table_info(portfolio_positions)")]
        if "user_id" not in p_cols:
            conn.execute(
                "ALTER TABLE portfolio_positions ADD COLUMN user_id TEXT NOT NULL DEFAULT ''"
            )
        # 注意：SQLite 的 ALTER ADD COLUMN 会改写 sqlite_master 中的建表语句，
        # 因此不能靠「建表语句是否含 user_id」判断，必须看 user_id 是否已是主键一部分。
        user_row = conn.execute(
            "SELECT pk FROM pragma_table_info('portfolio_positions') WHERE name='user_id'"
        ).fetchone()
        is_user_scoped = bool(user_row and user_row[0] and user_row[0] > 0)
        if not is_user_scoped:
            # 旧结构仍只有 security_code 主键，重建为复合主键
            conn.execute("ALTER TABLE portfolio_positions RENAME TO portfolio_positions_old")
            conn.execute(
                """
                CREATE TABLE portfolio_positions (
                    user_id TEXT NOT NULL DEFAULT '',
                    security_code TEXT NOT NULL,
                    security_name TEXT NOT NULL,
                    asset_type TEXT NOT NULL DEFAULT 'stock',
                    sector_code TEXT,
                    sector_name TEXT,
                    quantity REAL NOT NULL CHECK(quantity > 0),
                    avg_cost REAL NOT NULL CHECK(avg_cost >= 0),
                    opened_date TEXT,
                    target_weight REAL,
                    stop_loss REAL,
                    note TEXT,
                    created_at TEXT DEFAULT (datetime('now', 'localtime')),
                    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                    PRIMARY KEY (user_id, security_code)
                )
                """
            )
            # 旧表列可能不完全（不同历史版本），按存在性逐列回退，避免 SELECT 引用不存在的列
            src_cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_positions_old)")}
            def _col(name: str, fallback: str) -> str:
                return name if name in src_cols else fallback
            conn.execute(
                f"""
                INSERT INTO portfolio_positions
                    (user_id, security_code, security_name, asset_type, sector_code, sector_name,
                     quantity, avg_cost, opened_date, target_weight, stop_loss, note, created_at, updated_at)
                SELECT
                    COALESCE(user_id, ''),
                    {_col('security_code', 'security_code')},
                    {_col('security_name', "''")},
                    {_col('asset_type', "'stock'")},
                    {_col('sector_code', 'NULL')},
                    {_col('sector_name', 'NULL')},
                    {_col('quantity', 'quantity')},
                    {_col('avg_cost', 'avg_cost')},
                    {_col('opened_date', 'NULL')},
                    {_col('target_weight', 'NULL')},
                    {_col('stop_loss', 'NULL')},
                    {_col('note', 'NULL')},
                    datetime('now', 'localtime'),
                    datetime('now', 'localtime')
                FROM portfolio_positions_old
                """
            )
            conn.execute("DROP TABLE portfolio_positions_old")
            logger.info("迁移：portfolio_positions 已升级为多用户复合主键")
        # transactions 表
        t_cols = [r[1] for r in conn.execute("PRAGMA table_info(portfolio_transactions)")]
        if "user_id" not in t_cols:
            conn.execute(
                "ALTER TABLE portfolio_transactions ADD COLUMN user_id TEXT NOT NULL DEFAULT ''"
            )

    def ensure_sectors(self) -> int:
        """从本地 sector_map 初始化 sectors 表（幂等）。

        云端部署没有 sectors 元数据，但 signal_events/signal_performance 等表依赖
        sectors(code) 外键。该方法无需网络，仅从 config.sector_map 重建基础板块
        信息。返回写入/更新的行数。
        """
        rows = []
        # 申万一级
        for code, name in SW_LEVEL1_MAP.items():
            rows.append((code, name, 1, None, None, None))
        # 申万二级
        for code, (name, parent_code, parent_name) in SW_LEVEL2_MAP.items():
            rows.append((code, name, 2, parent_code, parent_name, None))
        self.insert_sectors_batch(rows)
        return len(rows)

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

    def clear_sectors(self):
        """清空 sectors 表（切源后重建用）。"""
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM sectors")
            conn.commit()
        except Exception as e:
            logger.error(f"清空 sectors 失败: {e}")
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
    def clear_benchmark_map(self):
        """清空 benchmark_map 表（切源后重建用）。"""
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM benchmark_map")
            conn.commit()
        except Exception as e:
            logger.error(f"清空 benchmark_map 失败: {e}")
        finally:
            conn.close()

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
    def clear_sector_groups(self):
        """清空 sector_groups 表（切源后重建用）。"""
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM sector_groups")
            conn.commit()
        except Exception as e:
            logger.error(f"清空 sector_groups 失败: {e}")
        finally:
            conn.close()

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

    def count_trade_calendar(self) -> int:
        """返回交易日历记录数（用于判断是否已初始化）。"""
        conn = self._get_conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM trade_calendar").fetchone()[0]
        finally:
            conn.close()

    # ============================================================
    # 板块资金流
    # ============================================================
    def upsert_sector_fund_flow(self, rows: List[Dict]):
        """批量写入/更新板块资金流（每日管线落盘）。"""
        if not rows:
            return
        conn = self._get_conn()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO sector_fund_flow "
                "(sector_code, date, signal, rank, rank_change, trend, "
                "main_net_inflow, updated_at) "
                "VALUES (:sector_code, :date, :signal, :rank, :rank_change, :trend, "
                ":main_net_inflow, datetime('now', 'localtime'))",
                rows,
            )
            conn.commit()
        except Exception as e:
            logger.error(f"写入板块资金流失败: {e}")
        finally:
            conn.close()

    def get_sector_fund_flow(self, sector_code: str = None, date: str = None) -> pd.DataFrame:
        """查询板块资金流；不传条件返回全部（按日期倒序、排名升序）。"""
        conn = self._get_conn()
        try:
            q = "SELECT * FROM sector_fund_flow WHERE 1=1"
            params = []
            if sector_code:
                q += " AND sector_code=?"
                params.append(sector_code)
            if date:
                q += " AND date=?"
                params.append(date)
            q += " ORDER BY date DESC, rank ASC"
            return pd.read_sql_query(q, conn, params=params)
        finally:
            conn.close()

    def get_latest_fund_flow_date(self) -> Optional[str]:
        """返回资金流表最新日期（无数据返回 None）。"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT date FROM sector_fund_flow ORDER BY date DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    # ============================================================
    # 板块分化度
    # ============================================================
    def upsert_sector_divergence(self, rows: List[Dict]):
        """批量写入/更新板块分化度（每日管线落盘）。"""
        if not rows:
            return
        conn = self._get_conn()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO sector_divergence "
                "(sector_code, date, divergence, method, updated_at) "
                "VALUES (:sector_code, :date, :divergence, :method, "
                "datetime('now', 'localtime'))",
                rows,
            )
            conn.commit()
        except Exception as e:
            logger.error(f"写入板块分化度失败: {e}")
        finally:
            conn.close()

    def get_sector_divergence(self, sector_code: str = None, date: str = None) -> pd.DataFrame:
        """查询板块分化度；不传条件返回全部（按日期倒序）。"""
        conn = self._get_conn()
        try:
            q = "SELECT * FROM sector_divergence WHERE 1=1"
            params = []
            if sector_code:
                q += " AND sector_code=?"
                params.append(sector_code)
            if date:
                q += " AND date=?"
                params.append(date)
            q += " ORDER BY date DESC"
            return pd.read_sql_query(q, conn, params=params)
        finally:
            conn.close()

    def get_latest_divergence_date(self) -> Optional[str]:
        """返回分化度表最新日期（无数据返回 None）。"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT date FROM sector_divergence ORDER BY date DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def delete_sector_fund_flow_not_in(self, codes: List[str]) -> int:
        """删除 sector_fund_flow 中不在 codes 列表里的记录，返回删除行数。"""
        if not codes:
            return 0
        conn = self._get_conn()
        try:
            placeholders = ",".join("?" * len(codes))
            cur = conn.execute(
                f"DELETE FROM sector_fund_flow WHERE sector_code NOT IN ({placeholders})",
                tuple(codes),
            )
            conn.commit()
            return cur.rowcount
        except Exception as e:
            logger.error(f"清理旧 sector_fund_flow 记录失败: {e}")
            return 0
        finally:
            conn.close()

    def delete_sector_divergence_not_in(self, codes: List[str]) -> int:
        """删除 sector_divergence 中不在 codes 列表里的记录，返回删除行数。"""
        if not codes:
            return 0
        conn = self._get_conn()
        try:
            placeholders = ",".join("?" * len(codes))
            cur = conn.execute(
                f"DELETE FROM sector_divergence WHERE sector_code NOT IN ({placeholders})",
                tuple(codes),
            )
            conn.commit()
            return cur.rowcount
        except Exception as e:
            logger.error(f"清理旧 sector_divergence 记录失败: {e}")
            return 0
        finally:
            conn.close()

    # ============================================================
    # 管线运行日志（运行保障 / 可观测性）
    # ============================================================
    def log_pipeline_run(self, run_id: str, started_at: str, finished_at: str,
                         status: str, target_date: str, steps: str, error: str = None):
        """写入一条管线运行记录（INSERT OR REPLACE）。"""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO pipeline_run_log "
                "(run_id, started_at, finished_at, status, target_date, steps, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, started_at, finished_at, status, target_date, steps, error),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"写入管线运行日志失败: {e}")
        finally:
            conn.close()

    def get_last_pipeline_runs(self, n: int = 5) -> List[Dict]:
        """返回最近 n 条管线运行记录（按开始时间倒序）。"""
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT * FROM pipeline_run_log ORDER BY started_at DESC LIMIT ?", (n,)
            )
            return [dict(r) for r in cursor.fetchall()]
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

    def delete_freshness(self, data_type: str, data_key: str = None):
        """删除已废弃或错误的数据新鲜度记录。"""
        conn = self._get_conn()
        try:
            if data_key is None:
                conn.execute("DELETE FROM data_freshness WHERE data_type = ?", (data_type,))
            else:
                conn.execute(
                    "DELETE FROM data_freshness WHERE data_type = ? AND data_key = ?",
                    (data_type, data_key),
                )
            conn.commit()
        except Exception as e:
            logger.error(f"删除数据新鲜度记录失败 {data_type}/{data_key}: {e}")
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
    # 用户持仓账本
    # ============================================================
    def get_portfolio_positions(self, user_id: str = "") -> pd.DataFrame:
        """返回某用户的当前持仓，按更新时间倒序。user_id 为空表示未归属（旧数据）。"""
        conn = self._get_conn()
        try:
            return pd.read_sql_query(
                "SELECT * FROM portfolio_positions WHERE user_id = ? ORDER BY updated_at DESC, security_code",
                conn,
                params=[str(user_id)],
            )
        finally:
            conn.close()

    def get_portfolio_transactions(self, security_code: str = None, limit: int = 200,
                                   user_id: str = "") -> pd.DataFrame:
        """返回某用户的真实操作日志；可按证券代码筛选。"""
        conn = self._get_conn()
        try:
            sql = "SELECT * FROM portfolio_transactions WHERE user_id = ?"
            params = [str(user_id)]
            if security_code:
                sql += " AND security_code = ?"
                params.append(security_code)
            sql += " ORDER BY trade_date DESC, id DESC LIMIT ?"
            params.append(int(limit))
            return pd.read_sql_query(sql, conn, params=params)
        finally:
            conn.close()

    def record_portfolio_transaction(
        self,
        trade_date: str,
        security_code: str,
        security_name: str,
        side: str,
        quantity: float,
        price: float,
        fee: float = 0.0,
        note: str = None,
        asset_type: str = "stock",
        sector_code: str = None,
        sector_name: str = None,
        target_weight: float = None,
        stop_loss: float = None,
        user_id: str = "",
    ) -> None:
        """原子记录一笔实际成交并更新当前持仓。

        BUY 按成交金额（含费用）更新加权平均成本；SELL 只减少数量；ADJUST
        用于数量调整且不改变成本。卖出超过持仓会拒绝，避免账本出现负数。
        所有读写按 user_id 隔离，确保不同使用者的仓位互不可见。
        """
        side = str(side).upper()
        if side not in {"BUY", "SELL", "ADJUST"}:
            raise ValueError("side 必须是 BUY、SELL 或 ADJUST")
        quantity, price, fee = float(quantity), float(price), float(fee)
        if quantity <= 0 or price < 0 or fee < 0:
            raise ValueError("数量必须大于 0，价格和费用不能为负数")
        user_id = str(user_id)

        conn = self._get_conn()
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT * FROM portfolio_positions WHERE user_id = ? AND security_code = ?",
                (user_id, security_code),
            ).fetchone()
            current_qty = float(row["quantity"]) if row else 0.0
            current_cost = float(row["avg_cost"]) if row else 0.0

            if side == "SELL" and quantity > current_qty + 1e-8:
                raise ValueError(f"卖出数量 {quantity:g} 超过当前持仓 {current_qty:g}")
            if side == "ADJUST" and not row:
                raise ValueError("调整持仓前需先存在当前头寸")

            if side == "BUY":
                new_qty = current_qty + quantity
                new_cost = ((current_qty * current_cost) + (quantity * price) + fee) / new_qty
            elif side == "SELL":
                new_qty = current_qty - quantity
                new_cost = current_cost
            else:
                new_qty = quantity
                new_cost = current_cost

            conn.execute(
                """
                INSERT INTO portfolio_transactions
                    (user_id, trade_date, security_code, security_name, side, quantity, price, fee, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, trade_date, security_code, security_name, side, quantity, price, fee, note),
            )

            if new_qty <= 1e-8:
                conn.execute(
                    "DELETE FROM portfolio_positions WHERE user_id = ? AND security_code = ?",
                    (user_id, security_code),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO portfolio_positions
                        (user_id, security_code, security_name, asset_type, sector_code, sector_name,
                         quantity, avg_cost, opened_date, target_weight, stop_loss, note, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                    ON CONFLICT(user_id, security_code) DO UPDATE SET
                        security_name = excluded.security_name,
                        asset_type = excluded.asset_type,
                        sector_code = COALESCE(excluded.sector_code, portfolio_positions.sector_code),
                        sector_name = COALESCE(excluded.sector_name, portfolio_positions.sector_name),
                        quantity = excluded.quantity,
                        avg_cost = excluded.avg_cost,
                        target_weight = COALESCE(excluded.target_weight, portfolio_positions.target_weight),
                        stop_loss = COALESCE(excluded.stop_loss, portfolio_positions.stop_loss),
                        note = COALESCE(excluded.note, portfolio_positions.note),
                        updated_at = datetime('now', 'localtime')
                    """,
                    (
                        user_id, security_code, security_name, asset_type, sector_code, sector_name,
                        new_qty, new_cost, trade_date, target_weight, stop_loss, note,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_portfolio_metadata(self, user_id: str, security_code: str, **fields) -> None:
        """仅更新当前用户持仓元数据，不写交易流水、不改数量/成本。"""
        allowed = {"security_name", "asset_type", "sector_code", "sector_name", "quantity", "avg_cost", "target_weight", "stop_loss", "note"}
        changes = {k: v for k, v in fields.items() if k in allowed}
        if not changes:
            raise ValueError("没有可修改的持仓属性")
        if "asset_type" in changes and changes["asset_type"] not in {"stock", "etf", "fund"}:
            raise ValueError("资产类型只能是 stock、etf 或 fund")
        if "quantity" in changes and float(changes["quantity"]) <= 0:
            raise ValueError("持仓数量必须大于 0")
        if "avg_cost" in changes and float(changes["avg_cost"]) < 0:
            raise ValueError("平均成本不能为负数")
        conn = self._get_conn()
        try:
            cols, vals = [], []
            for key, value in changes.items():
                cols.append(f"{key} = ?")
                vals.append(value)
            vals.extend([str(user_id), str(security_code)])
            cur = conn.execute(
                f"UPDATE portfolio_positions SET {', '.join(cols)}, updated_at = datetime('now', 'localtime') "
                "WHERE user_id = ? AND security_code = ?",
                vals,
            )
            if cur.rowcount == 0:
                raise ValueError("未找到要修改的持仓")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ============================================================
    # 信号事件账本
    # ============================================================
    def upsert_signal_events(self, events: List[Tuple]) -> int:
        """幂等写入状态切换事件，唯一键为（当前交易日、板块代码）。

        每条 tuple 顺序为：event_date, sector_code, sector_name, from_state,
        to_state, from_signal, to_signal, action, action_logic, trend,
        rs_percentile, rs_momentum_percentile, rs_momentum_cross_pct,
        source_version。重复同步会更新同一事实记录，不新增重复事件。
        """
        if not events:
            return 0
        conn = self._get_conn()
        try:
            conn.executemany("""
                INSERT INTO signal_events (
                    event_date, sector_code, sector_name, from_state, to_state,
                    from_signal, to_signal, action, action_logic, trend,
                    rs_percentile, rs_momentum_percentile, rs_momentum_cross_pct,
                    source_version, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(event_date, sector_code) DO UPDATE SET
                    sector_name=excluded.sector_name,
                    from_state=excluded.from_state,
                    to_state=excluded.to_state,
                    from_signal=excluded.from_signal,
                    to_signal=excluded.to_signal,
                    action=excluded.action,
                    action_logic=excluded.action_logic,
                    trend=excluded.trend,
                    rs_percentile=excluded.rs_percentile,
                    rs_momentum_percentile=excluded.rs_momentum_percentile,
                    rs_momentum_cross_pct=excluded.rs_momentum_cross_pct,
                    source_version=excluded.source_version,
                    synced_at=datetime('now', 'localtime')
            """, events)
            conn.commit()
            return len(events)
        except Exception as e:
            conn.rollback()
            logger.error(f"写入信号事件失败: {e}")
            raise
        finally:
            conn.close()

    def get_signal_events(
        self,
        start: str = None,
        end: str = None,
        sector_code: str = None,
        limit: int = None,
    ) -> pd.DataFrame:
        """查询信号事件账本，按事件日期倒序返回。"""
        clauses, params = [], []
        if start:
            clauses.append("event_date >= ?")
            params.append(start)
        if end:
            clauses.append("event_date <= ?")
            params.append(end)
        if sector_code:
            clauses.append("sector_code = ?")
            params.append(sector_code)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = "SELECT * FROM signal_events" + where + " ORDER BY event_date DESC, sector_code"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        conn = self._get_conn()
        try:
            return pd.read_sql_query(sql, conn, params=params)
        finally:
            conn.close()

    def upsert_signal_performance(self, rows: List[Tuple]) -> int:
        """幂等写入信号后续表现，唯一键为（事件交易日、板块代码）。

        每条 tuple 顺序为：event_date, sector_code, sector_name, from_state,
        to_state, signal_direction, price_t0, base_price, close_t5, close_t20,
        state_t5, state_t20, return_t5, return_t20, excess_t20, outcome。
        重复评估会覆盖同一事实记录的后续表现，便于口径演进后重算。
        """
        if not rows:
            return 0
        conn = self._get_conn()
        try:
            conn.executemany("""
                INSERT INTO signal_performance (
                    event_date, sector_code, sector_name, from_state, to_state,
                    signal_direction, price_t0, base_price, close_t5, close_t20,
                    state_t5, state_t20, return_t5, return_t20, excess_t20, outcome,
                    evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(event_date, sector_code) DO UPDATE SET
                    sector_name=excluded.sector_name,
                    from_state=excluded.from_state,
                    to_state=excluded.to_state,
                    signal_direction=excluded.signal_direction,
                    price_t0=excluded.price_t0,
                    base_price=excluded.base_price,
                    close_t5=excluded.close_t5,
                    close_t20=excluded.close_t20,
                    state_t5=excluded.state_t5,
                    state_t20=excluded.state_t20,
                    return_t5=excluded.return_t5,
                    return_t20=excluded.return_t20,
                    excess_t20=excluded.excess_t20,
                    outcome=excluded.outcome,
                    evaluated_at=datetime('now', 'localtime')
            """, rows)
            conn.commit()
            return len(rows)
        except Exception as e:
            conn.rollback()
            logger.error(f"写入信号后续表现失败: {e}")
            raise
        finally:
            conn.close()

    def get_signal_performance(
        self,
        start: str = None,
        end: str = None,
        sector_code: str = None,
        limit: int = None,
    ) -> pd.DataFrame:
        """查询信号后续表现账本，按事件日期倒序返回。"""
        clauses, params = [], []
        if start:
            clauses.append("event_date >= ?")
            params.append(start)
        if end:
            clauses.append("event_date <= ?")
            params.append(end)
        if sector_code:
            clauses.append("sector_code = ?")
            params.append(sector_code)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = "SELECT * FROM signal_performance" + where + " ORDER BY event_date DESC, sector_code"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        conn = self._get_conn()
        try:
            return pd.read_sql_query(sql, conn, params=params)
        finally:
            conn.close()

    def count_signal_performance(self) -> int:
        """返回已评估的信号后续表现记录数。"""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM signal_performance").fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def clear_signal_performance(self) -> int:
        """清空信号后续表现账本（重算口径前调用）。返回删除记录数。"""
        conn = self._get_conn()
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM signal_performance").fetchone()["n"]
            conn.execute("DELETE FROM signal_performance")
            conn.commit()
            return int(n)
        except Exception as e:
            conn.rollback()
            logger.error(f"清空信号后续表现失败: {e}")
            raise
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

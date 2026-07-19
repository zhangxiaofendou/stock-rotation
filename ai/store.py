"""
AI 模块数据存储
==============
研报与新闻的结构化存储，落库到与主系统相同的 SQLite 文件，便于复用
sectors(code) 外键与主看板元数据。

设计原则（遵循 PRD §7.4 / §5.6.3 阶段 E）：
- 每条研报/新闻结论都保留来源（券商 / 媒体）、时间（coverage_date / published_at）
  与可追溯字段，绝不凭空生成。
- 研报数据由爬虫或人工导入写入；本模块只负责结构化存储与读取，不抓取。
- 区分示例数据（is_seed=1）与真实数据，便于云端自初始化与演示。
"""

import sqlite3
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from config.logger import get_logger
from config.settings import SQLITE_DB_PATH

logger = get_logger(__name__)


CREATE_AI_TABLES_SQL = """
-- 研报结构化记录：评级 / 目标价 / 核心观点 / 风险关键词
-- 来源可追溯：broker + coverage_date + source_url
CREATE TABLE IF NOT EXISTS research_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector_code TEXT NOT NULL,          -- 关联申万二级板块（FK -> sectors.code）
    sector_name TEXT,
    broker TEXT NOT NULL,               -- 发布券商
    stock_code TEXT,                    -- 具体覆盖个股（板块级研报可为空）
    stock_name TEXT,
    rating TEXT,                        -- 当前评级：买入/增持/中性/减持/卖出
    prev_rating TEXT,                   -- 上次评级（用于判定上调/下调）
    target_price REAL,                  -- 目标价
    prev_target_price REAL,             -- 上次目标价
    rating_change TEXT,                 -- 上调 / 下调 / 首次 / 维持
    target_change_pct REAL,             -- (target - prev_target)/prev_target
    coverage_date TEXT NOT NULL,        -- 覆盖/评级日期 YYYY-MM-DD
    core_view TEXT,                     -- 核心观点（必读信息）
    risk_keywords TEXT,                 -- 风险关键词（逗号分隔）
    source_url TEXT,                    -- 原文链接
    is_seed INTEGER DEFAULT 0,          -- 1=示例数据；0=真实导入
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (sector_code) REFERENCES sectors(code)
);

-- 新闻条目：分类 + 情绪，用于新闻归类与情绪反向指标
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector_code TEXT,                   -- 关联板块（可为空表示全市场）
    headline TEXT NOT NULL,             -- 新闻标题
    category TEXT,                      -- 政策 / 公司 / 行业 / 宏观 / 其他
    sentiment TEXT,                     -- positive / negative / neutral
    published_at TEXT NOT NULL,         -- 发布时间 YYYY-MM-DD
    source TEXT,                        -- 媒体来源
    url TEXT,
    is_seed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_research_sector_date
    ON research_reports(sector_code, coverage_date DESC);
CREATE INDEX IF NOT EXISTS idx_news_sector_date
    ON news_items(sector_code, published_at DESC);
"""


class AIStore:
    """AI 模块的读写存储，复用主 SQLite 数据库文件。"""

    def __init__(self, db_path: str = None):
        self.db_path = str(db_path or SQLITE_DB_PATH)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        try:
            conn = self._get_conn()
            conn.executescript(CREATE_AI_TABLES_SQL)
            conn.commit()
            conn.close()
        except Exception as e:  # pragma: no cover
            logger.error(f"AI 存储初始化失败: {e}")
            raise

    # ============================================================
    # 研报 CRUD
    # ============================================================
    def upsert_research_reports(self, rows: List[Tuple]) -> int:
        """幂等写入研报。rows 元素：
        (sector_code, sector_name, broker, stock_code, stock_name, rating,
         prev_rating, target_price, prev_target_price, rating_change,
         target_change_pct, coverage_date, core_view, risk_keywords,
         source_url, is_seed)
        以 (broker, sector_code, stock_code, coverage_date) 去重更新。
        """
        if not rows:
            return 0
        conn = self._get_conn()
        try:
            conn.executemany(
                """
                INSERT INTO research_reports (
                    sector_code, sector_name, broker, stock_code, stock_name,
                    rating, prev_rating, target_price, prev_target_price,
                    rating_change, target_change_pct, coverage_date,
                    core_view, risk_keywords, source_url, is_seed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    rating=excluded.rating, prev_rating=excluded.prev_rating,
                    target_price=excluded.target_price,
                    prev_target_price=excluded.prev_target_price,
                    rating_change=excluded.rating_change,
                    target_change_pct=excluded.target_change_pct,
                    core_view=excluded.core_view,
                    risk_keywords=excluded.risk_keywords,
                    source_url=excluded.source_url
                """,
                rows,
            )
            conn.commit()
            return len(rows)
        except Exception as e:
            conn.rollback()
            logger.error(f"写入研报失败: {e}")
            raise
        finally:
            conn.close()

    def get_research_reports(
        self,
        sector_code: str = None,
        since: str = None,
        include_seed: bool = True,
        limit: int = 500,
    ) -> List[Dict]:
        """查询研报，按覆盖日期倒序。"""
        clauses, params = [], []
        if sector_code:
            clauses.append("sector_code = ?")
            params.append(sector_code)
        if since:
            clauses.append("coverage_date >= ?")
            params.append(since)
        if not include_seed:
            clauses.append("is_seed = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM research_reports" + where +
            " ORDER BY coverage_date DESC, id DESC LIMIT ?"
        )
        params.append(int(limit))
        conn = self._get_conn()
        try:
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def count_research_reports(self, include_seed: bool = True) -> int:
        conn = self._get_conn()
        try:
            sql = "SELECT COUNT(*) AS n FROM research_reports"
            if not include_seed:
                sql += " WHERE is_seed = 0"
            return int(conn.execute(sql).fetchone()["n"])
        finally:
            conn.close()

    def clear_seed_research(self) -> int:
        """清空示例研报（真实导入前清理演示数据）。"""
        conn = self._get_conn()
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM research_reports WHERE is_seed = 1"
            ).fetchone()["n"]
            conn.execute("DELETE FROM research_reports WHERE is_seed = 1")
            conn.commit()
            return int(n)
        finally:
            conn.close()

    # ============================================================
    # 新闻 CRUD
    # ============================================================
    def upsert_news(self, rows: List[Tuple]) -> int:
        """幂等写入新闻。rows 元素：
        (sector_code, headline, category, sentiment, published_at, source, url, is_seed)
        """
        if not rows:
            return 0
        conn = self._get_conn()
        try:
            conn.executemany(
                """
                INSERT INTO news_items (
                    sector_code, headline, category, sentiment,
                    published_at, source, url, is_seed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
            return len(rows)
        except Exception as e:
            conn.rollback()
            logger.error(f"写入新闻失败: {e}")
            raise
        finally:
            conn.close()

    def get_news(
        self,
        sector_code: str = None,
        since: str = None,
        include_seed: bool = True,
        limit: int = 500,
    ) -> List[Dict]:
        clauses, params = [], []
        if sector_code:
            clauses.append("sector_code = ?")
            params.append(sector_code)
        if since:
            clauses.append("published_at >= ?")
            params.append(since)
        if not include_seed:
            clauses.append("is_seed = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM news_items" + where +
            " ORDER BY published_at DESC, id DESC LIMIT ?"
        )
        params.append(int(limit))
        conn = self._get_conn()
        try:
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def count_news(self, include_seed: bool = True) -> int:
        conn = self._get_conn()
        try:
            sql = "SELECT COUNT(*) AS n FROM news_items"
            if not include_seed:
                sql += " WHERE is_seed = 0"
            return int(conn.execute(sql).fetchone()["n"])
        finally:
            conn.close()

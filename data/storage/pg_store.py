"""PostgreSQL 持久化存储（账号凭证 + 用户持仓）。

为什么需要它
------------
Streamlit Cloud 免费套餐没有 Persistent Storage，容器磁盘在每次重部署 / 重启后清空。
行情缓存丢了可以重新拉，但**账号和持仓是用户资产数据，绝不能丢**。
因此当环境中配置了 DATABASE_URL（例如 Supabase / Neon 的免费 Postgres）时，
账号凭证与持仓账本改为写入云数据库；未配置时自动回退到本地 SQLite / JSON，
本地开发与既有行为完全不变。

驱动兼容
--------
同时支持 psycopg（v3）与 psycopg2，二者占位符均为 %s，SQL 可共用。
优先 psycopg3（对新版 Python 的 wheel 支持更好）。
"""

import hashlib
import os
import secrets
import threading
import time
from typing import Any, List, Optional, Sequence, Tuple

import pandas as pd

# ------------------------------------------------------------
# 驱动加载（二选一，缺失时优雅降级）
# ------------------------------------------------------------
_DRIVER = None
_connect = None

try:  # psycopg 3
    import psycopg as _pg3

    _DRIVER = "psycopg3"
    _connect = _pg3.connect
except Exception:
    try:  # psycopg2
        import psycopg2 as _pg2

        _DRIVER = "psycopg2"
        _connect = _pg2.connect
    except Exception:
        _DRIVER = None
        _connect = None


# ------------------------------------------------------------
# 连接串获取
# ------------------------------------------------------------
def get_database_url() -> Optional[str]:
    """读取 DATABASE_URL。

    Streamlit Cloud 的 Secrets 会同时注入为环境变量，因此优先读 env；
    若拿不到则再尝试 st.secrets（本地 .streamlit/secrets.toml 场景）。
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return _normalize_url(url.strip())
    try:
        import streamlit as st  # 延迟导入，保证非 Streamlit 环境可用

        val = st.secrets.get("DATABASE_URL")  # type: ignore[attr-defined]
        if val:
            return _normalize_url(str(val).strip())
    except Exception:
        pass
    return None


def _normalize_url(url: str) -> str:
    """补全 Supabase 等托管库必须的 sslmode，并统一 scheme。"""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def is_enabled() -> bool:
    """当前是否应使用 Postgres 持久化。"""
    return bool(_DRIVER) and bool(get_database_url())


def driver_name() -> str:
    return _DRIVER or "none"


# ------------------------------------------------------------
# 建表
# ------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_users (
    username    TEXT PRIMARY KEY,
    salt        TEXT NOT NULL,
    pwd_hash    TEXT NOT NULL,
    iterations  INTEGER NOT NULL,
    created_at  TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS app_kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_positions (
    user_id       TEXT NOT NULL DEFAULT '',
    security_code TEXT NOT NULL,
    security_name TEXT NOT NULL,
    asset_type    TEXT NOT NULL DEFAULT 'stock',
    sector_code   TEXT,
    sector_name   TEXT,
    quantity      DOUBLE PRECISION NOT NULL CHECK (quantity > 0),
    avg_cost      DOUBLE PRECISION NOT NULL CHECK (avg_cost >= 0),
    opened_date   TEXT,
    target_weight DOUBLE PRECISION,
    stop_loss     DOUBLE PRECISION,
    note          TEXT,
    created_at    TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    updated_at    TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    PRIMARY KEY (user_id, security_code)
);

CREATE TABLE IF NOT EXISTS portfolio_transactions (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL DEFAULT '',
    trade_date    TEXT NOT NULL,
    security_code TEXT NOT NULL,
    security_name TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('BUY', 'SELL', 'ADJUST')),
    quantity      DOUBLE PRECISION NOT NULL CHECK (quantity > 0),
    price         DOUBLE PRECISION NOT NULL CHECK (price >= 0),
    fee           DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (fee >= 0),
    note          TEXT,
    created_at    TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE INDEX IF NOT EXISTS idx_pg_tx_user_date
    ON portfolio_transactions (user_id, trade_date DESC, id DESC);
"""

_schema_lock = threading.Lock()
_schema_ready = False


class PGUnavailable(RuntimeError):
    """Postgres 不可用（未配置 / 无驱动 / 连不上）。"""


def connect():
    """建立一个新连接；调用方负责关闭。"""
    if not _DRIVER:
        raise PGUnavailable(
            "未安装 Postgres 驱动。请在 requirements.txt 中加入 psycopg[binary] 或 psycopg2-binary。"
        )
    url = get_database_url()
    if not url:
        raise PGUnavailable("未配置 DATABASE_URL。")
    return _connect(url)


def ensure_schema(force: bool = False) -> None:
    """幂等建表，进程内只跑一次。"""
    global _schema_ready
    if _schema_ready and not force:
        return
    with _schema_lock:
        if _schema_ready and not force:
            return
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA)
            conn.commit()
            _schema_ready = True
        finally:
            conn.close()


def _rows_to_df(cur) -> pd.DataFrame:
    cols = [d[0] for d in (cur.description or [])]
    rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols) if cols else pd.DataFrame()


def healthcheck() -> Tuple[bool, str]:
    """连通性自检，返回 (是否可用, 说明)。用于界面诊断。"""
    if not _DRIVER:
        return False, "未安装 Postgres 驱动（psycopg / psycopg2）"
    if not get_database_url():
        return False, "未配置 DATABASE_URL"
    try:
        ensure_schema()
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        return True, f"Postgres 已连通（驱动 {_DRIVER}）"
    except Exception as e:
        msg = str(e)
        hint = ""
        low = msg.lower()
        if "network is unreachable" in low or "could not translate host" in low or "timeout" in low:
            hint = (
                "  提示：Supabase 的直连地址（db.xxx.supabase.co）仅支持 IPv6，"
                "Streamlit Cloud 走 IPv4 连不上。请在 Supabase 的 Connect 弹窗里改用 "
                "「Session pooler」或「Transaction pooler」提供的连接串"
                "（形如 postgresql://postgres.xxxx:密码@aws-0-区域.pooler.supabase.com:5432/postgres）。"
            )
        elif "password authentication failed" in low:
            hint = "  提示：数据库密码不对，请回 Supabase 重置或复制正确密码。"
        return False, f"连接失败：{msg}{hint}"


# ============================================================
# 账号凭证
# ============================================================
_creds_cache: Optional[dict] = None
_creds_cache_at: float = 0.0
_CREDS_TTL = 20.0  # 秒；避免每次 rerun 都打一次数据库


def load_credentials(force: bool = False) -> dict:
    """返回 {"users": {username: {salt, hash, iter}}}，结构与本地 JSON 版一致。"""
    global _creds_cache, _creds_cache_at
    now = time.time()
    if not force and _creds_cache is not None and (now - _creds_cache_at) < _CREDS_TTL:
        return _creds_cache
    ensure_schema()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username, salt, pwd_hash, iterations FROM app_users")
            rows = cur.fetchall()
    finally:
        conn.close()
    users = {
        r[0]: {"salt": r[1], "hash": r[2], "iter": int(r[3])}
        for r in rows
    }
    _creds_cache = {"users": users}
    _creds_cache_at = now
    return _creds_cache


def invalidate_credentials_cache() -> None:
    global _creds_cache, _creds_cache_at
    _creds_cache = None
    _creds_cache_at = 0.0


def add_user(username: str, rec: dict) -> bool:
    """新增用户；用户名已存在返回 False。"""
    ensure_schema()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_users (username, salt, pwd_hash, iterations)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (username) DO NOTHING
                """,
                (username, rec["salt"], rec["hash"], int(rec.get("iter", 200_000))),
            )
            inserted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    invalidate_credentials_cache()
    return inserted > 0


def get_kv(key: str) -> Optional[str]:
    ensure_schema()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT v FROM app_kv WHERE k = %s", (key,))
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_kv_if_absent(key: str, value: str) -> str:
    """写入并返回最终生效的值（并发下也保证全局唯一）。"""
    ensure_schema()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_kv (k, v) VALUES (%s, %s) ON CONFLICT (k) DO NOTHING",
                (key, value),
            )
            conn.commit()
            cur.execute("SELECT v FROM app_kv WHERE k = %s", (key,))
            row = cur.fetchone()
        return row[0] if row else value
    finally:
        conn.close()


_secret_cache: Optional[bytes] = None


def get_session_secret() -> bytes:
    """取会话签名密钥；不存在则生成并持久化。

    进程内缓存：每次 rerun 都查库既慢又放大连接抖动的影响。
    """
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    existing = get_kv("session_secret")
    if existing:
        _secret_cache = bytes.fromhex(existing)
        return _secret_cache
    generated = secrets.token_bytes(32).hex()
    final = set_kv_if_absent("session_secret", generated)
    _secret_cache = bytes.fromhex(final)
    return _secret_cache


# ============================================================
# 持仓账本（接口与 SQLiteStore 对齐）
# ============================================================
class PGStore:
    """Postgres 版持仓账本，方法签名与 SQLiteStore 保持一致，可直接替换。"""

    def __init__(self):
        ensure_schema()

    # -------- 读 --------
    def get_portfolio_positions(self, user_id: str = "") -> pd.DataFrame:
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM portfolio_positions WHERE user_id = %s "
                    "ORDER BY updated_at DESC, security_code",
                    (str(user_id),),
                )
                return _rows_to_df(cur)
        finally:
            conn.close()

    def get_portfolio_transactions(self, security_code: str = None, limit: int = 200,
                                   user_id: str = "") -> pd.DataFrame:
        conn = connect()
        try:
            sql = "SELECT * FROM portfolio_transactions WHERE user_id = %s"
            params: List[Any] = [str(user_id)]
            if security_code:
                sql += " AND security_code = %s"
                params.append(security_code)
            sql += " ORDER BY trade_date DESC, id DESC LIMIT %s"
            params.append(int(limit))
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return _rows_to_df(cur)
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
        conn = connect()
        try:
            assignments = [f"{key} = %s" for key in changes]
            vals = list(changes.values()) + [str(user_id), str(security_code)]
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE portfolio_positions SET {', '.join(assignments)}, updated_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS') "
                    "WHERE user_id = %s AND security_code = %s",
                    tuple(vals),
                )
                if cur.rowcount == 0:
                    raise ValueError("未找到要修改的持仓")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _rebuild_portfolio_position(self, conn, user_id: str, security_code: str) -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM portfolio_transactions WHERE user_id = %s AND security_code = %s ORDER BY id", (str(user_id), str(security_code)))
            rows = cur.fetchall(); cur.execute("SELECT * FROM portfolio_positions WHERE user_id = %s AND security_code = %s", (str(user_id), str(security_code)))
            old = cur.fetchone(); qty, cost_amount, opened_date, last_name = 0.0, 0.0, None, None
            for row in rows:
                side, q, price, fee = str(row[4]).upper(), float(row[5]), float(row[6]), float(row[7])
                if side == "BUY": qty += q; cost_amount += q * price + fee; opened_date = opened_date or row[1]
                elif side == "SELL":
                    if q > qty + 1e-8: raise ValueError(f"流水重算失败：{security_code} 卖出数量超过累计买入")
                    avg = cost_amount / qty if qty > 1e-8 else 0.0; qty -= q; cost_amount = qty * avg
                elif side == "ADJUST": qty = q
                last_name = row[3] or last_name
            cur.execute("DELETE FROM portfolio_positions WHERE user_id = %s AND security_code = %s", (str(user_id), str(security_code)))
            if qty <= 1e-8: return
            cur.execute("""INSERT INTO portfolio_positions
                (user_id, security_code, security_name, asset_type, sector_code, sector_name, quantity, avg_cost, opened_date, target_weight, stop_loss, note, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))""",
                (str(user_id), str(security_code), last_name or (old[2] if old else security_code), old[3] if old else "stock", old[4] if old else None, old[5] if old else None, qty, cost_amount / qty if qty else 0.0, opened_date or (old[8] if old else None), old[9] if old else None, old[10] if old else None, old[11] if old else None))

    def update_portfolio_transaction(self, transaction_id: int, user_id: str = "", **fields) -> None:
        allowed = {"trade_date", "security_code", "security_name", "side", "quantity", "price", "fee", "note"}; updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates: raise ValueError("没有可修改的流水字段")
        if "side" in updates and str(updates["side"]).upper() not in {"BUY", "SELL", "ADJUST"}: raise ValueError("操作必须是 BUY、SELL 或 ADJUST")
        if "quantity" in updates and float(updates["quantity"]) <= 0: raise ValueError("数量必须大于 0")
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT security_code FROM portfolio_transactions WHERE id = %s AND user_id = %s", (int(transaction_id), str(user_id))); before = cur.fetchone()
                if not before: raise ValueError("找不到这笔操作记录")
                old_code = before[0]; sets = ", ".join(f"{k} = %s" for k in updates); cur.execute(f"UPDATE portfolio_transactions SET {sets} WHERE id = %s AND user_id = %s", tuple(updates.values()) + (int(transaction_id), str(user_id)))
                new_code = updates.get("security_code", old_code); self._rebuild_portfolio_position(conn, user_id, old_code)
                if new_code != old_code: self._rebuild_portfolio_position(conn, user_id, new_code)
            conn.commit()
        except Exception: conn.rollback(); raise
        finally: conn.close()

    def delete_portfolio_transaction(self, transaction_id: int, user_id: str = "") -> None:
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT security_code FROM portfolio_transactions WHERE id = %s AND user_id = %s", (int(transaction_id), str(user_id))); row = cur.fetchone()
                if not row: raise ValueError("找不到这笔操作记录")
                cur.execute("DELETE FROM portfolio_transactions WHERE id = %s AND user_id = %s", (int(transaction_id), str(user_id))); self._rebuild_portfolio_position(conn, user_id, row[0])
            conn.commit()
        except Exception: conn.rollback(); raise
        finally: conn.close()

    # -------- 写 --------
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
        """原子记录成交并更新持仓，语义与 SQLite 版完全一致。"""
        side = str(side).upper()
        if side not in {"BUY", "SELL", "ADJUST"}:
            raise ValueError("side 必须是 BUY、SELL 或 ADJUST")
        quantity, price, fee = float(quantity), float(price), float(fee)
        if quantity <= 0 or price < 0 or fee < 0:
            raise ValueError("数量必须大于 0，价格和费用不能为负数")
        user_id = str(user_id)

        conn = connect()
        try:
            with conn.cursor() as cur:
                # 行级锁，避免并发下持仓被覆盖
                cur.execute(
                    "SELECT quantity, avg_cost FROM portfolio_positions "
                    "WHERE user_id = %s AND security_code = %s FOR UPDATE",
                    (user_id, security_code),
                )
                row = cur.fetchone()
                current_qty = float(row[0]) if row else 0.0
                current_cost = float(row[1]) if row else 0.0

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

                cur.execute(
                    """
                    INSERT INTO portfolio_transactions
                        (user_id, trade_date, security_code, security_name, side, quantity, price, fee, note)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (user_id, trade_date, security_code, security_name, side,
                     quantity, price, fee, note),
                )

                if new_qty <= 1e-8:
                    cur.execute(
                        "DELETE FROM portfolio_positions WHERE user_id = %s AND security_code = %s",
                        (user_id, security_code),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO portfolio_positions
                            (user_id, security_code, security_name, asset_type, sector_code, sector_name,
                             quantity, avg_cost, opened_date, target_weight, stop_loss, note, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))
                        ON CONFLICT (user_id, security_code) DO UPDATE SET
                            security_name = EXCLUDED.security_name,
                            asset_type    = EXCLUDED.asset_type,
                            sector_code   = COALESCE(EXCLUDED.sector_code, portfolio_positions.sector_code),
                            sector_name   = COALESCE(EXCLUDED.sector_name, portfolio_positions.sector_name),
                            quantity      = EXCLUDED.quantity,
                            avg_cost      = EXCLUDED.avg_cost,
                            target_weight = COALESCE(EXCLUDED.target_weight, portfolio_positions.target_weight),
                            stop_loss     = COALESCE(EXCLUDED.stop_loss, portfolio_positions.stop_loss),
                            note          = COALESCE(EXCLUDED.note, portfolio_positions.note),
                            updated_at    = to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
                        """,
                        (user_id, security_code, security_name, asset_type, sector_code, sector_name,
                         new_qty, new_cost, trade_date, target_weight, stop_loss, note),
                    )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

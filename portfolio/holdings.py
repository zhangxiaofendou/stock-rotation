"""持仓账本领域服务。

持仓是用户真实交易事实：成交记录追加保存，当前持仓由 SQLiteStore 原子更新。
本模块不计算九宫格状态、不发出买卖指令，供持仓页面、组合风险和后续建议引擎复用。
"""

from datetime import date
from typing import Optional

import pandas as pd

from data.storage import pg_store
from data.storage.sqlite_store import SQLiteStore


_UNSET = object()


def _default_store():
    """配置了 DATABASE_URL 时用云 Postgres（重部署不丢），否则用本地 SQLite。"""
    try:
        if pg_store.is_enabled():
            return pg_store.PGStore()
    except Exception:
        pass
    return SQLiteStore()


class PortfolioHoldings:
    """真实持仓与操作日志的应用服务。

    user_id 标识持仓所有者；传入后所有读写均按该用户隔离，确保不同使用者
    的仓位互不串读、互不可见。
    """

    def __init__(self, store=None, user_id: str = ""):
        self.store = store if store is not None else _default_store()
        self.user_id = str(user_id)

    def record_trade(
        self,
        security_code: str,
        security_name: str,
        side: str,
        quantity: float,
        price: float,
        trade_date: str = None,
        fee: float = 0.0,
        note: str = None,
        asset_type: str = "stock",
        sector_code: str = None,
        sector_name: str = None,
        target_weight: float = None,
        stop_loss: float = None,
    ) -> None:
        """记录一笔用户实际操作，并同步当前头寸。"""
        if not security_code or not security_name:
            raise ValueError("证券代码和证券名称不能为空")
        if not self.user_id:
            raise ValueError("持仓操作必须指定 user_id（当前用户未登录？）")
        self.store.record_portfolio_transaction(
            trade_date=trade_date or date.today().isoformat(),
            security_code=str(security_code).strip(),
            security_name=str(security_name).strip(),
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            note=note.strip() if note else None,
            asset_type=asset_type,
            sector_code=sector_code or None,
            sector_name=sector_name or None,
            target_weight=target_weight,
            stop_loss=stop_loss,
            user_id=self.user_id,
        )

    def update_metadata(self, security_code: str, **fields) -> None:
        """只修改持仓属性，不新增交易、不改变数量和平均成本。"""
        if not self.user_id:
            raise ValueError("持仓操作必须指定 user_id（当前用户未登录？）")
        allowed = {"security_name", "asset_type", "sector_code", "sector_name", "quantity", "avg_cost", "target_weight", "stop_loss", "note"}
        changes = {k: v for k, v in fields.items() if k in allowed}
        if not changes:
            raise ValueError("没有可修改的持仓属性")
        self.store.update_portfolio_metadata(user_id=self.user_id, security_code=str(security_code).strip(), **changes)

    def positions(self) -> pd.DataFrame:
        """返回当前用户的持仓，追加成本金额列以便页面汇总。"""
        df = self.store.get_portfolio_positions(user_id=self.user_id)
        if df.empty:
            return df
        df = df.copy()
        df["cost_amount"] = df["quantity"] * df["avg_cost"]
        return df

    def transactions(self, security_code: str = None, limit: int = 200) -> pd.DataFrame:
        """返回当前用户的成交/调账日志。"""
        return self.store.get_portfolio_transactions(
            security_code=security_code, limit=limit, user_id=self.user_id
        )

    def summary(self) -> dict:
        """仅按成本统计当前账本概览；市值、浮盈亏需由行情层后续补全。"""
        positions = self.positions()
        if positions.empty:
            return {
                "position_count": 0,
                "total_cost": 0.0,
                "sector_count": 0,
                "largest_position_cost": 0.0,
            }
        total_cost = float(positions["cost_amount"].sum())
        sector_count = int(positions["sector_name"].replace("", pd.NA).dropna().nunique())
        largest = float(positions["cost_amount"].max())
        return {
            "position_count": int(len(positions)),
            "total_cost": total_cost,
            "sector_count": sector_count,
            "largest_position_cost": largest,
        }

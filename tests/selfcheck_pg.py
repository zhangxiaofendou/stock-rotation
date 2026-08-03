"""Postgres 持久化层无头自检（不需要真实数据库）。

用桩连接替换驱动，校验：
1. 未配置 DATABASE_URL 时自动回退，不影响本地 SQLite 行为；
2. 连接串归一化（postgres:// → postgresql://，自动补 sslmode）；
3. 每条 SQL 的 %s 占位符数量与参数个数严格一致（防止运行期 ProgrammingError）；
4. 买入加权成本、卖出减仓、清仓删除等业务语义与 SQLite 版一致；
5. 所有持仓读写都带 user_id 过滤（多用户隔离不被绕过）。

运行：python tests/selfcheck_pg.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS - " if cond else "FAIL - ") + name + (f"  [{detail}]" if detail and not cond else ""))


# ------------------------------------------------------------
# 桩：模拟 DB-API 连接，记录执行过的 SQL
# ------------------------------------------------------------
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        params = params or ()
        n_ph = sql.count("%s")
        if n_ph != len(params):
            raise AssertionError(
                f"占位符({n_ph}) 与参数({len(params)}) 不匹配：{sql.strip()[:120]}"
            )
        self.conn.log.append((sql.strip(), tuple(params)))
        low = sql.lower()
        self.description = None
        self._rows = []
        self.rowcount = 1
        if "select quantity, avg_cost from portfolio_positions" in low:
            pos = self.conn.state.get("pos")
            self._rows = [pos] if pos else []
        elif "select 1" in low:
            self._rows = [(1,)]
        elif low.startswith("select * from portfolio_positions"):
            self.description = [("user_id",), ("security_code",), ("quantity",), ("avg_cost",)]
            self._rows = list(self.conn.state.get("rows", []))
        elif low.startswith("select * from portfolio_transactions"):
            self.description = [("id",), ("user_id",), ("security_code",)]
            self._rows = list(self.conn.state.get("tx", []))
        elif low.startswith("select trade_date, security_name, side, quantity, price, fee from portfolio_transactions"):
            # _rebuild_portfolio_position 列名访问，模拟 PG 按列顺序返回
            self._rows = list(self.conn.state.get("rebuild_tx", []))
        elif low.startswith("select security_name, asset_type, sector_code, sector_name, opened_date, target_weight, stop_loss, note from portfolio_positions"):
            self._rows = list(self.conn.state.get("rebuild_old", []))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, state):
        self.state = state
        self.log = []
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def main():
    os.environ.pop("DATABASE_URL", None)
    import data.storage.pg_store as pg

    # ---- 1. 未配置时回退 ----
    check("未配置 DATABASE_URL 时 is_enabled() 为 False", pg.is_enabled() is False)

    from portfolio.holdings import _default_store
    from data.storage.sqlite_store import SQLiteStore
    check("未配置时持仓服务回退到 SQLiteStore", isinstance(_default_store(), SQLiteStore))

    # ---- 2. 连接串归一化 ----
    u1 = pg._normalize_url("postgres://u:p@h:5432/db")
    check("postgres:// 被规范为 postgresql://", u1.startswith("postgresql://"), u1)
    check("自动补 sslmode=require", "sslmode=require" in u1, u1)
    u2 = pg._normalize_url("postgresql://u:p@h:5432/db?sslmode=disable")
    check("已有 sslmode 时不重复追加", u2.count("sslmode=") == 1, u2)

    # ---- 3. 配置后启用（用桩驱动接管连接）----
    os.environ["DATABASE_URL"] = "postgresql://u:p@localhost:5432/testdb"
    pg._DRIVER = "fake"
    state = {"pos": None, "rows": [], "tx": []}
    conns = []

    def fake_connect(url):
        c = FakeConn(state)
        conns.append(c)
        return c

    pg._connect = fake_connect
    pg._schema_ready = False
    check("配置 DATABASE_URL 后 is_enabled() 为 True", pg.is_enabled() is True)

    # 建表 SQL 可执行
    pg.ensure_schema(force=True)
    check("建表 SQL 执行成功（幂等 schema）", any("create table" in s.lower() for s, _ in conns[0].log))

    # 连通性自检
    ok, msg = pg.healthcheck()
    check("healthcheck 在可连通时返回 True", ok, msg)

    # ---- 4. 业务语义：买入建仓 ----
    store = pg.PGStore()
    state["pos"] = None
    store.record_portfolio_transaction(
        trade_date="2026-08-02", security_code="600000", security_name="浦发银行",
        side="BUY", quantity=1000, price=10.0, fee=5.0, user_id="alice",
    )
    last = conns[-1]
    ins = [(s, p) for s, p in last.log if "insert into portfolio_positions" in s.lower()]
    check("买入后写入持仓表", len(ins) == 1)
    if ins:
        params = ins[0][1]
        # 参数顺序：user_id, code, name, asset_type, sector_code, sector_name, qty, avg_cost, ...
        qty, cost = params[6], params[7]
        check("买入数量正确（1000）", abs(qty - 1000) < 1e-9, str(qty))
        check("买入均价含手续费（(1000*10+5)/1000=10.005）", abs(cost - 10.005) < 1e-9, str(cost))
        check("持仓写入带 user_id=alice", params[0] == "alice", str(params[0]))
    check("买入事务已提交", last.committed and not last.rolled_back)

    # ---- 5. 业务语义：加仓摊薄成本 ----
    state["pos"] = (1000.0, 10.0)
    store.record_portfolio_transaction(
        trade_date="2026-08-02", security_code="600000", security_name="浦发银行",
        side="BUY", quantity=1000, price=12.0, fee=0.0, user_id="alice",
    )
    ins = [(s, p) for s, p in conns[-1].log if "insert into portfolio_positions" in s.lower()]
    cost2 = ins[0][1][7]
    check("加仓后为加权平均成本（(10000+12000)/2000=11）", abs(cost2 - 11.0) < 1e-9, str(cost2))

    # ---- 6. 业务语义：卖出减仓不改成本 ----
    state["pos"] = (2000.0, 11.0)
    store.record_portfolio_transaction(
        trade_date="2026-08-02", security_code="600000", security_name="浦发银行",
        side="SELL", quantity=500, price=13.0, user_id="alice",
    )
    ins = [(s, p) for s, p in conns[-1].log if "insert into portfolio_positions" in s.lower()]
    check("卖出后数量减少为 1500", abs(ins[0][1][6] - 1500) < 1e-9, str(ins[0][1][6]))
    check("卖出不改变持仓成本（仍为 11）", abs(ins[0][1][7] - 11.0) < 1e-9, str(ins[0][1][7]))

    # ---- 7. 清仓删除 ----
    state["pos"] = (500.0, 11.0)
    store.record_portfolio_transaction(
        trade_date="2026-08-02", security_code="600000", security_name="浦发银行",
        side="SELL", quantity=500, price=13.0, user_id="alice",
    )
    dels = [s for s, _ in conns[-1].log if s.lower().startswith("delete from portfolio_positions")]
    check("全部卖出后删除持仓行", len(dels) == 1)

    # ---- 8. 超卖被拒绝 + 回滚 ----
    state["pos"] = (100.0, 11.0)
    raised = False
    try:
        store.record_portfolio_transaction(
            trade_date="2026-08-02", security_code="600000", security_name="浦发银行",
            side="SELL", quantity=1000, price=13.0, user_id="alice",
        )
    except ValueError:
        raised = True
    check("卖出超过持仓被拒绝", raised)
    check("异常时事务回滚", conns[-1].rolled_back and not conns[-1].committed)

    # ---- 9. 非法 side / 负数 ----
    for bad, desc in [
        (dict(side="GIVE", quantity=1, price=1), "非法 side 被拒绝"),
        (dict(side="BUY", quantity=0, price=1), "数量为 0 被拒绝"),
        (dict(side="BUY", quantity=1, price=-1), "负价格被拒绝"),
    ]:
        r = False
        try:
            store.record_portfolio_transaction(
                trade_date="2026-08-02", security_code="600000", security_name="X",
                user_id="alice", **bad,
            )
        except ValueError:
            r = True
        check(desc, r)

    # ---- 10. 多用户隔离：读取必须带 user_id 条件 ----
    state["rows"] = []
    store.get_portfolio_positions(user_id="bob")
    sel = [(s, p) for s, p in conns[-1].log if s.lower().startswith("select * from portfolio_positions")]
    check("查询持仓带 user_id 过滤", sel and "where user_id = %s" in sel[0][0].lower())
    check("查询持仓传入的是 bob", sel and sel[0][1][0] == "bob")

    state["tx"] = []
    store.get_portfolio_transactions(user_id="bob", security_code="600000", limit=50)
    sel = [(s, p) for s, p in conns[-1].log if s.lower().startswith("select * from portfolio_transactions")]
    check("查询流水带 user_id 过滤", sel and "where user_id = %s" in sel[0][0].lower())
    check("查询流水参数为 (bob, 600000, 50)", sel and sel[0][1] == ("bob", "600000", 50), str(sel and sel[0][1]))

    # ---- 11. 账号写入云库 ----
    pg.invalidate_credentials_cache()
    created = pg.add_user("carol", {"salt": "aa", "hash": "bb", "iter": 200000})
    check("add_user 执行插入并返回 True", created is True)
    sql_ins = [s for s, _ in conns[-1].log if "insert into app_users" in s.lower()]
    check("插入 app_users 使用 ON CONFLICT DO NOTHING（防重名）",
          sql_ins and "on conflict (username) do nothing" in sql_ins[0].lower())

    # ---- 12. 会话密钥持久化 ----
    secret = pg.get_session_secret()
    check("会话密钥为 32 字节", isinstance(secret, bytes) and len(secret) == 32, str(len(secret)))

    # ---- 12.5 流水重建（修复 PG 列索引错位导致 float('BUY') 的 bug） ----
    # 模拟 PG 按列顺序返回，列序：trade_date, security_name, side, quantity, price, fee
    state["rebuild_tx"] = [
        ("2026-06-16", "旅游ETF富国", "BUY", 6200.0, 0.597, 5.0),
        ("2026-07-30", "旅游ETF富国", "BUY", 18400.0, 1.012, 9.0),
    ]
    state["rebuild_old"] = []  # 之前没有持仓聚合行
    # 用一个新 conn，避免上一次 INSERT 影响
    state2 = {"rebuild_tx": state["rebuild_tx"], "rebuild_old": state["rebuild_old"]}
    conn2 = FakeConn(state2)
    conns.append(conn2)
    raised = None
    try:
        store._rebuild_portfolio_position(conn2, "alice", "159766")
    except Exception as e:
        raised = repr(e)
    check("_rebuild_portfolio_position 不再触发 float('BUY')", raised is None, raised)
    # 重建后应发出聚合 INSERT，参数里 quantity 和 avg_cost 应等于两笔合计
    ins = [(s, p) for s, p in conn2.log if "insert into portfolio_positions" in s.lower()]
    check("重建后写入持仓行", len(ins) == 1)
    if ins:
        params = ins[0][1]
        # 列序：user_id, code, name, asset_type, sector_code, sector_name, qty, avg_cost, ...
        qty, cost = params[6], params[7]
        check("重建后持仓数量 = 6200+18400 = 24600",
              abs(qty - 24600.0) < 1e-6, str(qty))
        # avg_cost = (6200*0.597 + 5 + 18400*1.012 + 9) / 24600
        expected = (6200 * 0.597 + 5.0 + 18400 * 1.012 + 9.0) / 24600.0
        check("重建后均价按加权金额计算", abs(cost - expected) < 1e-6,
              f"got {cost} vs expected {expected}")

    # ---- 13. 云库不可用时不误放行 ----
    def boom(url):
        raise RuntimeError("network is unreachable")

    pg._connect = boom
    pg.invalidate_credentials_cache()
    import importlib
    import auth as auth_mod
    importlib.reload(auth_mod)
    creds = auth_mod._load_creds()
    check("云库不可用时凭证为空（登录失败而非误放行）", creds == {"users": {}}, str(creds))
    ok2, msg2 = pg.healthcheck()
    check("healthcheck 在不可达时返回 False 且给出 IPv6/pooler 提示",
          (not ok2) and "pooler" in msg2, msg2)

    os.environ.pop("DATABASE_URL", None)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=== 自检汇总  结果：{passed} 通过 / {total - passed} 失败 ===")
    if passed == total:
        print("✅ Postgres 持久化层逻辑全部通过。")
        return 0
    print("❌ 存在失败项：")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"   - {n} {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

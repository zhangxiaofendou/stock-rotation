import os, sys, tempfile
sys.path.insert(0, ".")
os.environ["PERSISTENT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="meta_")
from data.storage.sqlite_store import SQLiteStore

store = SQLiteStore()
store.record_portfolio_transaction("2026-08-02", "159766", "旅游ETF富国", "BUY", 5900, 0.565, user_id="alice", asset_type="stock")
before = store.get_portfolio_positions(user_id="alice").iloc[0]
store.update_portfolio_metadata("alice", "159766", quantity=5900, avg_cost=0.565, asset_type="etf", sector_name="旅游")
after = store.get_portfolio_positions(user_id="alice").iloc[0]
assert float(before["quantity"]) == 5900
assert float(after["quantity"]) == 5900
assert after["asset_type"] == "etf" and after["sector_name"] == "旅游"
assert len(store.get_portfolio_transactions(user_id="alice")) == 1
print("结果：4/4 通过")

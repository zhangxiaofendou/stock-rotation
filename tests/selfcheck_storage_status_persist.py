"""验证 storage_status() 在持久卷生效时不再误报「数据不安全」。

防假绿：
- 旧实现：is_enabled() 为 False 时无条件返回 safe=False（红字误报），
  即使用 /mnt/workspace 持久卷接管了数据也会吓用户。
- 新实现：持久卷生效（/mnt/workspace 或 PERSISTENT_STORAGE_DIR）时返回
  safe=True, backend="persist-volume"，界面显示绿色「持久卷已接管」。

测试直接构造两种 config.settings 状态验证，并断言「持久卷下不报红」。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import data.storage.pg_store as pg

# 去掉可能存在的真实 DATABASE_URL，确保走「未配置云库」分支
os.environ.pop("DATABASE_URL", None)


def _patch_settings(persist_dir: str, source: str):
    """把 config.settings 的 PERSIST_DIR / PERSIST_DIR_SOURCE 替换成测试值。"""
    import config.settings as s
    s.PERSIST_DIR = __import__("pathlib").Path(persist_dir)
    s.PERSIST_DIR_SOURCE = source
    # pg_store 内部 `from config.settings import PERSIST_DIR, PERSIST_DIR_SOURCE`
    # 是局部导入，每次都重新读模块属性，所以直接改模块属性即可生效。


def test_persist_volume_mnt_workspace_is_safe():
    _patch_settings("/mnt/workspace", "/mnt/workspace")
    st = pg.storage_status()
    assert st["safe"] is True, f"持久卷应判安全, 实际: {st}"
    assert st["backend"] == "persist-volume", f"backend 应为 persist-volume, 实际: {st}"
    assert "无需配置云库" in st["detail"], f"detail 应说明无需云库, 实际: {st['detail']}"


def test_persist_volume_env_var_is_safe():
    d = tempfile.mkdtemp(prefix="persist_")
    _patch_settings(d, "PERSISTENT_STORAGE_DIR")
    st = pg.storage_status()
    assert st["safe"] is True, f"显式持久目录应判安全, 实际: {st}"
    assert st["backend"] == "persist-volume", f"backend 应为 persist-volume, 实际: {st}"


def test_local_persist_is_unsafe():
    """本地 .persist（临时层）无持久卷时，才应报不安全（红字）。"""
    d = tempfile.mkdtemp(prefix="local_persist_")
    _patch_settings(d, "PROJECT_ROOT/.persist")
    st = pg.storage_status()
    assert st["safe"] is False, f"本地临时层应判不安全, 实际: {st}"
    assert st["backend"] == "sqlite", f"backend 应为 sqlite, 实际: {st}"


# ---------- 防假绿：旧实现应 FAIL ----------
def _old_storage_status():
    """复刻旧实现（is_enabled False 即无条件 safe=False）。"""
    if not pg.is_enabled():
        return {"backend": "sqlite", "safe": False,
                "detail": "未配置 DATABASE_URL，使用本地临时存储，重部署/Reboot 会清空数据。"}
    return {"backend": "postgres", "safe": True, "detail": "ok"}


def test_old_impl_fails_on_persist_volume():
    """证明旧实现在持久卷下误报——这正是我们要修的 bug。"""
    _patch_settings("/mnt/workspace", "/mnt/workspace")
    old = _old_storage_status()
    # 旧实现永远 safe=False，但持久卷下应为 True → 旧逻辑确实 FAIL
    assert old["safe"] is False, "旧实现在持久卷下误报安全=False（bug 复现）"
    # 而新实现应判安全
    new = pg.storage_status()
    assert new["safe"] is True, "新实现应修正为安全=True"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
            ok += 1
        except AssertionError as e:
            print(f"✗ {t.__name__}: {e}")
        except Exception as e:
            print(f"! {t.__name__}: {e}")
    print(f"\n{ok}/{len(tests)} passed")
    sys.exit(0 if ok == len(tests) else 1)

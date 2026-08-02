"""无头自检：mock Streamlit，真实验证多用户认证守卫逻辑。

运行：python tests/selfcheck_auth.py
前置：项目根目录（本文件位于 <root>/tests/ 下，自动定位根目录）。

验证点：
1. 未登录 query_params 无 token -> guard() 返回 None（拦截，不渲染业务页）
2. 注册 -> 登录 -> 生成 token
3. token 可被 get_current_user 正确识别
4. 篡改 token 被拒绝（返回 None）
5. 重复用户名注册被拒绝
6. 错误密码登录被拒绝
7. 已登录时 guard() 放行并返回用户名
8. set_session 传「令牌」或「用户名」均能建立有效会话（历史 bug 回归）
9. clear_session 后会话彻底失效
10. 会话密钥进程内缓存稳定，不会导致既有令牌失效
"""
import os
import sys
import json
import tempfile
import unittest.mock as mock

# 1) 把持久化目录指向 temp，避免污染真实 .persist
TMP = tempfile.mkdtemp(prefix="auth_selfcheck_")
os.environ["PERSISTENT_STORAGE_DIR"] = TMP

# 2) 注入 mock 的 streamlit，模拟 query_params 字典行为
class QueryParamsMock:
    def __init__(self):
        self._d = {}
    def get(self, k, default=None):
        return self._d.get(k, default)
    def __setitem__(self, k, v):
        self._d[k] = v
    def __delitem__(self, k):
        self._d.pop(k, None)
    def __contains__(self, k):
        return k in self._d

qp = QueryParamsMock()
st_mock = mock.MagicMock()
st_mock.query_params = qp
st_mock.session_state = {}
sys.modules["streamlit"] = st_mock

# 3) import 项目模块（项目根 = tests/ 的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import config.settings as settings  # noqa: E402
import auth  # noqa: E402


def reset_session():
    """彻底清空浏览器侧会话痕迹（url 参数 + session_state）。"""
    qp._d.clear()
    st_mock.session_state.clear()


results = []
def check(name, cond):
    results.append((name, cond))
    print(("PASS" if cond else "FAIL"), "-", name)

# ---- 测试 1：未登录守卫拦截 ----
reset_session()
user = auth.guard()
check("未登录时 guard() 返回 None（拦截业务页）", user is None)

# ---- 测试 2：注册 ----
ok, msg = auth.register("alice", "secret123")
check("注册 alice 成功", ok and msg == "注册成功")
cred_path = str(settings.CREDENTIALS_PATH)
check("凭证文件已写入磁盘", os.path.exists(cred_path))
with open(cred_path, encoding="utf-8") as f:
    creds = json.load(f)
check("凭证含 alice 且密码为哈希（非明文）",
      "alice" in creds["users"] and "hash" in creds["users"]["alice"]
      and creds["users"]["alice"]["hash"] != "secret123")

# ---- 测试 3：重复注册被拒 ----
ok2, _ = auth.register("alice", "another456")
check("重复用户名注册被拒绝", not ok2)

# ---- 测试 4：登录生成 token，并由 get_current_user 识别 ----
ok3, token = auth.login("alice", "secret123")
check("登录成功并返回 token", ok3 and isinstance(token, str) and token)
reset_session()
qp["token"] = token
check("get_current_user 用 token 识别 alice", auth.get_current_user() == "alice")

# ---- 测试 5：密码错误被拒 ----
ok4, _ = auth.login("alice", "wrongpass")
check("错误密码登录被拒绝", not ok4)

# ---- 测试 6：篡改 token 被拒 ----
tampered = token[:-4] + "XXXX"
reset_session()
qp["token"] = tampered
check("篡改 token 被拒绝（get_current_user 返回 None）", auth.get_current_user() is None)

# ---- 测试 7：guard 已登录时返回用户名（不再拦截） ----
reset_session()
qp["token"] = token
check("已登录时 guard() 返回用户名（放行）", auth.guard() == "alice")

# ---- 测试 8：会话密钥文件生成 ----
check("会话密钥文件已生成", os.path.exists(str(settings.SESSION_SECRET_PATH)))

# ---- 测试 9：部署版本角标可获取 ----
tag = auth.deploy_tag()
check("部署版本角标可获取且非空", isinstance(tag, str) and bool(tag))

# ============================================================
# 回归测试：历史 bug —— set_session 收到 login() 返回的「令牌」时，
# 曾把令牌当用户名二次签名，产生「用户名=令牌串」的废令牌，
# 表现为「点注册并登录后又被弹回登录界面」。
# ============================================================
reset_session()
ok5, tok5 = auth.login("alice", "secret123")
auth.set_session(tok5)  # 传令牌
check("回归：set_session(令牌) 后 guard() 放行", auth.guard() == "alice")
check("回归：set_session(令牌) 未二次签名（url 中令牌与原令牌一致）",
      qp.get("token") == tok5)

reset_session()
auth.set_session("alice")  # 传用户名
check("set_session(用户名) 后 guard() 放行", auth.guard() == "alice")
check("set_session(用户名) 写入了合法令牌",
      auth._verify_token(qp.get("token")) == "alice")

# ---- 回归：仅靠 url 令牌（无 session_state）也必须能恢复登录态 ----
tok_only = qp.get("token")
st_mock.session_state.clear()
check("仅凭 url 令牌可恢复登录态（刷新页面不掉线）",
      auth.get_current_user() == "alice")

# ---- 退出登录后会话彻底失效 ----
auth.clear_session()
check("clear_session 后 get_current_user 返回 None", auth.get_current_user() is None)
check("clear_session 后 url 中已无 token", qp.get("token") is None)
check("clear_session 后 guard() 重新拦截", auth.guard() is None)

# ---- 密钥缓存稳定性：多次取密钥必须一致，否则既有令牌会集体失效 ----
s1 = auth._get_secret()
s2 = auth._get_secret()
check("会话密钥进程内稳定（两次取值一致）", s1 == s2)
reset_session()
qp["token"] = tok_only
check("密钥稳定后旧令牌仍然有效（不会被莫名踢下线）",
      auth.get_current_user() == "alice")

# ---- 汇总 ----
passed = sum(1 for _, c in results if c)
print(f"\n=== 自检汇总: {passed}/{len(results)} 通过 ===")
if passed == len(results):
    print("✅ 认证逻辑全部通过，守卫不会失效、无绕过风险。")
else:
    print("❌ 存在失败项，需修复。")
sys.exit(0 if passed == len(results) else 1)

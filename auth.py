"""轻量多用户认证（零第三方依赖）。

设计：
- 密码用 PBKDF2-HMAC-SHA256（20 万次迭代）+ 随机盐哈希存储，等价 bcrypt 强度。
- 会话用「签名令牌」存放于 st.query_params["token"]，刷新后免登录；
  令牌 = base64(用户名|HMAC(会话密钥, 用户名))，无法被篡改或伪造。
- 凭证与会话密钥均落在持久化目录（config.settings.PERSIST_DIR），
  跨 Streamlit Cloud 重部署保留。

不使用 streamlit-authenticator：其安装需联网且云端依赖不可控，
本实现仅用标准库，部署零新增依赖、零风险。
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
from typing import Optional, Tuple

import streamlit as st

from config.settings import CREDENTIALS_PATH, SESSION_SECRET_PATH
from data.storage import pg_store

PBKDF2_ITER = 200_000


# ============================================================
# 凭证存储
# ------------------------------------------------------------
# 配置了 DATABASE_URL（Supabase / Neon 等云 Postgres）时账号写入云数据库，
# 重部署不丢；否则回退到本地 JSON，保持本地开发行为不变。
# ============================================================
def _use_pg() -> bool:
    try:
        return pg_store.is_enabled()
    except Exception:
        return False


def _load_creds() -> dict:
    if _use_pg():
        try:
            return pg_store.load_credentials()
        except Exception:
            # 云库暂时不可用时不静默放行，返回空集合让登录失败而非误放行
            return {"users": {}}
    path = str(CREDENTIALS_PATH)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "users" in data:
                return data
        except Exception:
            pass
    return {"users": {}}


def _save_creds(creds: dict) -> None:
    path = str(CREDENTIALS_PATH)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ============================================================
# 密码哈希
# ============================================================
def hash_password(password: str) -> dict:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITER)
    return {"salt": salt.hex(), "hash": dk.hex(), "iter": PBKDF2_ITER}


def verify_password(password: str, rec: dict) -> bool:
    if not isinstance(rec, dict) or "salt" not in rec or "hash" not in rec:
        return False
    try:
        salt = bytes.fromhex(rec["salt"])
        it = int(rec.get("iter", PBKDF2_ITER))
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, it)
        return hmac.compare_digest(dk.hex(), rec["hash"])
    except Exception:
        return False


# ============================================================
# 会话签名
# ============================================================
_secret_cache: Optional[bytes] = None


def _get_secret() -> bytes:
    """取会话签名密钥（进程内缓存）。

    必须缓存：否则每次 rerun 都要打一次数据库，且云库偶发抖动时会退回本地随机密钥，
    导致此前签发的所有令牌集体失效、用户被莫名踢回登录页。
    """
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    if _use_pg():
        try:
            _secret_cache = pg_store.get_session_secret()
            return _secret_cache
        except Exception:
            pass  # 云库不可用时退回本地文件，至少不阻断登录界面渲染
    path = str(SESSION_SECRET_PATH)
    if os.path.exists(path):
        with open(path, "rb") as f:
            _secret_cache = f.read()
            return _secret_cache
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    s = secrets.token_bytes(32)
    with open(path, "wb") as f:
        f.write(s)
    _secret_cache = s
    return s


def reset_secret_cache() -> None:
    """清空密钥缓存（仅测试用）。"""
    global _secret_cache
    _secret_cache = None


def _make_token(username: str) -> str:
    secret = _get_secret()
    sig = hmac.new(secret, username.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{username}|{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def _verify_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        username, sig = raw.split("|", 1)
        secret = _get_secret()
        expected = hmac.new(secret, username.encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            # 账号被删除后令牌失效
            if username in _load_creds().get("users", {}):
                # 额外做整令牌回放校验，杜绝 base64 静默丢弃尾随字符导致的弱校验
                if hmac.compare_digest(_make_token(username), token):
                    return username
    except Exception:
        pass
    return None


# ============================================================
# 公开 API
# ============================================================
def register(username: str, password: str) -> Tuple[bool, str]:
    username = (username or "").strip()
    if not username:
        return False, "用户名不能为空"
    if not password or len(password) < 6:
        return False, "密码至少 6 位"
    rec = hash_password(password)
    if _use_pg():
        try:
            created = pg_store.add_user(username, rec)
        except Exception as e:
            return False, f"云数据库写入失败：{e}"
        if not created:
            return False, "该用户名已存在"
        return True, "注册成功"
    creds = _load_creds()
    if username in creds["users"]:
        return False, "该用户名已存在"
    creds["users"][username] = rec
    _save_creds(creds)
    return True, "注册成功"


def login(username: str, password: str) -> Tuple[bool, str]:
    username = (username or "").strip()
    creds = _load_creds()
    rec = creds.get("users", {}).get(username)
    if not rec or not verify_password(password or "", rec):
        return False, "用户名或密码错误"
    return True, _make_token(username)


_SESSION_KEY = "auth_user"


def get_current_user() -> Optional[str]:
    """返回当前登录用户名。

    优先读 session_state：避免每次 rerun 都做一次令牌校验 + 查库，
    同时规避 query_params 在表单提交后写入延迟导致的「登录后又被弹回」。
    """
    cached = st.session_state.get(_SESSION_KEY)
    if cached:
        return str(cached)
    token = st.query_params.get("token")
    user = _verify_token(token) if token else None
    if user:
        st.session_state[_SESSION_KEY] = user
    return user


def set_session(username_or_token: str) -> None:
    """建立登录会话。

    兼容传入「用户名」或「已签名令牌」两种形态：调用方曾把 login() 返回的令牌
    直接传进来，若无脑再签一次名会得到「用户名=令牌串」的废令牌，
    校验时查无此人从而被弹回登录页。这里统一归一化，杜绝该类调用错误。
    """
    value = str(username_or_token or "")
    if not value:
        return
    resolved = _verify_token(value)  # 传进来的本身就是合法令牌时返回其用户名
    if resolved:
        username, token = resolved, value
    else:
        username, token = value, _make_token(value)
    st.session_state[_SESSION_KEY] = username
    st.query_params["token"] = token


def clear_session() -> None:
    try:
        st.session_state.pop(_SESSION_KEY, None)
    except Exception:
        pass
    if "token" in st.query_params:
        del st.query_params["token"]


# ============================================================
# 登录 / 注册界面
# ============================================================
def deploy_tag() -> str:
    """返回当前部署的代码版本（git 短哈希），便于一眼确认 Cloud 是否同步到最新。"""
    try:
        root = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=3,
        )
        return (out.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _render_auth() -> None:
    st.title("🔐 登录 / 注册")
    st.caption("不同使用者使用独立账号，持仓互不干扰。密码本地哈希存储，不落明文。")
    st.caption(f"🔖 部署版本：{deploy_tag()}")

    # 存储后端状态：让「数据会不会丢」一眼可见
    if _use_pg():
        ok, msg = pg_store.healthcheck()
        if ok:
            st.success("💾 账号与持仓已接入云数据库，重新部署不会丢失。", icon="✅")
        else:
            st.error(f"💾 云数据库连接异常，账号无法保存：{msg}")
    else:
        st.warning(
            "💾 当前使用本地临时存储，Streamlit Cloud 重新部署后账号与持仓会被清空。"
            "请在后台 Secrets 中配置 DATABASE_URL 接入云数据库。",
            icon="⚠️",
        )

    # 首次使用提示
    try:
        has_user = bool(_load_creds().get("users"))
    except Exception:
        has_user = True
    if not has_user:
        st.info("👋 首次使用请先切换到「注册」页创建账号。", icon="ℹ️")

    tabs = st.tabs(["登录", "注册"])
    with tabs[0]:
        with st.form("auth_login"):
            u = st.text_input("用户名", key="login_u")
            p = st.text_input("密码", type="password", key="login_p")
            submitted = st.form_submit_button("登录")
        if submitted:
            try:
                ok, payload = login(u, p)
                if ok:
                    set_session((u or "").strip())  # 传用户名，非令牌
                    st.rerun()
                else:
                    st.error(payload)
            except Exception as e:
                st.error(f"登录失败：{e}")
                st.exception(e)
    with tabs[1]:
        with st.form("auth_register"):
            u = st.text_input("用户名（自定义）", key="reg_u")
            p = st.text_input("密码（至少 6 位）", type="password", key="reg_p")
            p2 = st.text_input("确认密码", type="password", key="reg_p2")
            submitted = st.form_submit_button("注册并登录")
        if submitted:
            try:
                if p != p2:
                    st.error("两次输入的密码不一致")
                else:
                    ok, msg = register(u, p)
                    if ok:
                        ok2, payload = login(u, p)
                        if ok2:
                            set_session((u or "").strip())  # 传用户名，非令牌
                            st.rerun()
                        else:
                            st.error(payload)
                    else:
                        st.error(msg)
            except Exception as e:
                st.error(f"注册失败：{e}")
                st.exception(e)


def guard() -> Optional[str]:
    """返回已登录用户名；未登录则渲染登录/注册界面并返回 None。

    在 main() 中作为守卫调用：未登录时只显示登录界面，不渲染任何业务页面。
    """
    user = get_current_user()
    if user:
        return user
    _render_auth()
    return None


def logout_control() -> None:
    """在侧边栏渲染退出登录按钮。"""
    if st.sidebar.button("🚪 退出登录", key="auth_logout"):
        clear_session()
        st.rerun()

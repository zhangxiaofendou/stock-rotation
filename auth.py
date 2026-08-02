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

PBKDF2_ITER = 200_000


# ============================================================
# 凭证存储
# ============================================================
def _load_creds() -> dict:
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
def _get_secret() -> bytes:
    path = str(SESSION_SECRET_PATH)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    s = secrets.token_bytes(32)
    with open(path, "wb") as f:
        f.write(s)
    return s


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
    creds = _load_creds()
    if username in creds["users"]:
        return False, "该用户名已存在"
    creds["users"][username] = hash_password(password)
    _save_creds(creds)
    return True, "注册成功"


def login(username: str, password: str) -> Tuple[bool, str]:
    username = (username or "").strip()
    creds = _load_creds()
    rec = creds.get("users", {}).get(username)
    if not rec or not verify_password(password or "", rec):
        return False, "用户名或密码错误"
    return True, _make_token(username)


def get_current_user() -> Optional[str]:
    token = st.query_params.get("token")
    return _verify_token(token) if token else None


def set_session(username: str) -> None:
    st.query_params["token"] = _make_token(username)


def clear_session() -> None:
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
    tabs = st.tabs(["登录", "注册"])
    with tabs[0]:
        with st.form("auth_login"):
            u = st.text_input("用户名", key="login_u")
            p = st.text_input("密码", type="password", key="login_p")
            if st.form_submit_button("登录"):
                ok, payload = login(u, p)
                if ok:
                    set_session(payload)
                    st.rerun()
                else:
                    st.error(payload)
    with tabs[1]:
        with st.form("auth_register"):
            u = st.text_input("用户名（自定义）", key="reg_u")
            p = st.text_input("密码（至少 6 位）", type="password", key="reg_p")
            p2 = st.text_input("确认密码", type="password", key="reg_p2")
            if st.form_submit_button("注册并登录"):
                if p != p2:
                    st.error("两次输入的密码不一致")
                else:
                    ok, msg = register(u, p)
                    if ok:
                        ok2, token = login(u, p)
                        if ok2:
                            set_session(token)
                            st.rerun()
                        else:
                            st.error(token)
                    else:
                        st.error(msg)


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

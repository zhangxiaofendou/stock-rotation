"""全链路一键诊断器。

存在意义
--------
线上出问题时，「猜一个原因 → 让用户去试 → 再猜」的排查方式极其低效，
本模块把所有可能出错的环节一次性全查完，直接给出「哪一环坏了 + 怎么修」。

覆盖 8 层：
    L1 运行环境   Python 版本、平台、是否 Streamlit Cloud
    L2 依赖       关键第三方包是否装上、版本、Postgres 驱动
    L3 代码版本   git 短哈希，判断云端是否同步到最新提交
    L4 配置       DATABASE_URL 静态体检（不连库就能查出 90% 的连不上问题）
    L5 持久化     持久目录可写性、是否为易失磁盘
    L6 数据库     真实连通、建表、读写探针、账号数与持仓数
    L7 认证       密码哈希、令牌签名、篡改拒绝、会话密钥稳定性
    L8 应用       页面模块可导入、存储后端选择是否符合预期

用法
----
    命令行：  python diagnostics.py
    页面内：  auth.render_diagnostics_panel()（登录页与侧边栏均有入口）

设计约束：任何单项检查失败都不得让诊断器本身崩溃；所有探针必须无副作用
（写入云库的探针键在检查结束时删除，认证检查只用纯函数与虚构用户名）。
"""

from __future__ import annotations

import importlib
import os
import platform
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
_ICON = {PASS: "[OK]  ", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}


@dataclass
class Check:
    category: str
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    def add(self, category: str, name: str, status: str, detail: str = "", fix: str = "") -> None:
        self.checks.append(Check(category, name, status, detail, fix))

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if c.status == WARN]

    def counts(self) -> dict:
        out = {PASS: 0, WARN: 0, FAIL: 0, SKIP: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def verdict(self) -> str:
        if self.failures:
            return "存在阻断性问题"
        if self.warnings:
            return "可运行，但有隐患"
        return "全部正常"


def _guard(report: Report, category: str, name: str):
    """把检查函数里的意外异常转成 FAIL，保证诊断器自身永不崩溃。"""
    class _Ctx:
        def __enter__(self_inner):
            return None

        def __exit__(self_inner, exc_type, exc, tb):
            if exc is not None:
                report.add(category, name, FAIL, f"检查过程异常：{exc!r}",
                           "这是诊断器内部错误，把本行发给开发者。")
                return True
            return False

    return _Ctx()


# ============================================================
# L1 运行环境
# ============================================================
def check_runtime(report: Report) -> None:
    cat = "L1 运行环境"
    with _guard(report, cat, "Python 版本"):
        v = sys.version_info
        ver = f"{v.major}.{v.minor}.{v.micro}"
        if v.major == 3 and 10 <= v.minor <= 13:
            report.add(cat, "Python 版本", PASS, ver)
        elif v.major == 3 and v.minor >= 14:
            report.add(cat, "Python 版本", WARN, ver,
                       "3.14 过新，部分二进制包（如 psycopg）可能没有对应 wheel 而装不上。"
                       "建议在 Streamlit Cloud 的 Settings → General 里把 Python version 改为 3.12。")
        else:
            report.add(cat, "Python 版本", WARN, ver, "版本偏低，建议 3.11 ~ 3.13。")

    with _guard(report, cat, "运行平台"):
        report.add(cat, "运行平台", PASS, f"{platform.system()} {platform.machine()}")

    with _guard(report, cat, "运行位置"):
        cloud = bool(os.environ.get("STREAMLIT_RUNTIME_ENV") or os.environ.get("STREAMLIT_SERVER_PORT")) \
            or "/mount/src" in PROJECT_ROOT.replace("\\", "/")
        report.add(cat, "运行位置", PASS,
                   "Streamlit Cloud" if cloud else "本地 / 自托管")


# ============================================================
# L2 依赖
# ============================================================
_REQUIRED = [
    ("streamlit", "streamlit"),
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("plotly", "plotly"),
]


def check_dependencies(report: Report) -> None:
    cat = "L2 依赖"
    for label, mod in _REQUIRED:
        with _guard(report, cat, label):
            try:
                m = importlib.import_module(mod)
                report.add(cat, label, PASS, getattr(m, "__version__", "已安装"))
            except Exception as e:
                report.add(cat, label, FAIL, f"导入失败：{e}",
                           f"在 requirements.txt 中确认 {label} 已列出，然后 Reboot 应用。")

    with _guard(report, cat, "Postgres 驱动"):
        from data.storage import pg_store
        drv = pg_store.driver_name()
        if drv != "none":
            report.add(cat, "Postgres 驱动", PASS, drv)
        elif pg_store.get_database_url():
            # 配了云库却没驱动 —— 数据一定存不进去，是阻断级问题
            report.add(cat, "Postgres 驱动", FAIL,
                       "已配置 DATABASE_URL 但 psycopg / psycopg2 均未安装",
                       "requirements.txt 需含 psycopg[binary]；若 Python 版本为 3.14 可能没有对应 wheel "
                       "而静默装不上，先把 Cloud 的 Python version 改成 3.12 再 Reboot。")
        else:
            report.add(cat, "Postgres 驱动", SKIP,
                       "未安装，且未配置 DATABASE_URL（本地开发走 SQLite，属正常）")


# ============================================================
# L3 代码版本
# ============================================================
def check_version(report: Report) -> None:
    cat = "L3 代码版本"
    with _guard(report, cat, "部署 commit"):
        try:
            out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                 cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5)
            sha = (out.stdout or "").strip()
        except Exception:
            sha = ""
        if sha:
            report.add(cat, "部署 commit", PASS, sha)
        else:
            report.add(cat, "部署 commit", WARN, "无法读取 git 信息",
                       "部署环境可能没有 .git 目录，属正常现象；无法据此判断版本同步情况。")


# ============================================================
# L4 配置：DATABASE_URL 静态体检
# ------------------------------------------------------------
# 这一层不连库，纯靠字符串就能查出绝大多数「连不上」的根因：
# 直连地址仅 IPv6、密码含未编码特殊字符、占位符没替换、pooler 用户名写错。
# ============================================================
def _mask(url: str) -> str:
    """隐藏密码后返回可安全展示的连接串。"""
    try:
        head, tail = url.split("://", 1)
        if "@" not in tail:
            return url
        userinfo, host = tail.rsplit("@", 1)
        if ":" in userinfo:
            user, _pwd = userinfo.split(":", 1)
            return f"{head}://{user}:******@{host}"
        return f"{head}://{userinfo}@{host}"
    except Exception:
        return "<无法解析>"


def check_database_url(report: Report) -> Optional[str]:
    cat = "L4 配置"
    raw: Optional[str] = None

    with _guard(report, cat, "DATABASE_URL 是否配置"):
        raw = os.environ.get("DATABASE_URL")
        if not raw:
            try:
                import streamlit as st
                raw = st.secrets.get("DATABASE_URL")  # type: ignore[attr-defined]
            except Exception:
                raw = None
        raw = str(raw).strip() if raw else None
        if not raw:
            report.add(cat, "DATABASE_URL 是否配置", WARN, "未配置",
                       "未配置时账号与持仓写在容器临时磁盘，Streamlit Cloud 每次重部署都会清空。"
                       "如需持久保存，请在 Settings → Secrets 里加一行："
                       'DATABASE_URL = "postgresql://用户名:密码@主机:5432/postgres"')
            return None
        report.add(cat, "DATABASE_URL 是否配置", PASS, _mask(raw))

    if not raw:
        return None

    # ---- 结构解析 ----
    scheme = user = pwd = host = port = dbname = ""
    with _guard(report, cat, "连接串结构"):
        if "://" not in raw:
            report.add(cat, "连接串结构", FAIL, "缺少 scheme",
                       '必须以 postgresql:// 开头，形如 postgresql://用户名:密码@主机:5432/postgres')
            return raw
        scheme, tail = raw.split("://", 1)
        if scheme not in ("postgresql", "postgres"):
            report.add(cat, "连接串结构", FAIL, f"scheme 是 {scheme}",
                       "必须是 postgresql:// 或 postgres://")
            return raw
        if "@" not in tail:
            report.add(cat, "连接串结构", FAIL, "缺少 @，无法区分账号与主机",
                       "正确格式：postgresql://用户名:密码@主机:5432/数据库名")
            return raw
        userinfo, hostpart = tail.rsplit("@", 1)
        user, _, pwd = userinfo.partition(":")
        hostport, _, dbname = hostpart.partition("/")
        dbname = dbname.split("?")[0]
        host, _, port = hostport.partition(":")
        report.add(cat, "连接串结构", PASS,
                   f"user={user} host={host} port={port or '(缺省)'} db={dbname or '(缺省)'}")

    # ---- 密码未编码的特殊字符：本次线上事故的真实根因 ----
    with _guard(report, cat, "密码特殊字符"):
        userinfo = raw.split("://", 1)[1].rsplit("@", 1)[0]
        bad = [ch for ch in ("@", "[", "]", "/", "?", "#", " ") if ch in userinfo]
        if "[YOUR-PASSWORD]" in raw or "your-password" in raw.lower():
            report.add(cat, "密码特殊字符", FAIL, "密码仍是占位符 [YOUR-PASSWORD]",
                       "把 [YOUR-PASSWORD] 替换成数据库真实密码（连方括号一起替换掉）。")
        elif bad:
            enc = {"@": "%40", "[": "%5B", "]": "%5D", "/": "%2F", "?": "%3F", "#": "%23", " ": "%20"}
            rules = "、".join(f"{c} 要写成 {enc[c]}" for c in bad)
            report.add(cat, "密码特殊字符", FAIL,
                       f"账号或密码里含未编码字符：{' '.join(bad)}（会被当成分隔符，导致主机名解析错误）",
                       f"两个办法二选一：① 到 Supabase 点 Reset database password，"
                       f"改成只含字母和数字的密码；② 手动 URL 编码——{rules}。")
        elif not pwd:
            report.add(cat, "密码特殊字符", FAIL, "没有密码部分",
                       "格式应为 用户名:密码@主机，冒号后面要有密码。")
        else:
            report.add(cat, "密码特殊字符", PASS, "无需转义的字符")

    # ---- 主机类型：Supabase 直连地址仅支持 IPv6，Streamlit Cloud 必然连不上 ----
    with _guard(report, cat, "主机可达性类型"):
        h = host.lower()
        if h.startswith("db.") and h.endswith(".supabase.co"):
            ref = h[3:-len(".supabase.co")]
            report.add(cat, "主机可达性类型", FAIL,
                       f"{host} 是 Supabase 直连地址，只解析出 IPv6",
                       "Streamlit Cloud 只有 IPv4，必须改用 Session pooler："
                       "Supabase → Connect → Session pooler，连接串形如 "
                       f"postgresql://postgres.{ref}:密码@aws-0-区域.pooler.supabase.com:5432/postgres")
        elif "pooler.supabase.com" in h:
            report.add(cat, "主机可达性类型", PASS, f"{host}（Supabase pooler，IPv4 可达）")
        elif h:
            report.add(cat, "主机可达性类型", PASS, host)
        else:
            report.add(cat, "主机可达性类型", FAIL, "主机名为空", "检查 @ 后面是否写了主机地址。")

    # ---- pooler 专用用户名格式 ----
    with _guard(report, cat, "用户名格式"):
        if "pooler.supabase.com" in host.lower():
            if user == "postgres":
                report.add(cat, "用户名格式", FAIL,
                           "使用 pooler 时用户名不能是裸 postgres",
                           "pooler 要求 postgres.<项目ref>，例如 postgres.abcdefghijklm。"
                           "直接从 Supabase 的 Session pooler 里整串复制，只替换密码。")
            elif user.startswith("postgres."):
                report.add(cat, "用户名格式", PASS, user)
            else:
                report.add(cat, "用户名格式", WARN, user,
                           "pooler 用户名通常形如 postgres.<项目ref>，请核对。")
        else:
            report.add(cat, "用户名格式", PASS, user or "(空)")

    # ---- 端口 ----
    with _guard(report, cat, "端口"):
        if not port:
            report.add(cat, "端口", WARN, "未指定，将用默认 5432", "建议显式写 :5432。")
        elif port not in ("5432", "6543"):
            report.add(cat, "端口", WARN, port, "Postgres 常用 5432（session）或 6543（transaction pooling）。")
        else:
            report.add(cat, "端口", PASS, port)

    # ---- DNS 解析：把「配置对不对」和「网络通不通」彻底分开 ----
    with _guard(report, cat, "主机 DNS 解析"):
        if host:
            try:
                infos = socket.getaddrinfo(host, None)
                fams = {("IPv6" if i[0] == socket.AF_INET6 else "IPv4") for i in infos}
                if fams == {"IPv6"}:
                    report.add(cat, "主机 DNS 解析", FAIL, f"{host} 只解析到 IPv6",
                               "Streamlit Cloud 不支持 IPv6，请改用 Session pooler 的连接串。")
                else:
                    report.add(cat, "主机 DNS 解析", PASS, f"{host} → {'/'.join(sorted(fams))}")
            except Exception as e:
                report.add(cat, "主机 DNS 解析", FAIL, f"{host} 解析失败：{e}",
                           "主机名拼错，或密码里的特殊字符把主机名截断了（见上一项）。")
    return raw


# ============================================================
# L5 持久化目录
# ============================================================
def check_persistence(report: Report) -> None:
    cat = "L5 持久化"
    with _guard(report, cat, "持久目录可写"):
        from config.settings import PERSIST_DIR
        probe = os.path.join(str(PERSIST_DIR), ".diag_probe")
        try:
            os.makedirs(str(PERSIST_DIR), exist_ok=True)
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            report.add(cat, "持久目录可写", PASS, str(PERSIST_DIR))
        except Exception as e:
            report.add(cat, "持久目录可写", FAIL, f"{PERSIST_DIR} 不可写：{e}",
                       "设置环境变量 PERSISTENT_STORAGE_DIR 指向一个可写目录。")

    with _guard(report, cat, "存储易失性"):
        from data.storage import pg_store
        env_dir = os.environ.get("PERSISTENT_STORAGE_DIR")
        if pg_store.is_enabled():
            report.add(cat, "存储易失性", PASS, "已接入云数据库，重部署不丢数据")
        elif env_dir:
            report.add(cat, "存储易失性", PASS, f"使用持久挂载目录 {env_dir}")
        else:
            report.add(cat, "存储易失性", WARN, "写在容器本地磁盘",
                       "Streamlit Cloud 重部署会清空。配置 DATABASE_URL（云数据库）"
                       "或 PERSISTENT_STORAGE_DIR（持久盘）二选一。")


# ============================================================
# L6 数据库连通与数据量
# ============================================================
def check_database(report: Report) -> None:
    cat = "L6 数据库"
    from data.storage import pg_store

    if not pg_store.is_enabled():
        report.add(cat, "云库连通性", SKIP, "未启用 Postgres（无驱动或未配置 DATABASE_URL）")
        return

    ok = False
    with _guard(report, cat, "云库连通性"):
        ok, msg = pg_store.healthcheck()
        report.add(cat, "云库连通性", PASS if ok else FAIL, msg,
                   "" if ok else "按上面 L4 配置层给出的修复建议处理，改完 Save → Reboot。")

    if not ok:
        return

    with _guard(report, cat, "读写探针"):
        key = "__diagnostic_probe__"
        import secrets as _s
        val = _s.token_hex(4)
        conn = pg_store.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO app_kv (k, v) VALUES (%s, %s) "
                            "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (key, val))
                conn.commit()
                cur.execute("SELECT v FROM app_kv WHERE k = %s", (key,))
                got = cur.fetchone()
                cur.execute("DELETE FROM app_kv WHERE k = %s", (key,))
                conn.commit()
        finally:
            conn.close()
        if got and got[0] == val:
            report.add(cat, "读写探针", PASS, "写入、读回、清理均成功")
        else:
            report.add(cat, "读写探针", FAIL, f"读回值不符：{got}",
                       "数据库权限或事务异常，检查该账号是否有写权限。")

    with _guard(report, cat, "数据量"):
        conn = pg_store.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM app_users")
                n_user = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM portfolio_positions")
                n_pos = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM portfolio_transactions")
                n_tx = cur.fetchone()[0]
                cur.execute("SELECT count(DISTINCT user_id) FROM portfolio_positions")
                n_owner = cur.fetchone()[0]
        finally:
            conn.close()
        report.add(cat, "数据量", PASS,
                   f"账号 {n_user} 个｜持仓 {n_pos} 条（分属 {n_owner} 个用户）｜流水 {n_tx} 条")

    with _guard(report, cat, "会话密钥持久化"):
        sec = pg_store.get_session_secret()
        report.add(cat, "会话密钥持久化", PASS if sec else FAIL,
                   "已存于云库，重启后登录态不会集体失效" if sec else "密钥为空",
                   "" if sec else "app_kv 表写入异常。")


# ============================================================
# L7 认证链路（纯函数 + 虚构用户名，无副作用）
# ============================================================
def check_auth(report: Report) -> None:
    cat = "L7 认证"
    try:
        import auth
    except Exception as e:
        report.add(cat, "认证模块导入", FAIL, f"{e}", "auth.py 有语法或依赖错误。")
        return

    with _guard(report, cat, "密码哈希"):
        rec = auth.hash_password("Diag#Probe#123")
        good = auth.verify_password("Diag#Probe#123", rec)
        bad = auth.verify_password("wrong-password", rec)
        if good and not bad:
            report.add(cat, "密码哈希", PASS, f"PBKDF2 {rec.get('iter')} 次迭代，正确密码通过、错误密码拒绝")
        else:
            report.add(cat, "密码哈希", FAIL, f"正确密码={good} 错误密码={bad}",
                       "hash_password / verify_password 实现被破坏。")

    with _guard(report, cat, "会话密钥稳定性"):
        s1 = auth._get_secret()
        s2 = auth._get_secret()
        if s1 == s2 and s1:
            report.add(cat, "会话密钥稳定性", PASS, "同一进程内取值恒定，不会把已登录用户踢下线")
        else:
            report.add(cat, "会话密钥稳定性", FAIL, "两次取值不一致",
                       "密钥缓存失效，会导致所有令牌随机失效、用户被弹回登录页。")

    with _guard(report, cat, "令牌签名与防篡改"):
        probe_user = "__diag_probe_user__"
        token = auth._make_token(probe_user)
        import base64 as _b64
        raw = _b64.urlsafe_b64decode(token.encode()).decode()
        name, _, sig = raw.partition("|")
        forged = _b64.urlsafe_b64encode(f"admin|{sig}".encode()).decode()
        ok_sign = (name == probe_user and len(sig) == 64)
        ok_forge = auth._verify_token(forged) is None
        if ok_sign and ok_forge:
            report.add(cat, "令牌签名与防篡改", PASS, "签名结构正确，改用户名的伪造令牌被拒绝")
        else:
            report.add(cat, "令牌签名与防篡改", FAIL,
                       f"签名结构正确={ok_sign} 伪造被拒={ok_forge}",
                       "令牌签名逻辑异常，存在越权风险，需立即修复。")

    with _guard(report, cat, "set_session 入参归一化"):
        # 历史 bug：把 login() 返回的令牌当用户名再签一次名，导致登录后被弹回。
        src = ""
        try:
            import inspect
            src = inspect.getsource(auth.set_session)
        except Exception:
            pass
        if "_verify_token" in src:
            report.add(cat, "set_session 入参归一化", PASS, "已兼容用户名与令牌两种入参")
        else:
            report.add(cat, "set_session 入参归一化", WARN, "未见归一化逻辑",
                       "若调用方传入令牌会二次签名，导致登录后被弹回登录页。")

    with _guard(report, cat, "账号存储后端"):
        backend = "云数据库" if auth._use_pg() else "本地文件"
        try:
            n = len(auth._load_creds().get("users", {}))
        except Exception:
            n = -1
        if n < 0:
            report.add(cat, "账号存储后端", FAIL, f"{backend}，读取账号失败",
                       "存储层不可用，此时任何账号都无法登录。")
        elif n == 0:
            report.add(cat, "账号存储后端", WARN, f"{backend}，当前 0 个账号",
                       "还没有注册过账号；若此前注册过，说明数据已丢失或连到了不同的库。")
        else:
            report.add(cat, "账号存储后端", PASS, f"{backend}，已有 {n} 个账号")


# ============================================================
# L8 应用层
# ============================================================
_APP_MODULES = [
    "config.settings",
    "data.storage.pg_store",
    "data.storage.sqlite_store",
    "auth",
    "portfolio.holdings",
    "report.generator",
    "dashboard.pages.portfolio",
    "dashboard.pages.reports",
    "dashboard.app",
]


def check_app(report: Report) -> None:
    cat = "L8 应用"
    broken = []
    for mod in _APP_MODULES:
        try:
            importlib.import_module(mod)
        except Exception as e:
            broken.append(f"{mod}: {e}")
    if broken:
        report.add(cat, "模块可导入", FAIL, "; ".join(broken),
                   "对应模块有语法错误或缺依赖，页面会白屏。")
    else:
        report.add(cat, "模块可导入", PASS, f"{len(_APP_MODULES)} 个核心模块全部可导入")

    with _guard(report, cat, "持仓存储后端"):
        from portfolio.holdings import PortfolioHoldings
        svc = PortfolioHoldings(user_id="__diag__")
        name = type(svc.store).__name__
        from data.storage import pg_store
        if pg_store.is_enabled() and name != "PGStore":
            report.add(cat, "持仓存储后端", FAIL, f"已配置云库却仍在用 {name}",
                       "后端选择逻辑异常，持仓仍会写到临时磁盘。")
        else:
            report.add(cat, "持仓存储后端", PASS, name)

    with _guard(report, cat, "多用户隔离"):
        from portfolio.holdings import PortfolioHoldings
        probe = PortfolioHoldings(user_id="__diag_user_probe__")
        df = probe.positions()
        if len(df) == 0:
            report.add(cat, "多用户隔离", PASS, "按 user_id 过滤生效（虚构用户查得 0 条持仓）")
        else:
            report.add(cat, "多用户隔离", FAIL,
                       f"虚构用户竟查到 {len(df)} 条持仓",
                       "user_id 过滤失效，会导致不同使用者互相看到对方仓位，须立即修复。")


# ============================================================
# 编排与输出
# ============================================================
def run_all() -> Report:
    report = Report()
    for fn in (check_runtime, check_dependencies, check_version,
               check_database_url, check_persistence, check_database,
               check_auth, check_app):
        try:
            fn(report)
        except Exception as e:  # 单层崩溃不影响其余层
            report.add(fn.__name__, "该层执行异常", FAIL, repr(e))
    return report


def format_text(report: Report) -> str:
    lines: List[str] = []
    c = report.counts()
    lines.append("=" * 62)
    lines.append("  板块轮动系统 · 全链路诊断报告")
    lines.append(f"  结论：{report.verdict()}    "
                 f"通过 {c[PASS]} ｜ 警告 {c[WARN]} ｜ 失败 {c[FAIL]} ｜ 跳过 {c[SKIP]}")
    lines.append("=" * 62)

    current = None
    for chk in report.checks:
        if chk.category != current:
            current = chk.category
            lines.append("")
            lines.append(f"【{current}】")
        lines.append(f"  {_ICON[chk.status]} {chk.name}: {chk.detail}")

    problems = report.failures + report.warnings
    if problems:
        lines.append("")
        lines.append("-" * 62)
        lines.append("需要处理的问题（按严重度排序）")
        lines.append("-" * 62)
        for i, chk in enumerate(problems, 1):
            tag = "阻断" if chk.status == FAIL else "隐患"
            lines.append(f"{i}. [{tag}] {chk.category} / {chk.name}")
            lines.append(f"   现象：{chk.detail}")
            if chk.fix:
                lines.append(f"   处理：{chk.fix}")
    else:
        lines.append("")
        lines.append("所有环节均正常，无需处理。")
    return "\n".join(lines)


def main() -> int:
    report = run_all()
    print(format_text(report))
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

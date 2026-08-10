"""部署版本角标。

每次页面渲染时显示当前 Cloud/容器里的 git HEAD short hash + commit 时间。
让用户一眼看清「远端是不是同步到了最新 commit」，避免「代码改了但 Cloud 没
刷新」导致的反复来回。

读取策略：
  1) 优先尝试 ``git rev-parse --short HEAD`` + ``git log -1 --format=%ci``；
  2) 失败则直接读 ``.git/HEAD`` + ``.git/refs/heads/<branch>`` 解析；
  3) 都没有则回退 "unknown"。

依赖：只读，不修改仓库状态。
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


_REPO_ROOT_HINTS = (".git", "streamlit_app.py", "dashboard", "config")


def _find_repo_root(start: Path) -> Path | None:
    """向上找含有 .git 的目录（仓库根）。"""
    cur = start.resolve()
    for _ in range(6):
        if (cur / ".git").exists():
            return cur
        if any((cur / h).exists() for h in _REPO_ROOT_HINTS if h != ".git"):
            # dashboard/ 这种子目录也接受，但 .git 优先级更高
            return cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent
    return None


def _read_git_file(repo_root: Path) -> tuple[str, str] | None:
    """直接从 .git 文件读 HEAD + ref，不依赖 git 二进制。"""
    head_path = repo_root / ".git" / "HEAD"
    if not head_path.exists():
        return None
    try:
        head = head_path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return None
    if not head.startswith("ref:"):
        # detached HEAD，文件里直接是 hash
        if re.fullmatch(r"[0-9a-f]{7,40}", head):
            return head[:7], ""
        return None
    ref_rel = head.split(":", 1)[1].strip()  # refs/heads/main
    ref_path = repo_root / ".git" / ref_rel
    if not ref_path.exists():
        # packed-refs
        packed = repo_root / ".git" / "packed-refs"
        if packed.exists():
            try:
                for line in packed.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1].strip() == ref_rel:
                        return parts[0][:7], ""
            except Exception:
                return None
        return None
    try:
        sha = ref_path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return None
    return (sha[:7] if sha else "", "")


def _git_subprocess(repo_root: Path) -> tuple[str, str] | None:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if not sha:
            return None
        when = subprocess.run(
            ["git", "log", "-1", "--format=%cI"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        return sha, when
    except Exception:
        return None


def get_deploy_version() -> dict:
    """读取当前部署版本号。

    Returns
    -------
    dict: {"short": "4b751e1", "full": "4b751e1...", "committed_at": "2026-08-10T13:36+08:00"}
          任何字段读不到都是空串，绝不抛异常。
    """
    # 仓库根：模块文件路径向上找
    here = Path(__file__).resolve().parent
    repo_root = _find_repo_root(here)
    short, when = "", ""
    if repo_root:
        got = _git_subprocess(repo_root) or _read_git_file(repo_root)
        if got:
            short, when = got
    # 退路：用模块文件 mtime（最差但总能有值）
    if not short:
        try:
            mt = datetime.fromtimestamp(here.stat().st_mtime, tz=timezone.utc)
            short = "build-" + mt.strftime("%m%d%H%M")
            when = mt.isoformat()
        except Exception:
            short, when = "unknown", ""
    return {"short": short, "committed_at": when}


def render_version_indicator(prefix: str = "🚀 部署版本", color: str = "#722ed1"):
    """在页面顶部渲染一行小角标，显示当前 commit。

    用 ``st.caption`` 包裹单行文字，hover 提示完整信息；HTML 也支持时
    给出与数据来源徽标一致的颜色样式。
    """
    from dashboard.components.data_source_badge import _hex_to_rgba  # noqa: WPS433
    import streamlit as st

    info = get_deploy_version()
    short = info.get("short") or "unknown"
    when = info.get("committed_at") or ""
    when_short = when[:16].replace("T", " ") if when else ""
    tip = f"commit {short}"
    if when_short:
        tip += f"  ·  {when_short} UTC"
    bg = _hex_to_rgba(color, 0.10)
    html = (
        f'<div style="display:inline-block;margin:2px 0 6px;'
        f'padding:1px 9px;border-radius:9px;font-size:11px;font-weight:600;'
        f'background:{bg};color:{color};border:1px solid {color};">'
        f'{prefix}：<code style="background:transparent;color:inherit;">{short}</code>'
        + (f'  <span style="opacity:.7;">·  {when_short}</span>' if when_short else "")
        + f'</div>'
    )
    st.markdown(html, unsafe_allow_html=True)
    # hover 提示
    if tip:
        st.caption(tip)
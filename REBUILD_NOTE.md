# 触发 ModelScope 容器重建

构建说明：本仓库从 f5234af 起已删除 `dashboard/pages/` 目录、改名为 `dashboard/views/`，
但 ModelScope 容器文件系统可能残留 `dashboard/pages/`（git pull 不清理未跟踪目录），
导致 Streamlit auto-discovery 仍发现 8 个英文 subpage。

本次提交触发 webhook → ModelScope 重新 git clone + checkout HEAD → 容器文件系统中残留的
`dashboard/pages/` 会被清空 → 侧栏仅剩 6 个中文 st.radio 菜单。

## 部署说明
- 入口：`app.py`（ModelScope / Studio） + `streamlit_app.py`（HF Spaces）
- 数据：22 个 parquet 在 `data/storage/parquet/`，已 `.gitignore` 不推；运行时从 Supabase parquet_mirror 恢复
- 持久化：账号/持仓写 Cloud Postgres（DATABASE_URL 环境变量），SQLite 作为本地镜像
- 多用户：登录界面默认账号已演示在「登录/注册」说明里，密码本地哈希

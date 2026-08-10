# HF Spaces 部署指南（Hugging Face Spaces）

> GitHub OAuth 出问题时，HF Spaces 是一个不依赖 GitHub 登录、且对 Streamlit 原生友好的备选平台。本文档给出从零到能跑的最短路径。

---

## 0. 为什么仓库"已经能跑"

- 入口文件 `streamlit_app.py` 已在仓库根（HF Streamlit Space 默认找的就是这个名）。
- 依赖清单 `requirements.txt` 在仓库根（HF 会自动 `pip install -r requirements.txt`）。
- Secrets 读取顺序已统一为 **先环境变量、再 st.secrets**（见 `data/storage/pg_store.py: get_database_url()`），HF 的 Variables / Secrets 会被注入为环境变量，无需改代码。

所以"代码改动 = 0"，你只需做平台侧配置即可。

---

## 1. 创建 HF 账号 + Space（约 3 分钟）

1. 打开 https://huggingface.co/join 注册（邮箱即可，**不依赖 GitHub**）。
2. 右上角头像 → **+ New Space**。
3. 填写：
   - **Space name**：`stock-rotation`（最终链接是 `https://huggingface.co/spaces/<你的用户名>/stock-rotation`）
   - **License**：随便选一个
   - **SDK**：选 **Streamlit** ← 关键
   - **Space hardware**：CPU basic（免费），够用
   - **Visibility**：Public 或 Private 随你（Public 才能直接打开链接）
4. 点 **Create Space**，会进到一个空仓库。

---

## 2. 把代码同步到 Space（选一种）

### 方式 A：用 git push（推荐，干净）

```bash
# 在本地仓库根目录
git remote add hf https://<你的HF用户名>:<你的HF_token>@huggingface.co/spaces/<你的HF用户名>/stock-rotation
# HF token 在 https://huggingface.co/settings/tokens 生成（type 选 write）
git push hf main
```

### 方式 B：HF 页面直接同步 GitHub

Space 页面 → **Files** → 顶部菜单 **Add file** → **Upload files**，把整个仓库 zip 上传（**注意排除 `.git/`、`__pycache__/`、`data/storage/parquet/*.parquet`、`*.pyc`**）。

### 方式 C：让 HF 直接拉 GitHub

Space 设置 → **Repository** → **Link to a GitHub repository** → 选 `zhangxiaofendou/stock-rotation`，分支 `main`。这样你下次 `git push github main` 时 HF 也会自动同步。

> 方式 C 最省事，但前提是 HF 能访问你的 GitHub repo（私有 repo 需要授权）。

---

## 3. 配置 Secrets（关键！）

Space 页面 → **Settings** → **Variables and secrets**：

### 必填（Secrets 区）
| Name | Value | 备注 |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:<密码>@db.<项目ref>.supabase.co:5432/postgres` | 复用你 Streamlit Cloud Secrets 里那条。**注意 HF Secrets 的 value 必须>=4 字符；空 value 会报"Invalid value"**。 |

### 可选（按需）
| Name | Value |
|---|---|
| `TUSHARE_TOKEN` | 你的 tushare token（如未启用数据源对比则不填） |
| `PERSISTENT_STORAGE_DIR` | HF 免费层磁盘非持久，**留空即可** |

### 不需要填的
- `PGHOST` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` —— 我们的代码统一从 `DATABASE_URL` 解析。

填完点 **Save**，Space 会自动重启（约 30–60 秒）。

---

## 4. 验证部署成功

打开 `https://huggingface.co/spaces/<你的用户名>/stock-rotation`，应看到：
- 页面**顶部角标**显示当前 commit hash（应等于你最近一次 push 的 hash，例如 `f90de23`）
- 顶部导航栏有"板块轮动监控 / 持仓管理 / 板块对账"等页面入口
- 「持仓管理」里 城地香江(603887) 的「所属行业 / 行业代码 / 板块状态」**已带出**（这是修复的核心验证点）

### 排查路径
- 页面卡在 "Building..."：看 Space 页面右上的 **Logs**，最常见是 `requirements.txt` 里有版本装不上。
- 页面打开但报 "Internal Server Error"：先看 Logs 顶部异常栈，多半是某个 `secrets` 没填。
- 持仓全空：通常是 `DATABASE_URL` 没填 / 填错，导致回退到本地 SQLite（HF 免费层磁盘非持久，重启就丢）。

---

## 5. 已知差异（vs Streamlit Cloud）

| 维度 | Streamlit Cloud | HF Spaces (CPU basic) |
|---|---|---|
| 磁盘持久化 | ✅ | ❌（每次重启清空，但你的数据在 Postgres，无影响） |
| Cold start | 极少（资源池常驻） | 48 小时无访问会休眠，下次访问 ~30s 唤醒 |
| 并发 | 多 | 低（CPU basic 同空间只 2 vCPU，复杂页面可能慢） |
| 资源限制 | 1 GB | 16 GB RAM / 2 vCPU / 50 GB 磁盘（实际上比 Streamlit 还宽松） |
| 登录 | GitHub OAuth（你刚撞 500 的就是这个） | HF 账号 / 邮箱，无 OAuth |
| 自定义域名 | ✅ | ❌（只能用 `huggingface.co/spaces/...`） |

---

## 6. 什么时候切回 Streamlit Cloud

GitHub status 恢复 + Streamlit Cloud Reboot 之后，可以两边都保留。本仓库的代码对**两个平台都兼容**，唯一区别是 Secrets 怎么填：
- Streamlit Cloud：`app settings → Secrets`，TOML 格式
- HF Spaces：`Settings → Variables and secrets`，key-value 格式

哪天 GitHub OAuth 又挂了，HF 这边继续用就行。

---

## 7. 常见问题

### Q: 我的 `DATABASE_URL` 里有特殊字符怎么办？
A: URL encode 一下。例如密码含 `@` → `%40`。最稳的是用 Supabase 给的"Connection string"原始值（已经是 URL-encoded 的）。

### Q: HF 构建日志说 `psycopg` 装不上？
A: `requirements.txt` 里写的是 `psycopg[binary]>=3.1.18`，`[binary]` 包含预编译 wheel，HF 镜像支持。如果还失败，可以临时换成 `psycopg2-binary>=2.9.9`（更老但兼容性更好）。

### Q: HF 显示"Space sleeping"？
A: 免费 CPU 层策略。点开链接会立刻唤醒（等 ~30s）。如果不想等，升级到付费层可避免。

### Q: 想换私有 repo 怎么搞？
A: 私有 repo 也能 push 给 HF Space（方式 A 里的 token），但 Space 本身如果是 Public，里面的代码逻辑也会被外界看到。生产敏感数据请用 HF Secrets 而不是写死在代码里。

---

_最后更新：2026-08-10 — 配合 commit `f90de23`_
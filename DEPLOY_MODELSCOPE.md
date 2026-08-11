# ModelScope 创空间部署指南（魔搭）

> 你之前在 Streamlit Cloud 卡在 GitHub OAuth 500、huggingface.co 国内又直连不上。
> ModelScope（阿里达摩院「魔搭」）是**国内可直连、登录走支付宝/淘宝/微信、不依赖 GitHub**
> 的备选平台，且对 Streamlit 原生友好。本指南给出从零到能跑的最短路径。

---

## 0. 为什么仓库"已经能跑"

- 本次新增入口文件 `app.py`（ModelScope 创空间默认找的就是这个名），内容与
  `streamlit_app.py` 完全等价，仓库对 Streamlit Cloud / HF Spaces / ModelScope 三平台
  同时兼容。
- `README.md` 已写入 HF Spaces / ModelScope 通用的 frontmatter（`sdk: streamlit`、
  `app_file: app.py`），平台会自动识别入口，无需在 UI 里手填。
- 依赖清单 `requirements.txt` 在仓库根（平台会自动 `pip install -r requirements.txt`）。
- Secrets 读取顺序已统一为 **先环境变量、再 st.secrets**（见
  `data/storage/pg_store.py: get_database_url()`），ModelScope 的「环境变量」会被注入
  为进程环境变量，无需改代码。

所以"代码改动 = 0"，你只需做平台侧配置即可。

---

## 1. 创建 ModelScope 账号 + 创空间（约 3 分钟）

1. 打开 https://modelscope.cn 注册 / 登录（支付宝 / 淘宝 / 微信扫码即可，
   **不依赖 GitHub**，这也正是选它的原因）。
2. 进入「创空间」（Studio）→ **创建创空间**（或「我的 → 创空间 → 新建」）。
3. 填写：
   - **名称**：`stock-rotation`（最终链接形如
     `https://modelscope.cn/studios/<你的用户名>/stock-rotation`）
   - **SDK**：选 **Streamlit** ← 关键
   - **硬件 / 算力**：CPU（免费层够用）
   - **可见性**：公开 / 私有随你（公开才能直接打开链接）
4. 创建后会进到一个空仓库（带一个 git 地址和 `app.py` 骨架，我们等下用自己的替换）。

> 如果创建时平台没有自动识别入口，在「设置 / 高级」里把**启动文件**手动设为 `app.py`
> 即可（README 的 frontmatter 通常会让平台自动认到）。

---

## 2. 把代码同步到创空间（选一种）

> ⚠️ 强烈建议用**方式 A**（直推 ModelScope 自己的 git），完全绕开 GitHub。
> 方式 C「关联 GitHub 仓库」需要 GitHub 授权，你现在正好撞 GitHub OAuth 500，别走这条。

### 方式 A：直推 ModelScope git（推荐，不依赖 GitHub）

```bash
# 在本地仓库根目录，添加 ModelScope 的远程（地址在创空间页面的「克隆 / Git 地址」里复制）
git remote add modelscope https://modelscope.cn/studios/<你的用户名>/stock-rotation.git
# 首次推送（ModelScope 用你的账号密码 / Access Token 认证，不是 GitHub）
git push modelscope main
```

> 认证：ModelScope 推送用你在 modelscope.cn 的**账号密码**或「访问令牌(Access Token)」。
> Token 在账号设置 → 安全 → 访问令牌 生成。若用 token，远程地址写成
> `https://<你的用户名>:<token>@modelscope.cn/studios/<你的用户名>/stock-rotation.git`。

### 方式 B：上传 zip（最稳，不用任何 git）

创空间页面 → **文件** → **上传**，把整个仓库 zip 上传
（**注意排除 `.git/`、`__pycache__/`、`*.pyc`**；parquet 数据文件可带可不带，
应用启动后会从 Postgres 恢复基数据）。

### 方式 C：关联 GitHub 仓库（不推荐，当前会卡 GitHub OAuth）

创空间设置里「关联代码仓库」→ 选 GitHub `zhangxiaofendou/stock-rotation`。
因为需要 GitHub 登录授权，你现在 OAuth 500 下这条路走不通，先别用。

---

## 3. 配置环境变量（关键！）

创空间页面 → **设置 → 环境变量**（或「Secrets」区，不同版本叫法略有差异）：

### 必填
| 变量名 | 值 | 备注 |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:<密码>@db.<项目ref>.supabase.co:5432/postgres` | 复用你 Streamlit Cloud Secrets 里那条 Postgres 连接串。密码含 `@` 等特殊字符需 URL encode（用 Supabase 给的原始 Connection string 最稳，已 encode）。 |

### 可选（按需）
| 变量名 | 值 |
|---|---|
| `TUSHARE_TOKEN` | 你的 tushare token（未启用数据源对比则不填） |

### 不需要填的
- `PGHOST` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` —— 代码统一从 `DATABASE_URL` 解析。

填完保存，创空间会自动重启（约 30–60 秒）。

---

## 4. 验证部署成功

打开 `https://modelscope.cn/studios/<你的用户名>/stock-rotation`，应看到：
- 页面**顶部角标**显示当前 commit hash（应等于你最近一次 push 的 hash，例如本次
  新增 `app.py` / `README.md` / 本文档后的 commit）
- 顶部导航栏有「板块轮动监控 / 持仓管理 / 板块对账」等页面入口
- 「持仓管理」里 城地香江(603887) 的「所属行业 / 行业代码 / 板块状态」**已带出**
  （这是之前修复的核心验证点）

### 排查路径
- 卡在「构建中 / Building」：看创空间「日志」，最常见是 `requirements.txt` 里某个
  版本装不上（如 `psycopg[binary]` 可临时换 `psycopg2-binary>=2.9.9`）。
- 打开报「Internal Server Error」：先看日志顶部异常栈，多半是环境变量没填。
- 持仓全空：通常是 `DATABASE_URL` 没填 / 填错，回退到本地 SQLite（免费层磁盘非持久，
  重启就丢）。

---

## 5. 已知差异（vs Streamlit Cloud / HF Spaces）

| 维度 | Streamlit Cloud | HF Spaces | ModelScope 创空间 |
|---|---|---|---|
| 国内直连 | ✅ | ❌（需梯子） | ✅ |
| 登录方式 | GitHub OAuth（你撞 500） | HF 账号 / 邮箱 | 支付宝 / 淘宝 / 微信 |
| 默认入口 | `streamlit_app.py` | `app.py` / `streamlit_app.py` | `app.py` |
| 磁盘持久化 | ✅ | ❌ | ❌（数据在 Postgres，无影响） |
| 休眠策略 | 常驻 | 48h 无访问休眠 | 有空闲回收，访问即唤醒 |
| 自定义域名 | ✅ | ❌ | ❌ |

---

## 6. 三平台并存策略

本仓库代码对**三个平台都兼容**，唯一区别是 Secrets / 环境变量怎么填：
- Streamlit Cloud：`app settings → Secrets`（TOML）
- HF Spaces：`Settings → Variables and secrets`（key-value）
- ModelScope：`设置 → 环境变量`（key-value）

哪个平台当下能用就用哪个。GitHub OAuth 恢复后，Streamlit Cloud 随时能切回去；
国内访问优先 ModelScope。

---

## 7. 常见问题

### Q: `DATABASE_URL` 里有特殊字符怎么办？
A: URL encode。密码含 `@` → `%40`。最稳是用 Supabase 给的「Connection string」原始值
（已 URL-encoded）。

### Q: 推送 ModelScope 报认证失败？
A: 确认用的是 modelscope.cn 的**账号密码 / Access Token**，不是 GitHub 凭据。
Token 在「账号设置 → 安全 → 访问令牌」。

### Q: 创空间没自动识别 `app.py`？
A: 在「设置 / 高级」把**启动文件**手动设为 `app.py`；README 的 frontmatter 一般会让
平台自动认到，但手动设一次最稳。

### Q: 免费层会休眠 / 回收？
A: 是的，但你的数据都在 Postgres（Supabase），唤醒后秒级恢复基数据，不影响业务。

---

_最后更新：2026-08-11 — 新增 `app.py` + `README.md`（frontmatter）使仓库兼容 ModelScope 创空间_

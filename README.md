---
title: 板块轮动监控
emoji: 📈
colorFrom: blue
colorTo: green
sdk: streamlit
sdk_version: 1.59.2
app_file: app.py
pinned: false
---

# 板块轮动监控 (stock-rotation)

一个面向 A 股板块轮动的持仓分析工具：行业九宫格分布、近 3 次状态变化轨迹、
板块对账与实时行情。数据源以东财 / baostock 为主，持仓与基数据落在 Postgres
（Supabase），部署层无状态。

## 一键部署兼容

本仓库对以下平台**零代码改动**兼容，唯一区别是 Secrets 怎么填：

- **Streamlit Cloud** —— 入口 `streamlit_app.py`，Secrets 用 TOML
- **Hugging Face Spaces** —— 入口 `app.py`（本 README 已声明 `app_file`）
- **ModelScope 创空间** —— 入口 `app.py`，环境变量在「环境变量」里配

## 必填环境变量 / Secrets

| 名称 | 说明 |
|---|---|
| `DATABASE_URL` | Postgres 连接串（`postgresql://...`），代码优先读环境变量 |

详见 `DEPLOY_HF.md` 与 `DEPLOY_MODELSCOPE.md`。

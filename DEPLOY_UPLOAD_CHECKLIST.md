# ModelScope 上传完整性核对清单

上传完成后，在「空间文件」里展开文件树，对照下面每一项，✅ 全有再点「确认并部署」。
（这是 stock-rotation 仓库根目录应有的内容，缺一不可，否则会 ModuleNotFoundError / ImportError）

## 顶层必须文件
- [ ] app.py            ← 入口（Streamlit）
- [ ] streamlit_app.py  ← Streamlit Cloud 兼容入口（可无，但建议有）
- [ ] requirements.txt  ← 依赖清单
- [ ] README.md         ← 含 sdk: streamlit 自动识别

## 顶层必须目录（点开里面要有 .py 文件，不是空的）
- [ ] dashboard/        ← 主程序（关键！里面要有 app.py、pages/、components/）
- [ ] data/            ← 数据源/存储/管线（里面要有 calendar→已改名 market_calendar.py、sources/、storage/、daily_pipeline.py）
- [ ] model/           ← 状态机/评分
- [ ] portfolio/       ← 持仓分析
- [ ] config/          ← 配置
- [ ] indicators/      ← 指标计算
- [ ] ai/              ← AI/情绪
- [ ] backtest/        ← 回测
- [ ] notification/    ← 通知
- [ ] signal_tracker/  ← 信号追踪
- [ ] report/          ← 报告生成

## 必须【没有】的危险项（出现在根目录会崩）
- [ ] calendar.py      ← 绝不能出现在任何层级根目录（已改名 market_calendar.py）
- [ ] .ssh/            ← 含私钥，绝不能传
- [ ] .git/            ← 不必传（平台自己管）

## 快速自检口诀
> 报错 `No module named 'X'` → X 这个目录/文件没传 → 补传它
> 报错 `No module named 'dashboard'` → dashboard/ 整个没传 → 重传整个仓库

## 最稳方案（推荐）
用「通过 Git 上传」：把 ModelScope 给的 git 地址发给助手，助手直接 git push，
一次性推全仓库（自动排除 .ssh/.git/.workbuddy），物理上不可能漏文件。

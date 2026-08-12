"""
Streamlit Cloud 入口文件 v2.0 — baostock 数据源
================================================
将 dashboard/app.py 作为主入口运行。
DEP RECONCILE: baostock>=0.8.8 replaces akshare for spot market data.
"""
import sys
import os

# 确保项目根目录在 Python path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 全局网络兜底超时：外部数据源（同花顺/东财/akshare）在云端被墙时，
# 未设超时的 socket 调用会无限挂起导致页面转圈。统一给 12s 硬上限。
import socket
socket.setdefaulttimeout(12)

from dashboard.app import main

if __name__ == "__main__":
    main()

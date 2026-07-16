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

from dashboard.app import main

if __name__ == "__main__":
    main()

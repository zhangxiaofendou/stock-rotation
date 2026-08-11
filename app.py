"""
ModelScope 创空间 (Studio) 入口文件
====================================
与 streamlit_app.py 完全等价，只是文件名改为 app.py —— 这是 ModelScope
创空间 / Hugging Face Spaces 默认寻找的入口名。两者并存，仓库对
Streamlit Cloud / HF Spaces / ModelScope 三平台均兼容。

将 dashboard/app.py 作为主入口运行。
"""
import sys
import os

# 确保项目根目录在 Python path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dashboard.app import main

if __name__ == "__main__":
    main()

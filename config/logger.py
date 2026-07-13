"""
日志工具模块
============
提供控制台输出和文件输出的日志工具。
针对 Windows 多线程环境做了兼容处理。
"""

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from .settings import LOG_DIR


class _SafeRotatingHandler(RotatingFileHandler):
    """Windows 安全的 RotatingFileHandler，捕获旋转时的文件锁冲突"""

    def doRollover(self):
        try:
            super().doRollover()
        except (PermissionError, OSError):
            # Windows 多线程环境下文件被占用，跳过旋转
            if self.stream:
                self.stream.close()
            self.mode = "w"
            self.stream = self._open()


def get_logger(name: str = "stock_rotation") -> logging.Logger:
    """
    获取配置好的日志记录器

    参数:
        name: 日志记录器名称

    返回:
        logging.Logger: 配置好的日志记录器
    """
    logger = logging.getLogger(name)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # 控制台输出 - WARNING 级别（减少 Streamlit 终端噪音）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.WARNING)
    console_fmt = logging.Formatter(
        "[%(levelname)s] %(name)s: %(message)s"
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # 文件输出 - DEBUG 级别
    log_file = LOG_DIR / "stock_rotation.log"
    file_handler = _SafeRotatingHandler(
        str(log_file),
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=3,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    return logger


def suppress_noisy_loggers():
    """抑制第三方库的日志噪音"""
    noisy = [
        "akshare", "matplotlib", "urllib3", "requests",
        "PIL", "parso", "asyncio", "numexpr",
    ]
    for name in noisy:
        logging.getLogger(name).setLevel(logging.WARNING)


# 启动时静默第三方库
suppress_noisy_loggers()

# 默认日志记录器实例
logger = get_logger()

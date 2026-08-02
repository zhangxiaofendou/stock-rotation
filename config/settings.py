"""
全局配置模块
============
项目根目录、数据存储路径、AkShare重试配置、回测配置、
板块风格基准映射、申万行业等级配置、日志配置等。
"""

import os
from pathlib import Path

# ============================================================
# 项目根目录
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ============================================================
# 数据存储路径
# ============================================================
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"

# Parquet 行情数据目录
PARQUET_DIR = DATA_DIR / "storage" / "parquet"

# ============================================================
# 持久化目录（跨部署保留：SQLite 持仓库 + 登录凭证 + 会话密钥）
# ------------------------------------------------------------
# 默认落在项目内的 .persist（本地开发可正常留存）。
# 在 Streamlit Cloud 上部署多用户持仓时，必须在后台「Secrets / 环境变量」
# 中设置 PERSISTENT_STORAGE_DIR 为已开启的 Persistent Storage 挂载路径，
# 否则数据仍写在临时磁盘、每次重部署即清空。
# ============================================================
PERSIST_DIR = Path(os.environ.get("PERSISTENT_STORAGE_DIR") or (PROJECT_ROOT / ".persist"))
PERSIST_DIR.mkdir(parents=True, exist_ok=True)

# SQLite 数据库路径（含板块/持仓/新鲜度等全部元数）—— 移入持久化目录
SQLITE_DB_PATH = PERSIST_DIR / "sector_rotation.db"

# 多用户登录凭证（JSON）与会话签名密钥
CREDENTIALS_PATH = PERSIST_DIR / "credentials.json"
SESSION_SECRET_PATH = PERSIST_DIR / "session_secret.bin"

# 确保目录存在
for _dir in [DATA_DIR, CACHE_DIR, PARQUET_DIR, SQLITE_DB_PATH.parent, CREDENTIALS_PATH.parent]:
    _dir.mkdir(parents=True, exist_ok=True)

# ============================================================
# 主数据源选择
# ============================================================
# "ths"：同花顺公开接口（行业板块K线到最新交易日收盘，无需 token，实测含 2026-07-31）
# "eastmoney"：东方财富公开接口（行情到最新交易日收盘，无需 token，准实时）
# "akshare"：原有 AkShare 实现（回退用）
PRIMARY_DATA_SOURCE = "ths"

# ============================================================
# AkShare 重试配置
# ============================================================
AKSHARE_RETRY_CONFIG = {
    "max_retries": 3,          # 最大重试次数
    "retry_interval": 2,        # 重试间隔（秒）
    "backoff_factor": 2,        # 退避因子（每次重试间隔翻倍）
}

# ============================================================
# 回测配置
# ============================================================
BACKTEST_CONFIG = {
    "start_date": "2018-01-01",  # 回测起始日期
    "transaction_cost": 0.0015,  # 交易成本 0.15%（单边）
    "benchmark": "000300.SH",     # 默认基准：沪深300
}

# ============================================================
# 板块风格基准映射（一级风格 → 基准指数代码）
# ============================================================
STYLE_BENCHMARK_MAP = {
    "大盘价值": "000300.SH",    # 沪深300
    "大盘成长": "000300.SH",    # 沪深300
    "中盘成长": "000905.SH",    # 中证500
    "中盘价值": "000905.SH",    # 中证500
    "小盘":     "000852.SH",    # 中证1000
    "小盘成长": "000852.SH",    # 中证1000
    "小盘价值": "000852.SH",    # 中证1000
}

# 基准指数代码 → 名称
BENCHMARK_INDEXES = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000016.SH": "上证50",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
}

# ============================================================
# 申万行业等级配置
# ============================================================
# 申万一级行业数量
SW_LEVEL1_COUNT = 31
# 申万二级行业数量（大约131个）
SW_LEVEL2_COUNT = 131

# ============================================================
# 日志配置
# ============================================================
LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "simple": {
            "format": "[%(levelname)s] %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "simple",
            "stream": "ext://sys.stdout",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "DEBUG",
            "formatter": "default",
            "filename": str(PROJECT_ROOT / "logs" / "stock_rotation.log"),
            "maxBytes": 10 * 1024 * 1024,  # 10MB
            "backupCount": 5,
            "encoding": "utf-8",
        },
    },
    "loggers": {
        "stock_rotation": {
            "level": "DEBUG",
            "handlers": ["console", "file"],
            "propagate": False,
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"],
    },
}

# 确保日志目录存在
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# 数据新鲜度配置
# ============================================================
# 数据过期警告阈值（小时）
DATA_STALE_HOURS = 24

# ============================================================
# 数据拉取配置
# ============================================================
# 批量拉取时的批次大小
BATCH_SIZE = 10
# 批次间休眠时间（秒），避免触发频率限制
BATCH_SLEEP = 1

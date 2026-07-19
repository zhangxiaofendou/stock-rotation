"""
实验记录存储
============
每次回测自动生成一条实验记录，存为 JSON（PRD §6.8）：
  实验ID / 时间戳 / 策略版本+git hash / 参数快照 / 回测区间 /
  核心指标 / 分年度收益 / 备注

存储路径：data/storage/experiments/*.json
（运行期产物，不纳入版本控制，与 db/parquet 同列 .gitignore）
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from config.logger import get_logger
from config.settings import DATA_DIR

logger = get_logger(__name__)

EXP_DIR = DATA_DIR / "storage" / "experiments"


def _git_hash() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(DATA_DIR.parent), capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def save_experiment(
    strategy_name: str,
    params: dict,
    metrics: dict,
    start: str,
    end: str,
    note: str = "",
) -> str:
    """保存一条实验记录，返回实验ID。"""
    EXP_DIR.mkdir(parents=True, exist_ok=True)
    exp_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    record = {
        "experiment_id": exp_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "strategy_name": strategy_name,
        "git_hash": _git_hash(),
        "params": params,
        "start": start,
        "end": end,
        "metrics": metrics,
        "note": note,
    }
    path = EXP_DIR / f"{exp_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2, default=str)
    logger.info("实验记录已保存: %s", exp_id)
    return exp_id


def list_experiments() -> List[dict]:
    """列出所有实验记录（按时间倒序）。"""
    if not EXP_DIR.exists():
        return []
    recs = []
    for f in EXP_DIR.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                recs.append(json.load(fh))
        except Exception as e:
            logger.warning("读取实验记录 %s 失败: %s", f, e)
    recs.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return recs


def load_experiment(exp_id: str) -> Optional[dict]:
    path = EXP_DIR / f"{exp_id}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

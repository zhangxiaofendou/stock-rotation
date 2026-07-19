"""
大模型 API 封装（可选，缺配置时优雅降级）
==========================================
遵循 PRD §7.4 三层漏斗：大模型只处理高变化研报（评级/目标价显著变化、首次覆盖），
绝大多数研报仅保留结构化数据走纯规则共识。

配置来源（任选其一，优先级：环境变量 > 配置文件）：
  - 环境变量：AI_API_KEY / AI_API_BASE / AI_MODEL
  - 配置文件：config/ai_config.json（已被 .gitignore 忽略，不入库）

API 走 OpenAI 兼容协议，使用标准库 urllib，不引入额外依赖。
未配置时所有调用方应走「规则/正则兜底」，本模块返回 configured=False。
"""

import os
import json
import urllib.request
import urllib.error
from typing import Optional, Dict

from config.logger import get_logger
from config.settings import PROJECT_ROOT

logger = get_logger(__name__)

CONFIG_PATH = PROJECT_ROOT / "config" / "ai_config.json"

DEFAULT_MODEL = "gpt-4o-mini"


def _load_config() -> Dict:
    cfg = {}
    # 1) 配置文件
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:  # pragma: no cover
            logger.warning(f"读取 ai_config.json 失败: {e}")
    # 2) 环境变量覆盖
    for k in ("AI_API_KEY", "AI_API_BASE", "AI_MODEL"):
        if os.environ.get(k):
            cfg[k.lower().replace("ai_", "")] = os.environ[k]
    return cfg


def is_configured() -> bool:
    """是否已配置可用的 API Key。"""
    return bool(_load_config().get("api_key"))


def chat(prompt: str, system: str = "", temperature: float = 0.2,
         max_tokens: int = 600) -> Dict:
    """调用大模型，返回 {configured, text, error}。

    未配置或调用失败时 text 为空、error 有说明，调用方据此走兜底。
    """
    cfg = _load_config()
    api_key = cfg.get("api_key")
    if not api_key:
        return {"configured": False, "text": "", "error": "未配置 AI_API_KEY"}
    api_base = cfg.get("api_base", "https://api.openai.com/v1").rstrip("/")
    model = cfg.get("model", DEFAULT_MODEL)

    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": (
            ([{"role": "system", "content": system}] if system else []) +
            [{"role": "user", "content": prompt}]
        ),
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{api_base}/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"]
        return {"configured": True, "text": text, "error": None}
    except urllib.error.URLError as e:
        return {"configured": True, "text": "", "error": f"网络错误: {e}"}
    except Exception as e:  # pragma: no cover
        return {"configured": True, "text": "", "error": f"调用失败: {e}"}


def extract_json_block(text: str) -> Optional[dict]:
    """从模型返回中尽量解析出 JSON（容忍代码块包裹）。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    try:
        return json.loads(s)
    except Exception:
        # 退而求其次：截取第一个 { 到最后一个 }
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b != -1 and b > a:
            try:
                return json.loads(s[a:b + 1])
            except Exception:
                return None
        return None

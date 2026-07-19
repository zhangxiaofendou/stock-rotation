"""统一通知分发服务。

设计要点（PRD 5.6 / 阶段 D）：
- 只分发摘要与链接，渠道凭据独立存放（config/notification.json），不进入页面代码；
- 被盘后报告、数据更新失败、熔断风险、持仓止损、信号失效等场景统一复用，
  避免多个模块各自发消息；
- 任何渠道未配置或发送失败时，仅记录并返回状态，不抛异常阻断主流程；
- 发送走标准库 urllib，无第三方依赖。

config/notification.json 结构示例：
{
  "serverchan": {"sendkey": "SCTxxxxxxxx"},
  "wecom":     {"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"},
  "email":     {"smtp_host": "smtp.xxx.com", "smtp_port": 465, "user": "a@b.com",
               "password": "******", "to": "c@d.com", "tls": true}
}
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Dict, List, Optional

from config.logger import get_logger
from config.settings import PARQUET_DIR

logger = get_logger(__name__)

# 配置与订阅文件路径
_CFG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "notification.json"
)
_SUBS_PATH = os.path.join(os.path.dirname(str(PARQUET_DIR)), "notification_subs.json")

# 可订阅事件（默认全部开启）
DEFAULT_EVENTS = {
    "report_generated": True,   # 盘后报告生成
    "data_failure": True,       # 数据更新失败
    "circuit_breaker": True,    # 市场进入防御/熔断
    "holdings_risk": True,      # 持仓触发减仓/止损/风险事项
    "signal_failure": False,    # 信号失效预警
}

EVENT_LABELS = {
    "report_generated": "盘后报告生成",
    "data_failure": "数据更新失败",
    "circuit_breaker": "市场熔断/防御",
    "holdings_risk": "持仓风险事项",
    "signal_failure": "信号失效预警",
}

_CHANNEL_LABELS = {
    "serverchan": "Server酱",
    "wecom": "企业微信机器人",
    "email": "邮件",
}


class NotificationService:
    """轻量通知分发器。"""

    def __init__(self, cfg_path: str = _CFG_PATH, subs_path: str = _SUBS_PATH):
        self.cfg_path = cfg_path
        self.subs_path = subs_path
        self._config = self._load_config()

    # ------------------------------------------------------------------ 配置
    def _load_config(self) -> dict:
        if not os.path.exists(self.cfg_path):
            return {}
        try:
            with open(self.cfg_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("读取通知配置失败: %s", e)
            return {}

    def configured_channels(self) -> List[str]:
        """返回已配置凭据的渠道名列表。"""
        out = []
        for ch in _CHANNEL_LABELS:
            cfg = self._config.get(ch)
            if isinstance(cfg, dict) and any(str(v).strip() for v in cfg.values()):
                out.append(ch)
        return out

    # -------------------------------------------------------------- 订阅开关
    def event_subscriptions(self) -> Dict[str, bool]:
        subs = dict(DEFAULT_EVENTS)
        if os.path.exists(self.subs_path):
            try:
                with open(self.subs_path, encoding="utf-8") as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    if k in subs:
                        subs[k] = bool(v)
            except Exception:
                pass
        return subs

    def set_subscription(self, event: str, enabled: bool) -> None:
        subs = self.event_subscriptions()
        subs[event] = bool(enabled)
        try:
            with open(self.subs_path, "w", encoding="utf-8") as f:
                json.dump(subs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存订阅设置失败: %s", e)

    def should_notify(self, event: str) -> bool:
        subs = self.event_subscriptions()
        if not subs.get(event, False):
            return False
        return len(self.configured_channels()) > 0

    # ----------------------------------------------------------------- 发送
    def send(self, title: str, summary: str, url: str = None,
             channels: List[str] = None) -> Dict[str, str]:
        """发送通知。返回 {渠道: 状态}。

        channels 省略时发往所有已配置渠道。任何失败仅记录，不抛异常。
        """
        targets = channels or self.configured_channels()
        if not targets:
            return {"__none__": "未配置任何通知渠道"}
        body = summary
        if url:
            body += f"\n\n详情：{url}"
        results: Dict[str, str] = {}
        for ch in targets:
            try:
                if ch == "serverchan":
                    results[ch] = self._send_serverchan(title, body)
                elif ch == "wecom":
                    results[ch] = self._send_wecom(title, body)
                elif ch == "email":
                    results[ch] = self._send_email(title, body)
                else:
                    results[ch] = "未知渠道"
            except Exception as e:
                results[ch] = f"失败：{e}"
                logger.warning("通知渠道 %s 发送失败: %s", ch, e)
        return results

    def notify_event(self, event: str, title: str, summary: str,
                     url: str = None) -> Optional[Dict[str, str]]:
        """按事件订阅判断是否发送；返回发送结果或 None。"""
        if not self.should_notify(event):
            return None
        return self.send(title, summary, url=url)

    # ----------------------------------------------------------- 渠道实现
    @staticmethod
    def _http_post_json(url: str, payload: dict, timeout: int = 10) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _send_serverchan(self, title: str, body: str) -> str:
        cfg = self._config.get("serverchan", {})
        sendkey = cfg.get("sendkey")
        if not sendkey:
            return "未配置 sendkey"
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        self._http_post_json(url, {"text": title, "desp": body.replace("\n", "\n\n")})
        return "已发送"

    def _send_wecom(self, title: str, body: str) -> str:
        cfg = self._config.get("wecom", {})
        webhook = cfg.get("webhook")
        if not webhook:
            return "未配置 webhook"
        content = f"**{title}**\n{body}"
        self._http_post_json(webhook, {"msgtype": "markdown", "markdown": {"content": content}})
        return "已发送"

    def _send_email(self, title: str, body: str) -> str:
        cfg = self._config.get("email", {})
        host = cfg.get("smtp_host")
        if not host:
            return "未配置 SMTP"
        try:
            import smtplib
            from email.mime.text import MIMEText
        except Exception as e:
            return f"email 模块不可用：{e}"
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = title
        msg["From"] = cfg.get("user", "")
        msg["To"] = cfg.get("to", "")
        port = int(cfg.get("smtp_port", 465))
        with smtplib.SMTP_SSL(host, port, timeout=10) as s:
            s.login(cfg.get("user", ""), cfg.get("password", ""))
            s.sendmail(cfg.get("user", ""), [cfg.get("to", "")], msg.as_string())
        return "已发送"


if __name__ == "__main__":
    svc = NotificationService()
    print("configured channels:", svc.configured_channels())
    print("subscriptions:", svc.event_subscriptions())

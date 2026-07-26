"""告警通知渠道适配器。"""

from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

import httpx


class AlertChannel(Protocol):
    """告警发送渠道协议。"""

    def send(self, subject: str, payload: dict[str, object]) -> None: ...


class LogChannel:
    """结构化日志告警渠道。"""

    def send(self, subject: str, payload: dict[str, object]) -> None:
        logging.getLogger("hl_mem.monitoring").warning("%s %s", subject, json.dumps(payload, ensure_ascii=False))


class WebhookChannel:
    """带超时的 JSON webhook 渠道。"""

    def __init__(self, url: str, timeout_seconds: float = 5.0) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds

    def send(self, subject: str, payload: dict[str, object]) -> None:
        response = httpx.post(self.url, json={"subject": subject, **payload}, timeout=self.timeout_seconds)
        response.raise_for_status()


class EmailChannel:
    """SMTP 邮件通知渠道。"""

    def __init__(self, host: str, port: int, sender: str, recipient: str, timeout_seconds: float = 10.0) -> None:
        self.host, self.port = host, port
        self.sender, self.recipient = sender, recipient
        self.timeout_seconds = timeout_seconds

    def send(self, subject: str, payload: dict[str, object]) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, self.recipient, subject
        message.set_content(json.dumps(payload, ensure_ascii=False, indent=2))
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds) as client:
            client.send_message(message)

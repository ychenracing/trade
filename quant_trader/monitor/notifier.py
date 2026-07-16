"""
通知模块 - 控制台 + 日志 + 可选webhook
"""
import datetime
import json
from typing import Optional

from utils.logger import log
from config.settings import NOTIFY_CONFIG


class Notifier:
    """消息通知"""

    def __init__(self):
        self.config = NOTIFY_CONFIG

    def send(self, title: str, message: str, level: str = "info"):
        """发送通知"""
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {title}: {message}"

        # 控制台
        if self.config.get("console", True):
            if level == "error":
                log.error(formatted)
            elif level == "warning":
                log.warning(formatted)
            else:
                log.info(formatted)

        # 日志文件（loguru已配置）

        # 企业微信
        webhook = self.config.get("wechat_webhook", "")
        if webhook:
            try:
                import requests  # 延迟导入，避免硬依赖
                resp = requests.post(
                    webhook,
                    json={
                        "msgtype": "text",
                        "text": {"content": formatted}
                    },
                    timeout=5,
                )
                if resp.status_code != 200:
                    log.warning(f"微信通知发送失败: {resp.status_code}")
            except ImportError:
                log.warning("requests未安装, 微信通知不可用。请 pip install requests")
            except Exception as e:
                log.warning(f"微信通知异常: {e}")

        # Telegram
        tg_token = self.config.get("telegram_token", "")
        tg_chat = self.config.get("telegram_chat_id", "")
        if tg_token and tg_chat:
            try:
                import requests  # 延迟导入
                url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
                resp = requests.post(url, json={
                    "chat_id": tg_chat,
                    "text": formatted,
                    "parse_mode": "HTML",
                }, timeout=5)
            except ImportError:
                log.warning("requests未安装, Telegram通知不可用。请 pip install requests")
            except Exception as e:
                log.warning(f"Telegram通知异常: {e}")

    def notify_trade(self, side: str, name: str, code: str,
                     price: float, shares: int, reason: str = ""):
        """交易通知"""
        emoji = "💰" if side == "buy" else "💸"
        msg = f"{emoji} {side.upper()} {name}({code}) {shares}股 @ {price:.2f}"
        if reason:
            msg += f"\n   原因: {reason}"
        self.send("交易信号", msg)

    def notify_risk(self, message: str, level: str = "warning"):
        """风控通知"""
        self.send("⚠️ 风控预警", message, level=level)

    def notify_daily_summary(self, summary: str):
        """每日收盘总结"""
        self.send("📊 收盘总结", summary)

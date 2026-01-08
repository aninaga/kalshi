"""
Alert Manager

Handles alerting via multiple channels: webhooks, Telegram, email.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Callable

import aiohttp

logger = logging.getLogger(__name__)


class AlertLevel(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class Alert:
    """Represents an alert."""
    id: str
    level: AlertLevel
    title: str
    message: str
    data: Optional[Dict]
    timestamp: datetime
    sent_channels: List[str]


class AlertManager:
    """
    Multi-channel alert manager.

    Supports:
    - Webhook (Discord, Slack, custom)
    - Telegram
    - Console logging
    """

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        min_alert_level: AlertLevel = AlertLevel.INFO,
    ):
        self.webhook_url = webhook_url or os.getenv("ALERT_WEBHOOK_URL")
        self.telegram_token = telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.min_alert_level = min_alert_level

        self._session: Optional[aiohttp.ClientSession] = None
        self._alert_counter = 0

        # Rate limiting
        self._alert_counts: Dict[str, List[datetime]] = {}
        self._rate_limit_window = 60  # seconds
        self._rate_limit_max = 10  # max alerts per window

        # Alert history
        self._recent_alerts: List[Alert] = []
        self._max_history = 100

        logger.info("AlertManager initialized")

    async def initialize(self):
        """Initialize alert manager."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )

    async def close(self):
        """Close alert manager."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _should_send(self, level: AlertLevel) -> bool:
        """Check if alert level meets minimum threshold."""
        levels = list(AlertLevel)
        return levels.index(level) >= levels.index(self.min_alert_level)

    def _check_rate_limit(self, key: str) -> bool:
        """Check if we're within rate limits."""
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - self._rate_limit_window

        # Clean old entries
        if key in self._alert_counts:
            self._alert_counts[key] = [
                t for t in self._alert_counts[key]
                if t.timestamp() > cutoff
            ]
        else:
            self._alert_counts[key] = []

        # Check limit
        if len(self._alert_counts[key]) >= self._rate_limit_max:
            return False

        self._alert_counts[key].append(now)
        return True

    async def send(
        self,
        level: AlertLevel,
        title: str,
        message: str,
        data: Optional[Dict] = None,
    ) -> Optional[Alert]:
        """
        Send an alert to all configured channels.

        Args:
            level: Alert severity level
            title: Short title
            message: Detailed message
            data: Optional structured data

        Returns:
            Alert object if sent, None if filtered/rate-limited
        """
        if not self._should_send(level):
            return None

        # Rate limit by level
        if not self._check_rate_limit(level.value):
            logger.warning(f"Alert rate limited: {title}")
            return None

        self._alert_counter += 1
        alert = Alert(
            id=f"alert_{self._alert_counter:06d}",
            level=level,
            title=title,
            message=message,
            data=data,
            timestamp=datetime.now(timezone.utc),
            sent_channels=[],
        )

        # Send to all channels
        tasks = []

        # Console (always)
        self._log_alert(alert)
        alert.sent_channels.append("console")

        # Webhook
        if self.webhook_url:
            tasks.append(self._send_webhook(alert))

        # Telegram
        if self.telegram_token and self.telegram_chat_id:
            tasks.append(self._send_telegram(alert))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"Alert channel failed: {result}")

        # Store in history
        self._recent_alerts.append(alert)
        if len(self._recent_alerts) > self._max_history:
            self._recent_alerts.pop(0)

        return alert

    def _log_alert(self, alert: Alert):
        """Log alert to console."""
        level_map = {
            AlertLevel.DEBUG: logging.DEBUG,
            AlertLevel.INFO: logging.INFO,
            AlertLevel.WARNING: logging.WARNING,
            AlertLevel.ERROR: logging.ERROR,
            AlertLevel.CRITICAL: logging.CRITICAL,
        }

        log_level = level_map.get(alert.level, logging.INFO)
        logger.log(log_level, f"[ALERT] {alert.title}: {alert.message}")

    async def _send_webhook(self, alert: Alert) -> bool:
        """Send alert to webhook (Discord/Slack compatible)."""
        if not self._session:
            await self.initialize()

        try:
            # Format for Discord/Slack
            color_map = {
                AlertLevel.DEBUG: 0x808080,
                AlertLevel.INFO: 0x0099FF,
                AlertLevel.WARNING: 0xFFCC00,
                AlertLevel.ERROR: 0xFF6600,
                AlertLevel.CRITICAL: 0xFF0000,
            }

            payload = {
                "embeds": [{
                    "title": f"[{alert.level.value.upper()}] {alert.title}",
                    "description": alert.message,
                    "color": color_map.get(alert.level, 0x0099FF),
                    "timestamp": alert.timestamp.isoformat(),
                    "fields": [],
                }]
            }

            # Add data fields
            if alert.data:
                for key, value in list(alert.data.items())[:5]:
                    payload["embeds"][0]["fields"].append({
                        "name": key,
                        "value": str(value)[:100],
                        "inline": True,
                    })

            async with self._session.post(
                self.webhook_url,
                json=payload
            ) as response:
                if response.status in (200, 204):
                    alert.sent_channels.append("webhook")
                    return True
                else:
                    logger.error(f"Webhook failed: {response.status}")
                    return False

        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return False

    async def _send_telegram(self, alert: Alert) -> bool:
        """Send alert to Telegram."""
        if not self._session:
            await self.initialize()

        try:
            level_emoji = {
                AlertLevel.DEBUG: "🔍",
                AlertLevel.INFO: "ℹ️",
                AlertLevel.WARNING: "⚠️",
                AlertLevel.ERROR: "❌",
                AlertLevel.CRITICAL: "🚨",
            }

            emoji = level_emoji.get(alert.level, "📢")
            text = f"{emoji} *{alert.title}*\n\n{alert.message}"

            if alert.data:
                text += "\n\n```\n"
                for key, value in list(alert.data.items())[:5]:
                    text += f"{key}: {value}\n"
                text += "```"

            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            payload = {
                "chat_id": self.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }

            async with self._session.post(url, json=payload) as response:
                if response.status == 200:
                    alert.sent_channels.append("telegram")
                    return True
                else:
                    logger.error(f"Telegram failed: {response.status}")
                    return False

        except Exception as e:
            logger.error(f"Telegram error: {e}")
            return False

    # Convenience methods
    async def info(self, title: str, message: str, data: Optional[Dict] = None):
        """Send info alert."""
        return await self.send(AlertLevel.INFO, title, message, data)

    async def warning(self, title: str, message: str, data: Optional[Dict] = None):
        """Send warning alert."""
        return await self.send(AlertLevel.WARNING, title, message, data)

    async def error(self, title: str, message: str, data: Optional[Dict] = None):
        """Send error alert."""
        return await self.send(AlertLevel.ERROR, title, message, data)

    async def critical(self, title: str, message: str, data: Optional[Dict] = None):
        """Send critical alert."""
        return await self.send(AlertLevel.CRITICAL, title, message, data)

    async def opportunity_found(
        self,
        market: str,
        profit_pct: float,
        arb_type: str,
    ):
        """Send alert for arbitrage opportunity."""
        await self.send(
            AlertLevel.INFO if profit_pct < 5 else AlertLevel.WARNING,
            f"Arbitrage: {profit_pct:.2f}%",
            f"Opportunity in {market[:50]}",
            {
                "type": arb_type,
                "profit": f"{profit_pct:.2f}%",
                "market": market[:80],
            }
        )

    async def execution_complete(
        self,
        execution_id: str,
        status: str,
        profit: float,
    ):
        """Send alert for execution completion."""
        level = AlertLevel.INFO if status == "success" else AlertLevel.ERROR
        await self.send(
            level,
            f"Execution: {status.upper()}",
            f"Trade {execution_id} completed",
            {
                "execution_id": execution_id,
                "status": status,
                "profit": f"${profit:.4f}",
            }
        )

    def get_recent_alerts(self, limit: int = 20) -> List[Alert]:
        """Get recent alerts."""
        return self._recent_alerts[-limit:]

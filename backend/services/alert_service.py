"""Alert service for critical system events.

Sends alerts via logging, Slack webhook, and email when configured.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class AlertService:
    @staticmethod
    async def send_alert(
        level: str, message: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        logger.critical("ALERT [%s]: %s | metadata=%s", level, message, metadata)

        from backend.config import Config

        if Config.SLACK_WEBHOOK_URL:
            try:
                import httpx

                fields = [{"title": k, "value": str(v)} for k, v in (metadata or {}).items()]
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        Config.SLACK_WEBHOOK_URL,
                        json={
                            "text": f"[{level}] {message}",
                            "attachments": [{"fields": fields}] if fields else [],
                        },
                    )
            except Exception:
                logger.exception("failed to send Slack alert")

        if Config.ALERT_EMAIL:
            try:
                from backend.services.email_service import EmailService

                body_parts = [f"Level: {level}", f"Message: {message}"]
                if metadata:
                    body_parts.append("Metadata:")
                    for k, v in metadata.items():
                        body_parts.append(f"  {k}: {v}")
                await EmailService.send(
                    Config.ALERT_EMAIL,
                    f"[ALERT] {level}: {message}",
                    "\n".join(body_parts),
                )
            except Exception:
                logger.exception("failed to send alert email")

    @staticmethod
    def send_alert_sync(
        level: str, message: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        logger.critical("ALERT [%s]: %s | metadata=%s", level, message, metadata)

        from backend.config import Config

        if Config.SLACK_WEBHOOK_URL:
            try:
                import httpx

                fields = [{"title": k, "value": str(v)} for k, v in (metadata or {}).items()]
                with httpx.Client(timeout=10) as client:
                    client.post(
                        Config.SLACK_WEBHOOK_URL,
                        json={
                            "text": f"[{level}] {message}",
                            "attachments": [{"fields": fields}] if fields else [],
                        },
                    )
            except Exception:
                logger.exception("failed to send Slack alert (sync)")

        if Config.ALERT_EMAIL:
            try:
                from backend.services.email_service import EmailService
                import asyncio

                body_parts = [f"Level: {level}", f"Message: {message}"]
                if metadata:
                    body_parts.append("Metadata:")
                    for k, v in metadata.items():
                        body_parts.append(f"  {k}: {v}")
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    loop.create_task(
                        EmailService.send(
                            Config.ALERT_EMAIL,
                            f"[ALERT] {level}: {message}",
                            "\n".join(body_parts),
                        )
                    )
                else:
                    asyncio.run(
                        EmailService.send(
                            Config.ALERT_EMAIL,
                            f"[ALERT] {level}: {message}",
                            "\n".join(body_parts),
                        )
                    )
            except Exception:
                logger.exception("failed to send alert email (sync)")

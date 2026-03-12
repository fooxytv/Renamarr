"""Discord webhook notifications."""

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Sends notifications to Discord via webhook."""

    def __init__(self, webhook_url: str | None = None):
        self.webhook_url = webhook_url or os.environ.get("RENAMARR_DISCORD_WEBHOOK")
        self._enabled = bool(self.webhook_url)
        if not self._enabled:
            logger.info("Discord notifications disabled (no webhook URL configured)")

    async def _send(self, embeds: list[dict]) -> None:
        """Send a message to Discord."""
        if not self._enabled:
            return

        payload = {
            "username": "Renamarr",
            "embeds": embeds,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self.webhook_url, json=payload)
                if response.status_code == 204:
                    logger.debug("Discord notification sent")
                else:
                    logger.warning(f"Discord webhook returned {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to send Discord notification: {e}")

    async def scan_completed(
        self,
        total_files: int,
        movies: int,
        tv: int,
        duplicates: int,
        pending: int,
        already_correct: int,
    ) -> None:
        """Notify that a scan has completed."""
        embed = {
            "title": "Scan Complete",
            "color": 3447003,  # Blue
            "fields": [
                {"name": "Total Files", "value": str(total_files), "inline": True},
                {"name": "Movies", "value": str(movies), "inline": True},
                {"name": "TV Episodes", "value": str(tv), "inline": True},
                {"name": "Need Renaming", "value": str(pending), "inline": True},
                {"name": "Duplicates Found", "value": str(duplicates), "inline": True},
                {"name": "Already Correct", "value": str(already_correct), "inline": True},
            ],
        }
        if pending > 0:
            embed["description"] = f"{pending} files ready for review in the web UI."

        await self._send([embed])

    async def execute_completed(
        self,
        renamed: int,
        failed: int,
        errors: list[str] | None = None,
    ) -> None:
        """Notify that renames have been executed."""
        if renamed == 0 and failed == 0:
            return

        color = 3066993 if failed == 0 else 15158332  # Green or Red
        embed = {
            "title": "Renames Executed",
            "color": color,
            "fields": [
                {"name": "Renamed", "value": str(renamed), "inline": True},
                {"name": "Failed", "value": str(failed), "inline": True},
            ],
        }

        if errors:
            error_text = "\n".join(errors[:5])
            if len(errors) > 5:
                error_text += f"\n...and {len(errors) - 5} more"
            embed["fields"].append({"name": "Errors", "value": f"```{error_text}```"})

        await self._send([embed])

    async def scan_failed(self, error: str) -> None:
        """Notify that a scan failed."""
        embed = {
            "title": "Scan Failed",
            "color": 15158332,  # Red
            "description": f"```{error}```",
        }
        await self._send([embed])

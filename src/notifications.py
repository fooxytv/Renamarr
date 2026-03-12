"""Discord webhook notifications."""

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Sends notifications to Discord via webhook."""

    def __init__(self, webhook_url: str | None = None, web_url: str | None = None):
        self.webhook_url = webhook_url or os.environ.get("RENAMARR_DISCORD_WEBHOOK")
        self.web_url = web_url or os.environ.get("RENAMARR_WEB_URL")
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
        pending_movies: int = 0,
        pending_tv: int = 0,
        duration_seconds: float | None = None,
    ) -> None:
        """Notify that a scan has completed."""
        embed = {
            "title": "Scan Complete",
            "color": 3447003,  # Blue
            "fields": [
                {"name": "Total Files", "value": str(total_files), "inline": True},
                {"name": "Movies", "value": str(movies), "inline": True},
                {"name": "TV Episodes", "value": str(tv), "inline": True},
                {"name": "Need Renaming", "value": f"{pending} ({pending_movies} movies, {pending_tv} TV)", "inline": True},
                {"name": "Duplicates Found", "value": str(duplicates), "inline": True},
                {"name": "Already Correct", "value": str(already_correct), "inline": True},
            ],
        }

        description_parts = []
        if pending > 0:
            if self.web_url:
                description_parts.append(f"[{pending} files ready for review]({self.web_url})")
            else:
                description_parts.append(f"{pending} files ready for review in the web UI.")

        if description_parts:
            embed["description"] = "\n".join(description_parts)

        if duration_seconds is not None:
            minutes = int(duration_seconds // 60)
            seconds = int(duration_seconds % 60)
            duration_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
            embed["footer"] = {"text": f"Scan completed in {duration_str}"}

        await self._send([embed])

    async def execute_completed(
        self,
        renamed: int,
        failed: int,
        errors: list[str] | None = None,
        renames: list[dict] | None = None,
        moved_to_trash: int = 0,
    ) -> None:
        """Notify that renames have been executed.

        renames: list of {"source": str, "destination": str, "media_type": str}
        """
        if renamed == 0 and failed == 0 and moved_to_trash == 0:
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

        if moved_to_trash > 0:
            embed["fields"].append({"name": "Moved to Trash", "value": str(moved_to_trash), "inline": True})

        # Build before → after code blocks grouped by type
        if renames:
            movie_renames = [r for r in renames if r["media_type"] == "movie"]
            tv_renames = [r for r in renames if r["media_type"] != "movie"]

            rename_parts = []

            if movie_renames:
                lines = []
                for r in movie_renames[:10]:
                    lines.append(f"{r['source']} → {r['destination']}")
                if len(movie_renames) > 10:
                    lines.append(f"...and {len(movie_renames) - 10} more")
                rename_parts.append({"name": "Movies", "value": f"```\n" + "\n".join(lines) + "\n```"})

            if tv_renames:
                lines = []
                for r in tv_renames[:10]:
                    lines.append(f"{r['source']} → {r['destination']}")
                if len(tv_renames) > 10:
                    lines.append(f"...and {len(tv_renames) - 10} more")
                rename_parts.append({"name": "TV", "value": f"```\n" + "\n".join(lines) + "\n```"})

            embed["fields"].extend(rename_parts)

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

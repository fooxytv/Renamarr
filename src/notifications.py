"""Discord webhook notifications."""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Sends notifications to Discord via webhook."""

    ALLOWED_WEBHOOK_PREFIXES = (
        "https://discord.com/api/webhooks/",
        "https://discordapp.com/api/webhooks/",
    )

    def __init__(self, webhook_url: str | None = None, web_url: str | None = None):
        url = webhook_url or os.environ.get("RENAMARR_DISCORD_WEBHOOK")

        # Validate webhook URL to prevent SSRF
        if url and not any(url.startswith(prefix) for prefix in self.ALLOWED_WEBHOOK_PREFIXES):
            logger.error(f"Discord webhook URL rejected: must start with {self.ALLOWED_WEBHOOK_PREFIXES[0]}")
            url = None

        self.webhook_url = url
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

    def _strip_html(self, text: str | None) -> str:
        """Strip HTML tags from API summaries."""
        if not text:
            return ""
        import re
        clean = re.sub(r'<[^>]+>', '', text)
        # Truncate long descriptions
        if len(clean) > 200:
            clean = clean[:197] + "..."
        return clean

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
        """Notify that renames have been executed — one embed per file.

        renames: list of {"source": str, "destination": str, "media_type": str,
                          "title": str, "year": int|None, "poster": str|None,
                          "plot": str|None, "confidence": int}
        """
        if renamed == 0 and failed == 0 and moved_to_trash == 0:
            return

        # Send individual notifications for each rename
        if renames:
            for r in renames:
                embed = {
                    "title": r.get("title") or r["destination"],
                    "color": 3066993,  # Green
                    "fields": [
                        {"name": "Renamed", "value": f"`{r['source']}`\n→ `{r['destination']}`"},
                    ],
                }

                year = r.get("year")
                if year:
                    embed["title"] += f" ({year})"

                plot = self._strip_html(r.get("plot"))
                if plot:
                    embed["description"] = plot

                poster = r.get("poster")
                if poster:
                    embed["thumbnail"] = {"url": poster}

                confidence = r.get("confidence", 0)
                if confidence > 0:
                    embed["fields"].append(
                        {"name": "Confidence", "value": f"{confidence}%", "inline": True}
                    )

                embed["fields"].append(
                    {"name": "Type", "value": "Movie" if r["media_type"] == "movie" else "TV", "inline": True}
                )

                embed["footer"] = {"text": "Rename completed"}

                await self._send([embed])
                await asyncio.sleep(0.5)  # Rate limit between messages

        # Summary if there were failures
        if failed > 0 or moved_to_trash > 0:
            summary = {
                "title": "Rename Summary",
                "color": 15158332 if failed > 0 else 3066993,
                "fields": [
                    {"name": "Renamed", "value": str(renamed), "inline": True},
                    {"name": "Failed", "value": str(failed), "inline": True},
                ],
            }
            if moved_to_trash > 0:
                summary["fields"].append(
                    {"name": "Moved to Trash", "value": str(moved_to_trash), "inline": True}
                )
            if errors:
                error_text = "\n".join(errors[:5])
                if len(errors) > 5:
                    error_text += f"\n...and {len(errors) - 5} more"
                summary["fields"].append({"name": "Errors", "value": f"```{error_text}```"})
            await self._send([summary])

    async def library_cleanup_completed(
        self,
        merged: int,
        moved_files: int,
        failed: int,
        errors: list[str] | None = None,
        renamed: int = 0,
    ) -> None:
        """Notify that library folder merges/renames have been executed."""
        if merged == 0 and failed == 0 and renamed == 0:
            return

        color = 3066993 if failed == 0 else 15158332
        embed = {
            "title": "Library Cleanup Complete",
            "color": color,
            "fields": [
                {"name": "Folders Merged", "value": str(merged), "inline": True},
                {"name": "Folders Renamed", "value": str(renamed), "inline": True},
                {"name": "Files Moved", "value": str(moved_files), "inline": True},
                {"name": "Failed", "value": str(failed), "inline": True},
            ],
        }

        if errors:
            error_text = "\n".join(errors[:5])
            if len(errors) > 5:
                error_text += f"\n...and {len(errors) - 5} more"
            embed["fields"].append({"name": "Errors", "value": f"```{error_text}```"})

        await self._send([embed])

    async def review_needed(
        self,
        files: list[dict],
    ) -> None:
        """Notify about files needing manual review — one embed per file.

        files: list of {"filename": str, "title": str, "confidence": int,
                        "file_id": str, "media_type": str, "year": int|None,
                        "poster": str|None, "plot": str|None,
                        "destination": str|None}
        """
        if not files:
            return

        for f in files:
            embed = {
                "title": f.get("title") or f["filename"],
                "color": 16750848,  # Orange
                "fields": [],
            }

            year = f.get("year")
            if year:
                embed["title"] += f" ({year})"

            # Plot/description
            plot = self._strip_html(f.get("plot"))
            if plot:
                embed["description"] = plot

            # Poster thumbnail
            poster = f.get("poster")
            if poster:
                embed["thumbnail"] = {"url": poster}

            # File info
            embed["fields"].append({
                "name": "File",
                "value": f"`{f['filename']}`",
            })

            destination = f.get("destination")
            if destination:
                embed["fields"].append({
                    "name": "Would rename to",
                    "value": f"`{destination}`",
                })

            embed["fields"].append({
                "name": "Confidence",
                "value": f"**{f['confidence']}%**",
                "inline": True,
            })
            embed["fields"].append({
                "name": "Type",
                "value": "Movie" if f["media_type"] == "movie" else "TV",
                "inline": True,
            })

            # Action links
            if self.web_url:
                file_id = f.get("file_id", "")
                links = (
                    f"[Approve]({self.web_url}/api/files/{file_id}/approve) | "
                    f"[Reject]({self.web_url}/api/files/{file_id}/reject) | "
                    f"[Open Renamarr]({self.web_url})"
                )
                embed["fields"].append({"name": "Actions", "value": links})

            embed["footer"] = {"text": "Review needed — low confidence match"}

            await self._send([embed])
            await asyncio.sleep(0.5)  # Rate limit between messages

    async def auto_approved(
        self,
        count: int,
        files: list[dict] | None = None,
    ) -> None:
        """Notify about files that were auto-approved — one embed per file.

        files: list of {"filename": str, "title": str, "confidence": int,
                        "year": int|None, "poster": str|None, "plot": str|None,
                        "destination": str|None, "media_type": str}
        """
        if count == 0:
            return

        if files:
            for f in files:
                embed = {
                    "title": f.get("title") or f["filename"],
                    "color": 3066993,  # Green
                    "fields": [],
                }

                year = f.get("year")
                if year:
                    embed["title"] += f" ({year})"

                plot = self._strip_html(f.get("plot"))
                if plot:
                    embed["description"] = plot

                poster = f.get("poster")
                if poster:
                    embed["thumbnail"] = {"url": poster}

                embed["fields"].append({
                    "name": "File",
                    "value": f"`{f['filename']}`",
                })

                destination = f.get("destination")
                if destination:
                    embed["fields"].append({
                        "name": "Will rename to",
                        "value": f"`{destination}`",
                    })

                embed["fields"].append({
                    "name": "Confidence",
                    "value": f"**{f['confidence']}%**",
                    "inline": True,
                })
                embed["fields"].append({
                    "name": "Type",
                    "value": "Movie" if f.get("media_type") == "movie" else "TV",
                    "inline": True,
                })

                if self.web_url:
                    embed["fields"].append({
                        "name": "Review",
                        "value": f"[Open Renamarr]({self.web_url})",
                    })

                embed["footer"] = {"text": "Auto-approved — high confidence match"}

                await self._send([embed])
                await asyncio.sleep(0.5)

    async def scan_failed(self, error: str) -> None:
        """Notify that a scan failed."""
        embed = {
            "title": "Scan Failed",
            "color": 15158332,  # Red
            "description": f"```{error}```",
        }
        await self._send([embed])

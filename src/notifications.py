"""Discord webhook notifications with built-in rate-limit queue."""

import asyncio
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

# Max individual rich embeds before switching to a summary
MAX_INDIVIDUAL_EMBEDS = 10

# Discord allows ~30 requests/minute per webhook. We stay under at 25/min.
DISCORD_RATE_LIMIT = 25
DISCORD_RATE_WINDOW = 60.0


class DiscordNotifier:
    """Sends notifications to Discord via webhook with rate-limit queue."""

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

        # Rate limiting: track send timestamps
        self._send_times: list[float] = []
        # Async queue for outbound messages
        self._queue: asyncio.Queue[list[dict]] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the background queue worker. Call from an async context."""
        if self._enabled and self._worker_task is None:
            self._worker_task = asyncio.create_task(self._queue_worker())
            logger.debug("Discord notification queue worker started")

    async def stop(self) -> None:
        """Drain the queue and stop the worker."""
        if self._worker_task:
            # Signal worker to stop
            await self._queue.put(None)  # type: ignore[arg-type]
            try:
                await asyncio.wait_for(self._worker_task, timeout=30)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
            self._worker_task = None

    async def _queue_worker(self) -> None:
        """Background worker that drains the queue respecting rate limits."""
        while True:
            embeds = await self._queue.get()
            if embeds is None:
                # Drain remaining items before stopping
                while not self._queue.empty():
                    remaining = self._queue.get_nowait()
                    if remaining is not None:
                        await self._send_now(remaining)
                break
            await self._send_now(embeds)
            self._queue.task_done()

    async def _wait_for_rate_limit(self) -> None:
        """Wait until we have room in the rate limit window."""
        now = time.monotonic()
        # Prune timestamps outside the window
        self._send_times = [t for t in self._send_times if now - t < DISCORD_RATE_WINDOW]
        if len(self._send_times) >= DISCORD_RATE_LIMIT:
            # Wait until the oldest timestamp falls out of the window
            wait = DISCORD_RATE_WINDOW - (now - self._send_times[0]) + 0.5
            logger.debug(f"Discord rate limit: waiting {wait:.1f}s")
            await asyncio.sleep(wait)
            self._send_times = [t for t in self._send_times if time.monotonic() - t < DISCORD_RATE_WINDOW]

    async def _send_now(self, embeds: list[dict]) -> None:
        """Send embeds immediately, respecting rate limits and retrying on 429."""
        if not self._enabled:
            return

        await self._wait_for_rate_limit()

        payload = {
            "username": "Renamarr",
            "embeds": embeds,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                for attempt in range(3):
                    response = await client.post(self.webhook_url, json=payload)
                    self._send_times.append(time.monotonic())
                    if response.status_code == 204:
                        logger.debug("Discord notification sent")
                        return
                    if response.status_code == 429:
                        retry_after = float(response.json().get("retry_after", 2))
                        logger.warning(f"Discord rate limited, retrying in {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue
                    logger.warning(f"Discord webhook returned {response.status_code}")
                    return
        except Exception as e:
            logger.error(f"Failed to send Discord notification: {e}")

    async def _send(self, embeds: list[dict]) -> None:
        """Queue embeds for sending. Falls back to direct send if worker not running."""
        if not self._enabled:
            return
        if self._worker_task and not self._worker_task.done():
            await self._queue.put(embeds)
        else:
            await self._send_now(embeds)

    def _strip_html(self, text: str | None) -> str:
        """Strip HTML tags from API summaries."""
        if not text:
            return ""
        import re
        clean = re.sub(r'<[^>]+>', '', text)
        if len(clean) > 200:
            clean = clean[:197] + "..."
        return clean

    def _build_file_embed(
        self, f: dict, color: int, footer_text: str, show_actions: bool = False
    ) -> dict:
        """Build a rich embed for a single file."""
        embed = {
            "title": f.get("title") or f.get("filename", "Unknown"),
            "color": color,
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
            "value": f"`{f.get('filename', '')}`",
        })

        destination = f.get("destination")
        if destination:
            label = "Renamed" if "completed" in footer_text.lower() else "Would rename to"
            embed["fields"].append({
                "name": label,
                "value": f"`{destination}`",
            })

        confidence = f.get("confidence", 0)
        if confidence > 0:
            embed["fields"].append({
                "name": "Confidence",
                "value": f"**{confidence}%**",
                "inline": True,
            })

        embed["fields"].append({
            "name": "Type",
            "value": "Movie" if f.get("media_type") == "movie" else "TV",
            "inline": True,
        })

        if show_actions and self.web_url:
            file_id = f.get("file_id", "")
            links = (
                f"[Approve]({self.web_url}/api/files/{file_id}/approve) | "
                f"[Reject]({self.web_url}/api/files/{file_id}/reject) | "
                f"[Open Renamarr]({self.web_url})"
            )
            embed["fields"].append({"name": "Actions", "value": links})
        elif self.web_url:
            embed["fields"].append({
                "name": "Review",
                "value": f"[Open Renamarr]({self.web_url})",
            })

        embed["footer"] = {"text": footer_text}
        return embed

    async def _send_file_notifications(
        self, files: list[dict], color: int, footer_text: str,
        summary_title: str, show_actions: bool = False,
    ) -> None:
        """Send individual embeds for the first N files, then a summary for the rest."""
        if not files:
            return

        # Send individual rich embeds for the first batch
        for f in files[:MAX_INDIVIDUAL_EMBEDS]:
            embed = self._build_file_embed(f, color, footer_text, show_actions)
            await self._send([embed])

        # Summary for the rest
        remaining = len(files) - MAX_INDIVIDUAL_EMBEDS
        if remaining > 0:
            summary = {
                "title": summary_title,
                "color": color,
                "description": f"**+{remaining} more file{'s' if remaining != 1 else ''}** not shown individually.",
            }
            if self.web_url:
                summary["description"] += f"\n[View all in Renamarr]({self.web_url})"
            lines = []
            for f in files[MAX_INDIVIDUAL_EMBEDS:MAX_INDIVIDUAL_EMBEDS + 20]:
                conf = f.get("confidence", 0)
                title = f.get("title") or f.get("filename", "?")
                lines.append(f"{f.get('filename', '?')} → {title} ({conf}%)")
            if remaining > 20:
                lines.append(f"...and {remaining - 20} more")
            summary["fields"] = [{
                "name": "Files",
                "value": "```\n" + "\n".join(lines) + "\n```",
            }]
            await self._send([summary])

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

        renames: list of {"source": str, "destination": str, "media_type": str,
                          "title": str, "year": int|None, "poster": str|None,
                          "plot": str|None, "confidence": int}
        """
        if renamed == 0 and failed == 0 and moved_to_trash == 0:
            return

        if renames:
            await self._send_file_notifications(
                files=renames,
                color=3066993,  # Green
                footer_text="Rename completed",
                summary_title=f"Renames Completed — {len(renames)} files",
            )

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
        """Notify about files needing manual review.

        First 10 get individual rich embeds with poster/plot/actions.
        The rest get a compact summary.
        """
        await self._send_file_notifications(
            files=files,
            color=16750848,  # Orange
            footer_text="Review needed — low confidence match",
            summary_title=f"Review Needed — {len(files)} files",
            show_actions=True,
        )

    async def auto_approved(
        self,
        count: int,
        files: list[dict] | None = None,
    ) -> None:
        """Notify about files that were auto-approved.

        First 10 get individual rich embeds. The rest get a compact summary.
        """
        if count == 0:
            return

        await self._send_file_notifications(
            files=files or [],
            color=3066993,  # Green
            footer_text="Auto-approved — high confidence match",
            summary_title=f"Auto-Approved — {count} files",
        )

    async def scan_failed(self, error: str) -> None:
        """Notify that a scan failed."""
        embed = {
            "title": "Scan Failed",
            "color": 15158332,  # Red
            "description": f"```{error}```",
        }
        await self._send([embed])

"""Discord bot for reaction-based file approve/reject/ignore."""

import asyncio
import logging
from typing import Callable, Awaitable

import discord

logger = logging.getLogger(__name__)

# Emoji -> status mapping
REACTION_MAP = {
    "\u2705": "approved",   # ✅
    "\u274c": "rejected",   # ❌
    "\U0001f507": "ignored",  # 🔇
}

# All emojis we add to messages
REACTION_EMOJIS = list(REACTION_MAP.keys())


class DiscordReactionBot(discord.Client):
    """Discord bot that sends review messages and handles reaction-based decisions."""

    def __init__(self, channel_id: int, action_callback: Callable[[str, str], Awaitable[bool]]):
        intents = discord.Intents.default()
        intents.guild_message_reactions = True
        intents.message_content = False
        super().__init__(intents=intents)

        self.channel_id = channel_id
        self.action_callback = action_callback
        # message_id -> file_id mapping for tracked review messages
        self._tracked_messages: dict[int, str] = {}
        self._channel: discord.TextChannel | None = None
        self._ready_event = asyncio.Event()

    async def on_ready(self) -> None:
        logger.info(f"Discord bot connected as {self.user}")
        channel = self.get_channel(self.channel_id)
        if channel and isinstance(channel, discord.TextChannel):
            self._channel = channel
            logger.info(f"Discord bot bound to channel: #{channel.name}")
        else:
            logger.error(f"Discord bot: channel {self.channel_id} not found or not a text channel")
        self._ready_event.set()

    async def wait_until_ready_with_timeout(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return self._channel is not None
        except asyncio.TimeoutError:
            logger.error("Discord bot failed to connect within timeout")
            return False

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        # Ignore our own reactions
        if payload.user_id == self.user.id:
            return

        # Only process reactions on tracked messages
        file_id = self._tracked_messages.get(payload.message_id)
        if not file_id:
            return

        emoji = str(payload.emoji)
        status = REACTION_MAP.get(emoji)
        if not status:
            return

        logger.info(f"Discord reaction {emoji} on file {file_id} -> {status}")

        success = await self.action_callback(file_id, status)
        if success:
            # Remove other reaction emojis to show which action was taken
            if self._channel:
                try:
                    message = await self._channel.fetch_message(payload.message_id)
                    for reaction_emoji in REACTION_EMOJIS:
                        if reaction_emoji != emoji:
                            await message.remove_reaction(reaction_emoji, self.user)
                except discord.errors.NotFound:
                    pass
                except Exception as e:
                    logger.warning(f"Failed to clean up reactions: {e}")

            # Stop tracking this message
            del self._tracked_messages[payload.message_id]
            logger.info(f"File {file_id} {status} via Discord reaction")
        else:
            logger.warning(f"Failed to apply {status} to file {file_id}")

    async def send_review_embed(self, embed_dict: dict, file_id: str) -> None:
        """Send an embed to the channel and add reaction emojis for approve/reject/ignore."""
        if not self._channel:
            logger.warning("Discord bot: no channel available, skipping review embed")
            return

        try:
            embed = discord.Embed.from_dict(embed_dict)
            message = await self._channel.send(embed=embed)

            # Add reaction emojis
            for emoji in REACTION_EMOJIS:
                await message.add_reaction(emoji)

            # Track this message for reaction handling
            self._tracked_messages[message.id] = file_id
            logger.debug(f"Sent review embed for file {file_id}, message {message.id}")
        except Exception as e:
            logger.error(f"Failed to send Discord review embed: {e}")

    async def send_embed(self, embed_dict: dict) -> None:
        """Send an embed without reaction tracking (for non-review notifications)."""
        if not self._channel:
            return

        try:
            embed = discord.Embed.from_dict(embed_dict)
            await self._channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send Discord embed: {e}")

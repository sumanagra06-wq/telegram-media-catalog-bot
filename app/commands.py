from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from .config import Config

LOGGER = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="dashboard", description="Emergency dashboard repost"),
]

# The owner uses the same dashboard-first command menu. Reply-based /index remains
# an intentionally hidden operational recovery path for storage posts.
OWNER_COMMANDS = USER_COMMANDS


async def register_owner_commands(bot: Bot, owner_id: int) -> bool:
    try:
        await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
    except TelegramAPIError:
        # Telegram may not know the private chat until the owner has started the bot.
        LOGGER.warning(
            "Could not install owner-scoped commands for %s; /dashboard will retry it",
            owner_id,
            exc_info=True,
        )
        return False
    return True


async def register_commands(bot: Bot, config: Config) -> None:
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    for owner_id in config.owner_ids:
        await register_owner_commands(bot, owner_id)

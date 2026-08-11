from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from .config import Config

LOGGER = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Open the main dashboard"),
    BotCommand(command="menu", description="Open the main dashboard"),
    BotCommand(command="watchlist", description="Open your watchlist"),
    BotCommand(command="help", description="Show usage instructions"),
    BotCommand(command="cancel", description="Cancel the current operation"),
]

OWNER_COMMANDS = USER_COMMANDS + [
    BotCommand(command="admin", description="Open the Admin Panel"),
    BotCommand(command="categories", description="Manage storage categories"),
    BotCommand(command="category_add", description="Register a category channel"),
    BotCommand(command="index", description="Index a forwarded storage post"),
    BotCommand(command="files", description="View catalog file status"),
    BotCommand(command="failures", description="View indexing failures"),
    BotCommand(command="users", description="Manage bot users"),
    BotCommand(command="access_mode", description="Change user access mode"),
    BotCommand(command="stats", description="Show bot statistics"),
    BotCommand(command="db_status", description="Check Telegram databases"),
    BotCommand(command="backup", description="Export database backups"),
    BotCommand(command="audit", description="View administrative audit"),
    BotCommand(command="bot_settings", description="View system settings"),
]


async def register_owner_commands(bot: Bot, owner_id: int) -> bool:
    try:
        await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
    except TelegramAPIError:
        # Telegram may not know the private chat until the owner has started the bot.
        LOGGER.warning(
            "Could not install owner-scoped commands for %s; /start will retry it",
            owner_id,
            exc_info=True,
        )
        return False
    return True


async def register_commands(bot: Bot, config: Config) -> None:
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    for owner_id in config.owner_ids:
        await register_owner_commands(bot, owner_id)

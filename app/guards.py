from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, User
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Config
from .models import UserProfile, UserStatus
from .repositories import UserRepository
from .utils import safe_html

LOGGER = logging.getLogger(__name__)


async def ensure_registered(
    actor: User,
    users: UserRepository,
    config: Config,
    bot: Bot | None = None,
) -> tuple[UserProfile, bool]:
    profile, created = await users.ensure_user(
        user_id=actor.id,
        first_name=actor.first_name,
        last_name=actor.last_name,
        username=actor.username,
        language_code=actor.language_code,
        is_owner=config.is_owner(actor.id),
    )
    if created and profile.status == UserStatus.PENDING and bot is not None:
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="✅ Approve", callback_data=f"aus:{actor.id}:active"),
            InlineKeyboardButton(text="⛔ Ban", callback_data=f"aus:{actor.id}:banned"),
        )
        for owner_id in config.owner_ids:
            try:
                await bot.send_message(
                    owner_id,
                    "New access request\n\n"
                    f"Name: {safe_html(actor.full_name)}\n"
                    f"Username: @{safe_html(actor.username) if actor.username else 'None'}\n"
                    f"User ID: <code>{actor.id}</code>",
                    reply_markup=builder.as_markup(),
                )
            except TelegramAPIError:
                # An owner may not have started the bot yet. The pending list remains authoritative.
                LOGGER.info("Could not notify owner %s about a pending user", owner_id)
    return profile, created


def can_use_bot(profile: UserProfile, config: Config) -> bool:
    return config.is_owner(profile.telegram_user_id) or profile.status == UserStatus.ACTIVE


def access_denied_text(profile: UserProfile) -> str:
    if profile.status == UserStatus.PENDING:
        return "🕓 Your access request is waiting for owner approval."
    if profile.status == UserStatus.SUSPENDED:
        return "⏸ Your access to this bot is currently suspended."
    if profile.status == UserStatus.BANNED:
        return "⛔ You are not allowed to use this bot."
    return "⛔ Access denied."

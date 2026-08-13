from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

from .repositories import UserRepository
from .storage import StorageError

LOGGER = logging.getLogger(__name__)


def _message_is_unavailable(exc: TelegramBadRequest) -> bool:
    detail = str(exc).casefold()
    return any(
        marker in detail
        for marker in (
            "message to edit not found",
            "message_id_invalid",
            "message can't be edited",
            "message can not be edited",
        )
    )


class PanelManager:
    """Owns each user's pinned dashboard and single temporary workspace."""

    def __init__(
        self,
        bot: Bot,
        users: UserRepository,
        *,
        expiry_seconds: float = 300,
    ) -> None:
        self.bot = bot
        self.users = users
        self.expiry_seconds = expiry_seconds
        self._expiry_tasks: dict[int, asyncio.Task[None]] = {}
        self._user_locks: dict[int, asyncio.Lock] = {}

    def _user_lock(self, user_id: int) -> asyncio.Lock:
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    def is_dashboard(self, user_id: int, message_id: int | None) -> bool:
        profile = self.users.get_user(user_id)
        return bool(profile and profile.panel_dashboard_message_id == message_id)

    def is_workspace(self, user_id: int, message_id: int | None) -> bool:
        profile = self.users.get_user(user_id)
        return bool(profile and profile.panel_workspace_message_id == message_id)

    async def ensure_delivery_topic(self, user_id: int, *, replace: bool = False) -> int | None:
        """Return the user's permanent private-chat delivery topic, creating it once."""
        async with self._user_lock(user_id):
            profile = self.users.get_user(user_id)
            if profile is None:
                raise ValueError("User not found")
            if profile.delivery_topic_id is not None and not replace:
                return profile.delivery_topic_id
            try:
                bot_user = await self.bot.get_me()
            except TelegramAPIError:
                LOGGER.warning(
                    "Could not check private-topic support; delivering to General for user %s",
                    user_id,
                    exc_info=True,
                )
                return None
            if not bot_user.has_topics_enabled:
                LOGGER.warning(
                    "Private-chat topics are disabled for the bot; delivering to General for user %s",
                    user_id,
                )
                return None
            try:
                topic = await self.bot.create_forum_topic(
                    user_id,
                    "📦 Deliveries",
                    icon_color=7_322_096,
                )
            except TelegramAPIError:
                LOGGER.warning(
                    "Could not create delivery topic for user %s", user_id, exc_info=True
                )
                return None
            try:
                await self.users.set_delivery_topic(user_id, topic.message_thread_id)
            except (StorageError, ValueError):
                with suppress(TelegramAPIError):
                    await self.bot.delete_forum_topic(user_id, topic.message_thread_id)
                raise
            try:
                await self.bot.send_message(
                    user_id,
                    "📦 <b>DELIVERY INBOX</b>\n"
                    "<blockquote>Protected files stay organized in this topic.</blockquote>\n"
                    "Search and Watchlist controls remain in General. Delivered media is never "
                    "removed by automatic chat cleanup.",
                    message_thread_id=topic.message_thread_id,
                )
            except TelegramAPIError:
                LOGGER.info("Could not post delivery-topic introduction for user %s", user_id)
            return topic.message_thread_id

    async def reopen_delivery_topic(self, user_id: int, topic_id: int) -> bool:
        try:
            await self.bot.reopen_forum_topic(user_id, topic_id)
        except TelegramAPIError:
            return False
        return True

    async def ensure_dashboard(
        self,
        *,
        user_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup,
    ) -> int:
        async with self._user_lock(user_id):
            return await self._ensure_dashboard(
                user_id=user_id,
                text=text,
                reply_markup=reply_markup,
            )

    async def _ensure_dashboard(
        self,
        *,
        user_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup,
    ) -> int:
        profile = self.users.get_user(user_id)
        if profile is None:
            raise ValueError("User not found")
        message_id = profile.panel_dashboard_message_id
        if message_id is not None:
            try:
                await self.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    if not _message_is_unavailable(exc):
                        raise
                    with suppress(TelegramBadRequest, TelegramForbiddenError):
                        await self.bot.delete_message(user_id, message_id)
                    message_id = None
            except TelegramForbiddenError:
                message_id = None

        if message_id is None:
            message = await self.bot.send_message(user_id, text, reply_markup=reply_markup)
            message_id = message.message_id
            await self.users.set_panel_dashboard_message(user_id, message_id)

        try:
            await self.bot.pin_chat_message(
                user_id,
                message_id,
                disable_notification=True,
            )
        except TelegramAPIError:
            LOGGER.warning("Could not pin dashboard for user %s", user_id)
        return message_id

    async def repost_dashboard(
        self,
        *,
        user_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup,
    ) -> int:
        """Post a fresh pinned dashboard and retire the previous dashboard card."""
        async with self._user_lock(user_id):
            profile = self.users.get_user(user_id)
            if profile is None:
                raise ValueError("User not found")
            previous_id = profile.panel_dashboard_message_id
            message = await self.bot.send_message(user_id, text, reply_markup=reply_markup)
            message_id = message.message_id
            pinned = True
            try:
                await self.bot.pin_chat_message(
                    user_id,
                    message_id,
                    disable_notification=True,
                )
            except TelegramAPIError:
                pinned = False
                LOGGER.warning("Could not pin replacement dashboard for user %s", user_id)
            if not pinned and previous_id is not None:
                with suppress(TelegramBadRequest, TelegramForbiddenError):
                    await self.bot.delete_message(user_id, message_id)
                return previous_id
            try:
                await self.users.set_panel_dashboard_message(user_id, message_id)
            except StorageError:
                if pinned:
                    with suppress(TelegramAPIError):
                        await self.bot.unpin_chat_message(user_id, message_id=message_id)
                with suppress(TelegramBadRequest, TelegramForbiddenError):
                    await self.bot.delete_message(user_id, message_id)
                raise
            if previous_id is not None and previous_id != message_id:
                with suppress(TelegramAPIError):
                    await self.bot.unpin_chat_message(user_id, message_id=previous_id)
                with suppress(TelegramBadRequest, TelegramForbiddenError):
                    await self.bot.delete_message(user_id, previous_id)
            return message_id

    async def render_workspace(
        self,
        *,
        user_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None,
        create: bool = True,
    ) -> int | None:
        async with self._user_lock(user_id):
            return await self._render_workspace(
                user_id=user_id,
                text=text,
                reply_markup=reply_markup,
                create=create,
            )

    async def _render_workspace(
        self,
        *,
        user_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None,
        create: bool,
    ) -> int | None:
        profile = self.users.get_user(user_id)
        if profile is None:
            raise ValueError("User not found")
        message_id = profile.panel_workspace_message_id
        if message_id is not None:
            try:
                await self.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    if not _message_is_unavailable(exc):
                        raise
                    with suppress(TelegramBadRequest, TelegramForbiddenError):
                        await self.bot.delete_message(user_id, message_id)
                    await self.users.clear_panel_workspace_message(
                        user_id,
                        expected_message_id=message_id,
                    )
                    message_id = None
            except TelegramForbiddenError:
                await self.users.clear_panel_workspace_message(
                    user_id,
                    expected_message_id=message_id,
                )
                message_id = None

        if message_id is None and create:
            message = await self.bot.send_message(user_id, text, reply_markup=reply_markup)
            message_id = message.message_id
            await self.users.set_panel_workspace_message(user_id, message_id)
        if message_id is not None:
            self.touch(user_id, message_id)
        return message_id

    async def render_existing_workspace(
        self,
        *,
        user_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> bool:
        profile = self.users.get_user(user_id)
        if profile is None or profile.panel_workspace_message_id is None:
            return False
        rendered = await self.render_workspace(
            user_id=user_id,
            text=text,
            reply_markup=reply_markup,
            create=True,
        )
        return rendered is not None

    def touch(self, user_id: int, message_id: int) -> bool:
        if not self.is_workspace(user_id, message_id):
            return False
        previous = self._expiry_tasks.pop(user_id, None)
        if previous is not None:
            previous.cancel()
        self._expiry_tasks[user_id] = asyncio.create_task(
            self._expire_workspace(user_id, message_id)
        )
        return True

    async def _expire_workspace(self, user_id: int, message_id: int) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self.expiry_seconds)
            async with self._user_lock(user_id):
                if not self.is_workspace(user_id, message_id):
                    return
                try:
                    await self.bot.delete_message(user_id, message_id)
                except TelegramAPIError:
                    LOGGER.warning(
                        "Could not delete workspace %s for user %s; disabling its buttons",
                        message_id,
                        user_id,
                    )
                    try:
                        await self.bot.edit_message_reply_markup(
                            chat_id=user_id,
                            message_id=message_id,
                            reply_markup=None,
                        )
                    except TelegramAPIError:
                        LOGGER.info(
                            "Could not disable expired workspace %s for user %s",
                            message_id,
                            user_id,
                        )
                await self.users.clear_panel_workspace_message(
                    user_id,
                    expected_message_id=message_id,
                )
        finally:
            if self._expiry_tasks.get(user_id) is current_task:
                self._expiry_tasks.pop(user_id, None)

    async def close_workspace(self, user_id: int, message_id: int) -> bool:
        task = self._expiry_tasks.pop(user_id, None)
        if task is not None:
            task.cancel()
        async with self._user_lock(user_id):
            if not self.is_workspace(user_id, message_id):
                return False
            try:
                await self.bot.delete_message(user_id, message_id)
            except TelegramAPIError:
                try:
                    await self.bot.edit_message_reply_markup(
                        chat_id=user_id,
                        message_id=message_id,
                        reply_markup=None,
                    )
                except TelegramAPIError:
                    LOGGER.info(
                        "Could not disable closed workspace %s for user %s",
                        message_id,
                        user_id,
                    )
            return await self.users.clear_panel_workspace_message(
                user_id,
                expected_message_id=message_id,
            )

    async def close_current_workspace(self, user_id: int) -> bool:
        profile = self.users.get_user(user_id)
        if profile is None or profile.panel_workspace_message_id is None:
            return False
        return await self.close_workspace(user_id, profile.panel_workspace_message_id)

    async def cleanup_stale_workspaces(self) -> int:
        stale = [
            (profile.telegram_user_id, profile.panel_workspace_message_id)
            for profile in self.users.list_users()
            if profile.panel_workspace_message_id is not None
        ]
        for user_id, message_id in stale:
            try:
                await self.bot.delete_message(user_id, message_id)
            except TelegramAPIError:
                try:
                    await self.bot.edit_message_reply_markup(
                        chat_id=user_id,
                        message_id=message_id,
                        reply_markup=None,
                    )
                except TelegramAPIError:
                    LOGGER.info(
                        "Could not remove stale workspace %s for user %s",
                        message_id,
                        user_id,
                    )
        return await self.users.clear_all_panel_workspace_messages()

    async def shutdown(self) -> None:
        tasks = tuple(self._expiry_tasks.values())
        self._expiry_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class PanelActivityMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        panels: PanelManager | None = data.get("panels")
        user_id: int | None
        if isinstance(event, CallbackQuery):
            message = event.message
            user_id = event.from_user.id
        elif isinstance(event, Message):
            message = event
            user_id = event.from_user.id if event.from_user else None
        else:
            message = None
            user_id = None
        if panels is not None and user_id is not None and message is not None:
            message_id = getattr(message, "message_id", None)
            if message_id is not None:
                panels.touch(user_id, message_id)
        return await handler(event, data)

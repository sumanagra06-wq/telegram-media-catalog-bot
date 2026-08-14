from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ..config import Config
from ..guards import access_denied_text, can_use_bot, ensure_registered
from ..models import Category, CategoryMode, FileRecord, MediaType
from ..panels import PanelManager
from ..repositories import CatalogRepository, UserRepository
from ..services import CatalogQueryService, SearchSessionStore, delivery_caption
from ..storage import StorageError
from ..ui import (
    content_screen,
    delivery_receipt,
    no_results,
    pack_screen,
    search_results,
    season_screen,
    variants_screen,
)
from ..utils import safe_html
from .common import edit_screen

LOGGER = logging.getLogger(__name__)
router = Router(name="search")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


async def _active_callback(callback: CallbackQuery, users: UserRepository, config: Config):
    profile, _ = await ensure_registered(callback.from_user, users, config)
    if not can_use_bot(profile, config):
        await callback.answer(access_denied_text(profile), show_alert=True)
        return None
    return profile


async def _render_clean_search(
    message: Message,
    bot: Bot,
    panels: PanelManager | None,
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> None:
    if message.from_user is not None and panels is not None:
        rendered = await panels.render_workspace(
            user_id=message.from_user.id,
            text=text,
            reply_markup=markup,
        )
        if rendered is not None:
            try:
                await bot.delete_message(message.from_user.id, message.message_id)
            except TelegramAPIError:
                LOGGER.info("Could not auto-clean search query %s", message.message_id)
            return
    await message.answer(text, reply_markup=markup)


@router.message(
    StateFilter(None),
    F.text,
    ~F.text.startswith("/"),
    ~F.is_topic_message,
)
async def plain_title_search(
    message: Message,
    bot: Bot,
    config: Config,
    users: UserRepository,
    query: CatalogQueryService,
    sessions: SearchSessionStore,
    panels: PanelManager | None = None,
) -> None:
    if (
        message.chat.type != "private"
        or message.from_user is None
        or getattr(message, "message_thread_id", None) is not None
    ):
        return
    profile, _ = await ensure_registered(message.from_user, users, config, bot)
    if not can_use_bot(profile, config):
        await message.answer(access_denied_text(profile))
        return
    raw_query = " ".join((message.text or "").split()).strip()
    if len(raw_query) < 2:
        await _render_clean_search(
            message,
            bot,
            panels,
            "🔎 <b>SEARCH NEEDS A LITTLE MORE</b>\n"
            "Please type at least two characters from the title.",
            None,
        )
        return
    if len(raw_query) > 100:
        await _render_clean_search(
            message,
            bot,
            panels,
            "✂️ <b>SEARCH IS TOO LONG</b>\n"
            "Send only the movie or series title, optionally followed by its year.",
            None,
        )
        return
    hits = query.search(raw_query)
    if not hits:
        text, markup = no_results(raw_query)
        await _render_clean_search(message, bot, panels, text, markup)
        return
    contents = [hit.content for hit in hits]
    session = sessions.create(
        message.from_user.id,
        raw_query,
        [item.id for item in contents],
    )
    text, markup = search_results(session, contents, 0)
    await _render_clean_search(message, bot, panels, text, markup)


@router.callback_query(F.data.startswith("sr:"))
async def search_page_callback(
    callback: CallbackQuery,
    sessions: SearchSessionStore,
    catalog: CatalogRepository,
    users: UserRepository,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    _, token, page_text = callback.data.split(":", 2)
    session = sessions.get(token, callback.from_user.id)
    if session is None:
        await callback.answer("This search expired. Type the title again.", show_alert=True)
        return
    contents = [
        content
        for content_id in session.content_ids
        if (content := catalog.get_content(content_id)) is not None
    ]
    text, markup = search_results(session, contents, int(page_text))
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("ct:"))
async def content_callback(
    callback: CallbackQuery,
    catalog: CatalogRepository,
    query: CatalogQueryService,
    users: UserRepository,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    _, content_id, token, page_text = callback.data.split(":", 3)
    content = catalog.get_content(content_id)
    if content is None:
        await callback.answer("This title is no longer available.", show_alert=True)
        return
    category = catalog.get_category(content.category_id)
    if category is None:
        await callback.answer("The title category is unavailable.", show_alert=True)
        return
    text, markup = content_screen(
        content=content,
        category=category,
        query=query,
        back_token=token,
        back_page=int(page_text),
    )
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("se:"))
async def season_callback(
    callback: CallbackQuery,
    catalog: CatalogRepository,
    query: CatalogQueryService,
    users: UserRepository,
    config: Config,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    _, content_id, season_text, token, page_text = callback.data.split(":", 4)
    content = catalog.get_content(content_id)
    if content is None:
        await callback.answer("This series is unavailable.", show_alert=True)
        return
    text, markup = season_screen(content, int(season_text), query, token, int(page_text))
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("epg:"))
async def episode_page_callback(
    callback: CallbackQuery,
    catalog: CatalogRepository,
    query: CatalogQueryService,
    users: UserRepository,
    config: Config,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    _, content_id, season_text, token, result_page, episode_page = callback.data.split(":", 5)
    content = catalog.get_content(content_id)
    if content is None:
        await callback.answer("This series is unavailable.", show_alert=True)
        return
    text, markup = season_screen(
        content,
        int(season_text),
        query,
        token,
        int(result_page),
        int(episode_page),
    )
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("ep:"))
async def episode_callback(
    callback: CallbackQuery,
    bot: Bot,
    catalog: CatalogRepository,
    query: CatalogQueryService,
    users: UserRepository,
    config: Config,
    panels: PanelManager | None = None,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    _, content_id, season_text, episode_text, token, result_page = callback.data.split(":", 5)
    content = catalog.get_content(content_id)
    if content is None:
        await callback.answer("This series is unavailable.", show_alert=True)
        return
    variants = query.episode_variants(content_id, int(season_text), int(episode_text))
    if not variants:
        await callback.answer("This episode is unavailable.", show_alert=True)
        return
    if len(variants) == 1:
        await callback.answer("Preparing secure delivery…")
        await _deliver_file(callback, variants[0].id, bot, catalog, config, panels)
        return
    text, markup = variants_screen(
        content,
        int(season_text),
        int(episode_text),
        variants,
        token,
        int(result_page),
    )
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("pk:"))
async def pack_callback(
    callback: CallbackQuery,
    catalog: CatalogRepository,
    query: CatalogQueryService,
    users: UserRepository,
    config: Config,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    _, content_id, season_text, token, result_page = callback.data.split(":", 4)
    content = catalog.get_content(content_id)
    if content is None:
        await callback.answer("This series is unavailable.", show_alert=True)
        return
    parts = query.season_pack_parts(content_id, int(season_text))
    if not parts:
        await callback.answer("This season pack is unavailable.", show_alert=True)
        return
    text, markup = pack_screen(content, int(season_text), parts, token, int(result_page))
    await callback.answer()
    await edit_screen(callback, text, markup)


@dataclass(frozen=True, slots=True)
class DeliveryTopicSpec:
    category_id: str
    name: str
    icon_color: int


def _delivery_topic_spec(category: Category) -> DeliveryTopicSpec:
    icons = {
        CategoryMode.SINGLE: "🎬",
        CategoryMode.EPISODIC: "📺",
        CategoryMode.MIXED: "🗂",
    }
    colors = {
        CategoryMode.SINGLE: 7_322_096,
        CategoryMode.EPISODIC: 9_367_192,
        CategoryMode.MIXED: 13_338_331,
    }
    return DeliveryTopicSpec(
        category_id=category.id,
        name=f"{icons[category.mode]} {category.name}"[:128],
        icon_color=colors[category.mode],
    )


def _is_delivery_topic_error(exc: TelegramBadRequest) -> bool:
    detail = str(exc).casefold()
    return any(
        marker in detail
        for marker in (
            "message thread not found",
            "thread not found",
            "topic_closed",
            "topic closed",
            "thread has been closed",
            "message thread is closed",
            "message thread closed",
            "thread was deleted",
            "topic was deleted",
            "chat is not a forum",
        )
    )


async def _ensure_delivery_target(
    panels: PanelManager | None,
    user_id: int,
    spec: DeliveryTopicSpec,
    *,
    replace: bool = False,
) -> int | None:
    if panels is None:
        return None
    try:
        return await panels.ensure_delivery_topic(
            user_id,
            category_id=spec.category_id,
            topic_name=spec.name,
            icon_color=spec.icon_color,
            replace=replace,
        )
    except (StorageError, ValueError):
        LOGGER.exception(
            "Could not persist a delivery topic for user %s; using General",
            user_id,
        )
        return None


async def _copy_to_delivery_topic(
    *,
    bot: Bot,
    panels: PanelManager | None,
    user_id: int,
    spec: DeliveryTopicSpec,
    source_chat_id: int,
    source_message_id: int,
    caption: str,
    protect_content: bool,
) -> int | None:
    topic_id = await _ensure_delivery_target(panels, user_id, spec)

    async def copy(target_topic_id: int | None) -> None:
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=source_chat_id,
            message_id=source_message_id,
            message_thread_id=target_topic_id,
            caption=caption,
            parse_mode="HTML",
            protect_content=protect_content,
        )

    try:
        await copy(topic_id)
        return topic_id
    except TelegramBadRequest as exc:
        if topic_id is None or not _is_delivery_topic_error(exc) or panels is None:
            raise

        if "closed" in str(exc).casefold() and await panels.reopen_delivery_topic(
            user_id, topic_id
        ):
            try:
                await copy(topic_id)
                return topic_id
            except TelegramBadRequest as reopened_exc:
                if not _is_delivery_topic_error(reopened_exc):
                    raise

        replacement_id = await _ensure_delivery_target(panels, user_id, spec, replace=True)
        try:
            await copy(replacement_id)
            return replacement_id
        except TelegramBadRequest as replacement_exc:
            if replacement_id is None or not _is_delivery_topic_error(replacement_exc):
                raise

        # A topic can disappear between creation and copy. General is the final safe target.
        await copy(None)
        return None


async def _send_file_id_with_delivery_fallback(
    *,
    bot: Bot,
    panels: PanelManager | None,
    user_id: int,
    spec: DeliveryTopicSpec,
    topic_id: int | None,
    record: FileRecord,
    caption: str,
    protect_content: bool,
) -> int | None:
    async def send(target_topic_id: int | None) -> None:
        if record.media_type == MediaType.VIDEO:
            await bot.send_video(
                chat_id=user_id,
                video=record.telegram_file_id,
                message_thread_id=target_topic_id,
                caption=caption,
                parse_mode="HTML",
                protect_content=protect_content,
            )
        else:
            await bot.send_document(
                chat_id=user_id,
                document=record.telegram_file_id,
                message_thread_id=target_topic_id,
                caption=caption,
                parse_mode="HTML",
                protect_content=protect_content,
            )

    try:
        await send(topic_id)
        return topic_id
    except TelegramBadRequest as exc:
        if topic_id is None or not _is_delivery_topic_error(exc) or panels is None:
            raise

        if "closed" in str(exc).casefold() and await panels.reopen_delivery_topic(
            user_id, topic_id
        ):
            try:
                await send(topic_id)
                return topic_id
            except TelegramBadRequest as reopened_exc:
                if not _is_delivery_topic_error(reopened_exc):
                    raise

        replacement_id = await _ensure_delivery_target(panels, user_id, spec, replace=True)
        try:
            await send(replacement_id)
            return replacement_id
        except TelegramBadRequest as replacement_exc:
            if replacement_id is None or not _is_delivery_topic_error(replacement_exc):
                raise

        await send(None)
        return None


async def _finish_delivery(
    *,
    callback: CallbackQuery,
    bot: Bot,
    panels: PanelManager | None,
    receipt_text: str,
    receipt_markup: InlineKeyboardMarkup,
) -> None:
    source_message_id = (
        getattr(callback.message, "message_id", None) if callback.message is not None else None
    )
    receipt_message_id: int | None = None
    if panels is not None:
        try:
            receipt_message_id = await panels.render_delivery_receipt(
                user_id=callback.from_user.id,
                text=receipt_text,
                reply_markup=receipt_markup,
            )
        except (StorageError, ValueError, RuntimeError, TelegramAPIError):
            LOGGER.warning(
                "Delivery succeeded but the reusable receipt failed for user %s",
                callback.from_user.id,
                exc_info=True,
            )
    if source_message_id is None or source_message_id == receipt_message_id:
        return
    try:
        await bot.delete_message(callback.from_user.id, source_message_id)
    except TelegramAPIError:
        LOGGER.info("Could not auto-clean delivery source card %s", source_message_id)


async def _deliver_file(
    callback: CallbackQuery,
    file_id: str,
    bot: Bot,
    catalog: CatalogRepository,
    config: Config,
    panels: PanelManager | None = None,
) -> None:
    record = catalog.get_file(file_id)
    if record is None or not record.available:
        await callback.message.answer(
            "⚠️ <b>FILE UNAVAILABLE</b>\n"
            "This version cannot be delivered right now. Please choose another available file."
        )
        return
    content = catalog.get_content(record.content_id)
    category = catalog.get_category(record.category_id)
    if content is None or category is None:
        await callback.message.answer(
            "⚠️ <b>TITLE UNAVAILABLE</b>\nThis title is no longer in the delivery catalog."
        )
        return

    spec = _delivery_topic_spec(category)
    caption = delivery_caption(record, content.kind, category.name)

    async def finish(topic_id: int | None) -> None:
        text, markup = delivery_receipt(
            content,
            record,
            category,
            spec.name if topic_id is not None else None,
        )
        await _finish_delivery(
            callback=callback,
            bot=bot,
            panels=panels,
            receipt_text=text,
            receipt_markup=markup,
        )

    try:
        delivery_topic_id = await _copy_to_delivery_topic(
            bot=bot,
            panels=panels,
            user_id=callback.from_user.id,
            spec=spec,
            source_chat_id=record.source_chat_id,
            source_message_id=record.source_message_id,
            caption=caption,
            protect_content=config.protect_delivered_content,
        )
    except TelegramForbiddenError:
        # Usually means the user blocked the bot; the source record is still valid.
        return
    except TelegramBadRequest as exc:
        message = str(exc).casefold()
        source_missing = "message to copy not found" in message or "message_id_invalid" in message
        if not source_missing:
            await callback.message.answer(
                "⚠️ <b>DELIVERY INTERRUPTED</b>\n"
                "Telegram could not send this file. Please wait a moment and try again."
            )
            return

        # Telegram file IDs provide a server-side fallback without downloading or re-uploading
        # the media through Railway.
        try:
            delivery_topic_id = await _ensure_delivery_target(
                panels,
                callback.from_user.id,
                spec,
            )
            delivery_topic_id = await _send_file_id_with_delivery_fallback(
                bot=bot,
                panels=panels,
                user_id=callback.from_user.id,
                spec=spec,
                topic_id=delivery_topic_id,
                record=record,
                caption=caption,
                protect_content=config.protect_delivered_content,
            )
            await finish(delivery_topic_id)
            return
        except TelegramForbiddenError:
            # A blocked bot cannot deliver to either a topic or General; the file remains valid.
            return
        except TelegramBadRequest:
            await catalog.mark_file_available(file_id, False)
            await callback.message.answer(
                "❌ <b>FILE NO LONGER AVAILABLE</b>\n"
                "The broken version was hidden from delivery and the owner was notified."
            )
            for owner_id in config.owner_ids:
                try:
                    await bot.send_message(
                        owner_id,
                        "⚠️ <b>SOURCE FILE UNAVAILABLE</b>\n"
                        "<blockquote>Automatic delivery failed and the file was hidden.</blockquote>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"🆔 <b>File</b>  •  <code>{file_id}</code>\n"
                        f"🎬 <b>Title</b>  •  {safe_html(record.title)}",
                    )
                except (TelegramBadRequest, TelegramForbiddenError):
                    LOGGER.info("Could not notify owner %s about unavailable file", owner_id)
    else:
        await finish(delivery_topic_id)


@router.callback_query(F.data.startswith("fl:"))
async def file_callback(
    callback: CallbackQuery,
    bot: Bot,
    catalog: CatalogRepository,
    users: UserRepository,
    config: Config,
    panels: PanelManager | None = None,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    file_id = callback.data.split(":", 1)[1]
    await callback.answer("Preparing secure delivery…")
    await _deliver_file(callback, file_id, bot, catalog, config, panels)

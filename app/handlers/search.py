from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ..config import Config
from ..guards import access_denied_text, can_use_bot, ensure_registered
from ..models import FileRecord, MediaType
from ..panels import PanelManager
from ..repositories import CatalogRepository, UserRepository
from ..services import CatalogQueryService, SearchSessionStore, delivery_caption
from ..storage import StorageError
from ..ui import (
    content_screen,
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
        await callback.answer("Preparing temporary file…")
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


async def _copy_to_private_chat(
    *,
    bot: Bot,
    user_id: int,
    source_chat_id: int,
    source_message_id: int,
    caption: str,
) -> int:
    message = await bot.copy_message(
        chat_id=user_id,
        from_chat_id=source_chat_id,
        message_id=source_message_id,
        caption=caption,
        parse_mode="HTML",
        protect_content=False,
    )
    return message.message_id


async def _send_file_id_to_private_chat(
    *,
    bot: Bot,
    user_id: int,
    record: FileRecord,
    caption: str,
) -> int:
    if record.media_type == MediaType.VIDEO:
        message = await bot.send_video(
            chat_id=user_id,
            video=record.telegram_file_id,
            caption=caption,
            parse_mode="HTML",
            protect_content=False,
        )
    else:
        message = await bot.send_document(
            chat_id=user_id,
            document=record.telegram_file_id,
            caption=caption,
            parse_mode="HTML",
            protect_content=False,
        )
    return message.message_id


async def _finish_delivery(
    *,
    callback: CallbackQuery,
    bot: Bot,
    panels: PanelManager,
) -> None:
    user_id = callback.from_user.id
    source_message_id = (
        getattr(callback.message, "message_id", None) if callback.message is not None else None
    )
    closed_workspace_id: int | None = None
    try:
        closed_workspace_id = await panels.close_current_workspace(user_id)
    except (StorageError, ValueError, TelegramAPIError):
        LOGGER.warning("Could not retire workspace for user %s", user_id, exc_info=True)
    if source_message_id is not None and source_message_id != closed_workspace_id:
        try:
            await bot.delete_message(user_id, source_message_id)
        except TelegramAPIError:
            LOGGER.info("Could not auto-clean delivery source card %s", source_message_id)
    # The permanent pinned dashboard is intentionally untouched. Only owner broadcasts and the
    # explicit emergency recovery command post a fresh dashboard.


async def _deliver_file_now(
    callback: CallbackQuery,
    file_id: str,
    bot: Bot,
    catalog: CatalogRepository,
    config: Config,
    panels: PanelManager | None,
) -> None:
    if panels is None:
        await callback.message.answer(
            "⚠️ <b>DELIVERY TEMPORARILY UNAVAILABLE</b>\n"
            "The safe expiry service is not ready. Please try again in a moment."
        )
        return
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

    caption = delivery_caption(record, content.kind, category.name)
    try:
        delivered_message_id = await _copy_to_private_chat(
            bot=bot,
            user_id=callback.from_user.id,
            source_chat_id=record.source_chat_id,
            source_message_id=record.source_message_id,
            caption=caption,
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
        try:
            delivered_message_id = await _send_file_id_to_private_chat(
                bot=bot,
                user_id=callback.from_user.id,
                record=record,
                caption=caption,
            )
        except TelegramForbiddenError:
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
            return

    # Anchor and persist the media ID as soon as Telegram confirms the copy. A process crash while
    # sending the reminder can no longer orphan an otherwise unknown file message.
    expires_at = panels.delivery_expires_at()
    try:
        await panels.register_temporary_delivery(
            user_id=callback.from_user.id,
            media_message_id=delivered_message_id,
            notice_message_id=None,
            expires_at=expires_at,
        )
    except (StorageError, ValueError):
        await callback.message.answer(
            "⚠️ <b>DELIVERY INTERRUPTED</b>\n"
            "The temporary file was removed because its five-minute cleanup could not be saved. "
            "Please try again."
        )
        return

    try:
        notice = await bot.send_message(
            callback.from_user.id,
            "⏳ <b>SAVE WITHIN 5 MINUTES</b>\n"
            "Save/download the file now, or forward it to Saved Messages or another chat. "
            "The file and this reminder will auto-delete.",
        )
    except TelegramAPIError:
        deleted = await panels.discard_registered_temporary_delivery(
            user_id=callback.from_user.id,
            media_message_id=delivered_message_id,
        )
        LOGGER.warning(
            "Expiry notice failed for file %s belonging to user %s; immediate cleanup %s",
            delivered_message_id,
            callback.from_user.id,
            "completed" if deleted else "will retry from its durable record",
        )
        return

    try:
        await panels.attach_temporary_delivery_notice(
            user_id=callback.from_user.id,
            media_message_id=delivered_message_id,
            notice_message_id=notice.message_id,
        )
    except (StorageError, ValueError):
        deleted = await panels.discard_registered_temporary_delivery(
            user_id=callback.from_user.id,
            media_message_id=delivered_message_id,
            notice_message_id=notice.message_id,
        )
        if not deleted:
            # The provisional record may already have expired while Telegram was sending the
            # reminder; delete the late reminder even when no durable record remains.
            await panels.discard_delivery_messages(
                user_id=callback.from_user.id,
                media_message_id=delivered_message_id,
                notice_message_id=notice.message_id,
            )
        await callback.message.answer(
            "⚠️ <b>DELIVERY INTERRUPTED</b>\n"
            "The temporary file was removed because its reminder could not be safely tracked. "
            "Please try again."
        )
        return

    await _finish_delivery(
        callback=callback,
        bot=bot,
        panels=panels,
    )


async def _deliver_file(
    callback: CallbackQuery,
    file_id: str,
    bot: Bot,
    catalog: CatalogRepository,
    config: Config,
    panels: PanelManager | None = None,
) -> None:
    if panels is None:
        await _deliver_file_now(callback, file_id, bot, catalog, config, panels)
        return
    async with panels.delivery_lock(callback.from_user.id):
        await _deliver_file_now(callback, file_id, bot, catalog, config, panels)


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
    await callback.answer("Preparing temporary file…")
    await _deliver_file(callback, file_id, bot, catalog, config, panels)

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from ..config import Config
from ..guards import access_denied_text, can_use_bot, ensure_registered
from ..models import MediaType
from ..panels import PanelManager
from ..repositories import CatalogRepository, UserRepository
from ..services import CatalogQueryService, SearchSessionStore, delivery_caption
from ..ui import (
    content_screen,
    no_results,
    pack_screen,
    search_results,
    season_screen,
    selectable_results,
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


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def plain_title_search(
    message: Message,
    bot: Bot,
    config: Config,
    users: UserRepository,
    query: CatalogQueryService,
    sessions: SearchSessionStore,
    panels: PanelManager | None = None,
) -> None:
    if message.chat.type != "private" or message.from_user is None:
        return
    profile, _ = await ensure_registered(message.from_user, users, config, bot)
    if not can_use_bot(profile, config):
        await message.answer(access_denied_text(profile))
        return
    raw_query = " ".join((message.text or "").split()).strip()
    if len(raw_query) < 2:
        await message.answer(
            "🔎 <b>SEARCH NEEDS A LITTLE MORE</b>\n"
            "Please type at least two characters from the title."
        )
        return
    if len(raw_query) > 100:
        await message.answer(
            "✂️ <b>SEARCH IS TOO LONG</b>\n"
            "Send only the movie or series title, optionally followed by its year."
        )
        return
    current_profile = users.get_user(message.from_user.id)
    workspace_active = bool(
        panels and current_profile and current_profile.panel_workspace_message_id is not None
    )
    hits = query.search(raw_query)
    if not hits:
        text, markup = no_results(raw_query)
        if panels and workspace_active:
            rendered = await panels.render_existing_workspace(
                user_id=message.from_user.id,
                text=text,
                reply_markup=markup,
            )
            if rendered:
                return
        await message.answer(text, reply_markup=markup)
        return
    contents = [hit.content for hit in hits]
    session = sessions.create(
        message.from_user.id,
        raw_query,
        [item.id for item in contents],
        selectable=workspace_active,
    )
    if session.selectable:
        text, markup = selectable_results(session, contents, 0)
    else:
        text, markup = search_results(session, contents, 0)
    if panels and workspace_active:
        rendered = await panels.render_existing_workspace(
            user_id=message.from_user.id,
            text=text,
            reply_markup=markup,
        )
        if rendered:
            return
    await message.answer(text, reply_markup=markup)


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
    if session.selectable:
        text, markup = selectable_results(session, contents, int(page_text))
    else:
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
        await callback.answer("Sending file…")
        await _deliver_file(callback, variants[0].id, bot, catalog, config)
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


async def _deliver_file(
    callback: CallbackQuery,
    file_id: str,
    bot: Bot,
    catalog: CatalogRepository,
    config: Config,
) -> None:
    record = catalog.get_file(file_id)
    if record is None or not record.available:
        await callback.message.answer(
            "⚠️ <b>FILE UNAVAILABLE</b>\n"
            "This version cannot be delivered right now. Please choose another available file."
        )
        return
    content = catalog.get_content(record.content_id)
    if content is None:
        await callback.message.answer(
            "⚠️ <b>TITLE UNAVAILABLE</b>\nThis title is no longer in the delivery catalog."
        )
        return
    try:
        await bot.copy_message(
            chat_id=callback.from_user.id,
            from_chat_id=record.source_chat_id,
            message_id=record.source_message_id,
            caption=delivery_caption(record, content.kind),
            parse_mode="HTML",
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
            send_kwargs = {
                "chat_id": callback.from_user.id,
                "caption": delivery_caption(record, content.kind),
                "parse_mode": "HTML",
                "protect_content": config.protect_delivered_content,
            }
            if record.media_type == MediaType.VIDEO:
                await bot.send_video(video=record.telegram_file_id, **send_kwargs)
            else:
                await bot.send_document(document=record.telegram_file_id, **send_kwargs)
            return
        except (TelegramBadRequest, TelegramForbiddenError):
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


@router.callback_query(F.data.startswith("fl:"))
async def file_callback(
    callback: CallbackQuery,
    bot: Bot,
    catalog: CatalogRepository,
    users: UserRepository,
    config: Config,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    file_id = callback.data.split(":", 1)[1]
    await callback.answer("Sending file…")
    await _deliver_file(callback, file_id, bot, catalog, config)

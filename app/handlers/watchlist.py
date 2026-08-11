from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..config import Config
from ..guards import access_denied_text, can_use_bot, ensure_registered
from ..repositories import CatalogRepository, UserRepository
from ..services import CatalogQueryService
from ..ui import CODE_WATCH, content_screen, watchlist_entries, watchlist_home
from .common import edit_screen

router = Router(name="watchlist")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


async def _show_watchlist_message(
    message: Message,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
    bot: Bot,
) -> None:
    if message.from_user is None:
        return
    profile, _ = await ensure_registered(message.from_user, users, config, bot)
    if not can_use_bot(profile, config):
        await message.answer(access_denied_text(profile))
        return
    text, markup = watchlist_home(profile, catalog.list_categories(include_disabled=True))
    await message.answer(text, reply_markup=markup)


@router.message(Command("watchlist"))
async def watchlist_command(
    message: Message,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
    bot: Bot,
) -> None:
    await _show_watchlist_message(message, users, catalog, config, bot)


@router.callback_query(F.data == "menu:watchlist")
async def watchlist_callback(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
) -> None:
    profile, _ = await ensure_registered(callback.from_user, users, config)
    if not can_use_bot(profile, config):
        await callback.answer(access_denied_text(profile), show_alert=True)
        return
    text, markup = watchlist_home(profile, catalog.list_categories(include_disabled=True))
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wl:"))
async def set_watchlist_callback(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    query: CatalogQueryService,
    config: Config,
) -> None:
    profile, _ = await ensure_registered(callback.from_user, users, config)
    if not can_use_bot(profile, config):
        await callback.answer(access_denied_text(profile), show_alert=True)
        return
    _, content_id, code = callback.data.split(":", 2)
    content = catalog.get_content(content_id)
    if content is None:
        await callback.answer("This title is unavailable.", show_alert=True)
        return
    category = catalog.get_category(content.category_id)
    if category is None:
        await callback.answer("This category is unavailable.", show_alert=True)
        return
    if code == "r":
        removed = await users.remove_watchlist(callback.from_user.id, content.id)
        message = "Removed from your watchlist." if removed else "This title was not saved."
    else:
        status = CODE_WATCH.get(code)
        if status is None:
            await callback.answer("Invalid watchlist status.", show_alert=True)
            return
        await users.set_watch_status(
            user_id=callback.from_user.id,
            content_id=content.id,
            title=content.title,
            year=content.year,
            category_id=category.id,
            category_name=category.name,
            status=status,
        )
        message = f"Saved as {status.value.replace('_', ' ').title()}."
    refreshed = users.get_user(callback.from_user.id)
    entry = refreshed.watchlist.get(content.id) if refreshed else None
    text, markup = content_screen(
        content=content,
        category=category,
        query=query,
        watch_status=entry.status if entry else None,
    )
    await callback.answer(message)
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wls:"))
async def watchlist_status_page(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
) -> None:
    profile, _ = await ensure_registered(callback.from_user, users, config)
    if not can_use_bot(profile, config):
        await callback.answer(access_denied_text(profile), show_alert=True)
        return
    _, code, page_text = callback.data.split(":", 2)
    status = CODE_WATCH.get(code)
    if status is None:
        await callback.answer("Invalid status.", show_alert=True)
        return
    entries = sorted(
        (entry for entry in profile.watchlist.values() if entry.status == status),
        key=lambda item: item.updated_at,
        reverse=True,
    )
    text, markup = watchlist_entries(
        status.value.replace("_", " ").title(),
        entries,
        int(page_text),
        f"wls:{code}",
    )
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlc:"))
async def watchlist_category_page(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
) -> None:
    profile, _ = await ensure_registered(callback.from_user, users, config)
    if not can_use_bot(profile, config):
        await callback.answer(access_denied_text(profile), show_alert=True)
        return
    _, category_id, page_text = callback.data.split(":", 2)
    category = catalog.get_category(category_id)
    title = category.name if category else "Category"
    entries = sorted(
        (entry for entry in profile.watchlist.values() if entry.category_id == category_id),
        key=lambda item: item.updated_at,
        reverse=True,
    )
    text, markup = watchlist_entries(
        title,
        entries,
        int(page_text),
        f"wlc:{category_id}",
    )
    await callback.answer()
    await edit_screen(callback, text, markup)

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Config
from ..guards import access_denied_text, can_use_bot, ensure_registered
from ..models import UserProfile, UserStatus
from ..presentation import ActionButton as InlineKeyboardButton
from ..repositories import CatalogRepository, UserRepository
from ..services import CatalogQueryService
from ..ui import (
    CODE_WATCH,
    public_watchlist_directory,
    watchlist_add_method,
    watchlist_category_picker,
    watchlist_entries,
    watchlist_entry_detail,
    watchlist_home,
    watchlist_status_picker,
)
from ..utils import compact_label, safe_html
from .common import edit_screen

router = Router(name="watchlist")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
DIVIDER = "━━━━━━━━━━━━━━━━━━"


def _cancel_markup() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="menu:watchlist"))
    return builder


class WatchlistAddState(StatesGroup):
    manual_title = State()
    manual_status = State()
    catalog_query = State()
    catalog_status = State()


async def _active_callback(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
) -> UserProfile | None:
    profile, _ = await ensure_registered(callback.from_user, users, config)
    if not can_use_bot(profile, config):
        await callback.answer(access_denied_text(profile), show_alert=True)
        return None
    return profile


async def _show_watchlist_message(
    message: Message,
    users: UserRepository,
    config: Config,
    bot: Bot,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return
    profile, _ = await ensure_registered(message.from_user, users, config, bot)
    if not can_use_bot(profile, config):
        await message.answer(access_denied_text(profile))
        return
    await state.clear()
    text, markup = watchlist_home(profile)
    await message.answer(text, reply_markup=markup)


@router.message(Command("watchlist"))
async def watchlist_command(
    message: Message,
    users: UserRepository,
    config: Config,
    bot: Bot,
    state: FSMContext,
) -> None:
    await _show_watchlist_message(message, users, config, bot, state)


@router.callback_query(F.data == "menu:watchlist")
async def watchlist_callback(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
    state: FSMContext,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    await state.clear()
    text, markup = watchlist_home(profile)
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data == "wla:start")
async def add_title_start(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
    state: FSMContext,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    await state.clear()
    text, markup = watchlist_add_method()
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data == "wla:manual")
async def manual_add_start(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
    state: FSMContext,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    await state.clear()
    text, markup = watchlist_category_picker(catalog.list_categories(), "wamc", "Manual title")
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wamc:"))
async def manual_category_selected(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
    state: FSMContext,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    category_id = callback.data.split(":", 1)[1]
    category = catalog.get_category(category_id)
    if category is None or not category.enabled:
        await callback.answer("Category is unavailable.", show_alert=True)
        return
    await state.set_state(WatchlistAddState.manual_title)
    await state.update_data(category_id=category.id, category_name=category.name)
    await callback.answer()
    await edit_screen(
        callback,
        "✍️ <b>ADD A CUSTOM TITLE</b>\n"
        f"<blockquote>{safe_html(category.name)} • Step 2 of 3</blockquote>\n"
        f"{DIVIDER}\n"
        "⌨️ Send the title name in your next message.\n\n"
        "💡 You can save any title, even when it is not in the library.",
        _cancel_markup().as_markup(),
    )


@router.message(WatchlistAddState.manual_title, F.text)
async def manual_title_received(message: Message, state: FSMContext) -> None:
    title = " ".join(message.text.split()).strip()
    if not title or len(title) > 160:
        await message.answer("Send a title between 1 and 160 characters, or /cancel.")
        return
    await state.update_data(title=title)
    await state.set_state(WatchlistAddState.manual_status)
    text, markup = watchlist_status_picker(title, "wams")
    await message.answer(text, reply_markup=markup)


@router.callback_query(WatchlistAddState.manual_status, F.data.startswith("wams:"))
async def manual_status_selected(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
    state: FSMContext,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    status = CODE_WATCH.get(callback.data.split(":", 1)[1])
    if status is None:
        await callback.answer("Invalid status.", show_alert=True)
        return
    data = await state.get_data()
    if not all(key in data for key in ("title", "category_id", "category_name")):
        await state.clear()
        await callback.answer("This add-title session expired.", show_alert=True)
        return
    category = catalog.get_category(data["category_id"])
    if category is None or not category.enabled:
        await state.clear()
        await callback.answer("Category is no longer available.", show_alert=True)
        return
    _, created = await users.upsert_watchlist_entry(
        user_id=callback.from_user.id,
        title=data["title"],
        category_id=category.id,
        category_name=category.name,
        status=status,
    )
    await state.clear()
    profile = users.get_user(callback.from_user.id)
    if profile is None:
        raise RuntimeError("Registered watchlist owner disappeared")
    text, markup = watchlist_home(profile)
    await callback.answer("✅ Title added." if created else "✅ Existing title updated.")
    await edit_screen(callback, text, markup)


@router.callback_query(F.data == "wla:catalog")
async def catalog_add_start(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
    state: FSMContext,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    await state.clear()
    text, markup = watchlist_category_picker(catalog.list_categories(), "wacc", "Catalog title")
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wacc:"))
async def catalog_category_selected(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
    state: FSMContext,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    category_id = callback.data.split(":", 1)[1]
    category = catalog.get_category(category_id)
    if category is None or not category.enabled:
        await callback.answer("Category is unavailable.", show_alert=True)
        return
    await state.set_state(WatchlistAddState.catalog_query)
    await state.update_data(category_id=category.id)
    await callback.answer()
    await edit_screen(
        callback,
        "🔎 <b>FIND A CATALOG TITLE</b>\n"
        f"<blockquote>{safe_html(category.name)} • Step 2 of 3</blockquote>\n"
        f"{DIVIDER}\n"
        "⌨️ Send all or part of the title name.\n\n"
        "🎯 The closest matching catalog titles will appear first.",
        _cancel_markup().as_markup(),
    )


@router.message(WatchlistAddState.catalog_query, F.text)
async def catalog_title_query(
    message: Message,
    query: CatalogQueryService,
    catalog: CatalogRepository,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    category_id = data.get("category_id")
    matches = [
        hit.content for hit in query.search(message.text) if hit.content.category_id == category_id
    ][:8]
    if not matches:
        await message.answer(
            "🔍 <b>NO CATALOG MATCH</b>\n"
            "Try fewer words or check the spelling. Use /cancel to stop."
        )
        return
    builder = InlineKeyboardBuilder()
    for content in matches:
        year = f" ({content.year or 'Unknown'})"
        icon = "📺" if content.kind.value == "series" else "🎬"
        builder.row(
            InlineKeyboardButton(
                text=compact_label(f"{icon} {content.title}{year}", 58),
                callback_data=f"wacp:{content.id}",
                style="primary",
            )
        )
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="menu:watchlist"))
    await message.answer(
        "🎯 <b>CHOOSE A CATALOG TITLE</b>\n"
        f"<blockquote>{len(matches)} matching title{'s' if len(matches) != 1 else ''}</blockquote>\n"
        f"{DIVIDER}\n"
        "Select the correct title below:",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(WatchlistAddState.catalog_query, F.data.startswith("wacp:"))
async def catalog_title_selected(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
    state: FSMContext,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    content_id = callback.data.split(":", 1)[1]
    content = catalog.get_content(content_id)
    data = await state.get_data()
    if content is None or content.category_id != data.get("category_id"):
        await callback.answer("Catalog title is unavailable.", show_alert=True)
        return
    await state.set_state(WatchlistAddState.catalog_status)
    await state.update_data(content_id=content.id)
    text, markup = watchlist_status_picker(content.title, f"wacs:{content.id}")
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(WatchlistAddState.catalog_status, F.data.startswith("wacs:"))
async def catalog_status_selected(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
    state: FSMContext,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    _, content_id, code = callback.data.split(":", 2)
    status = CODE_WATCH.get(code)
    content = catalog.get_content(content_id)
    data = await state.get_data()
    if status is None or content is None or content.id != data.get("content_id"):
        await callback.answer("This add-title selection expired.", show_alert=True)
        return
    category = catalog.get_category(content.category_id)
    if category is None:
        await callback.answer("Category is unavailable.", show_alert=True)
        return
    _, created = await users.upsert_watchlist_entry(
        user_id=callback.from_user.id,
        content_id=content.id,
        title=content.title,
        year=content.year,
        category_id=category.id,
        category_name=category.name,
        status=status,
    )
    await state.clear()
    profile = users.get_user(callback.from_user.id)
    if profile is None:
        raise RuntimeError("Registered watchlist owner disappeared")
    text, markup = watchlist_home(profile)
    await callback.answer("✅ Title added." if created else "✅ Existing title updated.")
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlm:"))
async def my_watchlist_page(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    page = int(callback.data.split(":", 1)[1])
    entries = sorted(profile.watchlist.values(), key=lambda item: item.updated_at, reverse=True)
    text, markup = watchlist_entries(profile, entries, page, own=True)
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wle:"))
async def my_watchlist_entry(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    _, entry_id, page_text = callback.data.split(":", 2)
    entry = users.get_watchlist_entry(profile.telegram_user_id, entry_id)
    if entry is None:
        await callback.answer("Watchlist entry not found.", show_alert=True)
        return
    content_available = bool(entry.content_id and catalog.get_content(entry.content_id))
    text, markup = watchlist_entry_detail(
        entry,
        profile,
        own=True,
        content_available=content_available,
        page=int(page_text),
    )
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlu:"))
async def update_entry_status(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    _, entry_id, code = callback.data.split(":", 2)
    status = CODE_WATCH.get(code)
    if status is None:
        await callback.answer("Invalid status.", show_alert=True)
        return
    try:
        entry = await users.update_watchlist_status(profile.telegram_user_id, entry_id, status)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    content_available = bool(entry.content_id and catalog.get_content(entry.content_id))
    text, markup = watchlist_entry_detail(
        entry, profile, own=True, content_available=content_available
    )
    await callback.answer("Status updated.")
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wld:"))
async def remove_entry_confirm(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    entry_id = callback.data.split(":", 1)[1]
    entry = users.get_watchlist_entry(profile.telegram_user_id, entry_id)
    if entry is None:
        await callback.answer("Watchlist entry not found.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🗑 Remove title", callback_data=f"wldc:{entry.id}", style="danger"
        ),
        InlineKeyboardButton(text="✖️ Cancel", callback_data=f"wle:{entry.id}:0"),
    )
    await callback.answer()
    await edit_screen(
        callback,
        "🗑 <b>REMOVE FROM WATCHLIST?</b>\n"
        f"<blockquote>{safe_html(entry.title)}</blockquote>\n"
        f"{DIVIDER}\n"
        "This only removes your saved entry. The catalog title and files are not affected.",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("wldc:"))
async def remove_entry(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    entry_id = callback.data.split(":", 1)[1]
    removed = await users.remove_watchlist_entry(profile.telegram_user_id, entry_id)
    refreshed = users.get_user(profile.telegram_user_id)
    if refreshed is None:
        raise RuntimeError("Registered watchlist owner disappeared")
    text, markup = watchlist_home(refreshed)
    await callback.answer("✅ Removed." if removed else "ℹ️ Entry was already removed.")
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlvis:"))
async def watchlist_visibility(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    is_public = callback.data.split(":", 1)[1] == "1"
    updated = await users.set_watchlist_visibility(profile.telegram_user_id, is_public)
    text, markup = watchlist_home(updated)
    await callback.answer("🌐 Watchlist shared." if is_public else "🔒 Watchlist is now private.")
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlp:"))
async def public_watchlists(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    page = int(callback.data.split(":", 1)[1])
    visible_users = users.public_watchlist_users(exclude_user_id=profile.telegram_user_id)
    text, markup = public_watchlist_directory(visible_users, page)
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlv:"))
async def shared_watchlist(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
) -> None:
    viewer = await _active_callback(callback, users, config)
    if viewer is None:
        return
    _, owner_id_text, page_text = callback.data.split(":", 2)
    owner = users.get_user(int(owner_id_text))
    if owner is None or owner.status != UserStatus.ACTIVE or not owner.watchlist_public:
        await callback.answer("This watchlist is private or unavailable.", show_alert=True)
        return
    entries = sorted(owner.watchlist.values(), key=lambda item: item.updated_at, reverse=True)
    text, markup = watchlist_entries(owner, entries, int(page_text), own=False)
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wved:"))
async def shared_watchlist_entry(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    _, owner_id_text, entry_id, page_text = callback.data.split(":", 3)
    owner = users.get_user(int(owner_id_text))
    if owner is None or owner.status != UserStatus.ACTIVE or not owner.watchlist_public:
        await callback.answer("This watchlist is private or unavailable.", show_alert=True)
        return
    entry = owner.watchlist.get(entry_id)
    if entry is None:
        await callback.answer("Watchlist entry not found.", show_alert=True)
        return
    content_available = bool(entry.content_id and catalog.get_content(entry.content_id))
    text, markup = watchlist_entry_detail(
        entry,
        owner,
        own=False,
        content_available=content_available,
        page=int(page_text),
    )
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.message(StateFilter(WatchlistAddState), ~F.text)
async def watchlist_non_text_input(message: Message) -> None:
    await message.answer("⌨️ Please send a text title, or use /cancel to stop.")

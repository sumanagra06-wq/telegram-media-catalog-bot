from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Config
from ..guards import access_denied_text, can_use_bot, ensure_registered
from ..panels import PanelManager
from ..presentation import ActionButton as InlineKeyboardButton
from ..repositories import CatalogRepository, UserRepository
from ..services import CatalogQueryService, SearchSessionStore
from ..ui import (
    admin_dashboard,
    panel_browse_categories,
    panel_workspace_home,
    search_results,
    watchlist_home,
)

router = Router(name="panel")
router.callback_query.filter(F.message.chat.type == "private")
DIVIDER = "━━━━━━━━━━━━━━━━━━"


async def _authorized_panel_callback(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
    panels: PanelManager,
    *,
    workspace_only: bool = False,
):
    profile, _ = await ensure_registered(callback.from_user, users, config)
    if not can_use_bot(profile, config):
        await callback.answer(access_denied_text(profile), show_alert=True)
        return None
    message_id = callback.message.message_id if callback.message else None
    valid = panels.is_workspace(callback.from_user.id, message_id)
    if not workspace_only:
        valid = valid or panels.is_dashboard(callback.from_user.id, message_id)
    if not valid:
        await callback.answer(
            "This panel is no longer active. Use /dashboard to repost your pinned dashboard.",
            show_alert=True,
        )
        return None
    return profile


async def _workspace_render(
    callback: CallbackQuery,
    panels: PanelManager,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    await panels.render_workspace(
        user_id=callback.from_user.id,
        text=text,
        reply_markup=markup,
    )


def _search_prompt() -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗂 Browse instead", callback_data="p:browse"))
    builder.row(
        InlineKeyboardButton(text="◀️ Workspace", callback_data="p:home"),
        InlineKeyboardButton(text="✖️ Close", callback_data="p:close"),
    )
    return (
        (
            "🔎 <b>SEARCH FOR A FILE</b>\n"
            "<blockquote>Delivery-only search • send a title next.</blockquote>\n"
            f"{DIVIDER}\n"
            "⌨️ Type a movie or series name in your next message.\n\n"
            "🎯 Results open file, season, episode, and version controls only.\n"
            "📚 To save titles, use the dedicated Watchlist tab.\n\n"
            "💡 Try <code>Dark</code> or <code>Dune 2021</code>."
        ),
        builder.as_markup(),
    )


@router.callback_query(F.data == "p:home")
async def workspace_home_callback(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
    panels: PanelManager,
    state: FSMContext,
) -> None:
    if await _authorized_panel_callback(callback, users, config, panels) is None:
        return
    await state.clear()
    text, markup = panel_workspace_home(config.is_owner(callback.from_user.id))
    await callback.answer()
    await _workspace_render(callback, panels, text, markup)


@router.callback_query(F.data == "p:search")
async def panel_search_callback(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
    panels: PanelManager,
    state: FSMContext,
) -> None:
    if await _authorized_panel_callback(callback, users, config, panels) is None:
        return
    await state.clear()
    text, markup = _search_prompt()
    await callback.answer()
    await _workspace_render(callback, panels, text, markup)


@router.callback_query(F.data == "p:browse")
async def panel_browse_callback(
    callback: CallbackQuery,
    catalog: CatalogRepository,
    users: UserRepository,
    config: Config,
    panels: PanelManager,
    state: FSMContext,
) -> None:
    if await _authorized_panel_callback(callback, users, config, panels) is None:
        return
    await state.clear()
    text, markup = panel_browse_categories(catalog.list_categories())
    await callback.answer()
    await _workspace_render(callback, panels, text, markup)


@router.callback_query(F.data.startswith("pb:"))
async def panel_browse_category_callback(
    callback: CallbackQuery,
    query: CatalogQueryService,
    sessions: SearchSessionStore,
    catalog: CatalogRepository,
    users: UserRepository,
    config: Config,
    panels: PanelManager,
) -> None:
    if (
        await _authorized_panel_callback(
            callback,
            users,
            config,
            panels,
            workspace_only=True,
        )
        is None
    ):
        return
    category_id = (callback.data or "").split(":", 1)[1]
    category = catalog.get_category(category_id)
    if category is None or not category.enabled:
        await callback.answer("Category is unavailable.", show_alert=True)
        return
    contents = query.browse_category(category.id)
    if not contents:
        text, markup = panel_browse_categories(catalog.list_categories())
        await callback.answer("No indexed titles are available in that collection yet.")
        await _workspace_render(callback, panels, text, markup)
        return
    session = sessions.create(
        callback.from_user.id,
        category.name,
        [content.id for content in contents],
        result_heading=f"BROWSE · {category.name.upper()}",
    )
    text, markup = search_results(
        session,
        contents,
        0,
        heading=f"BROWSE · {category.name.upper()}",
        prompt="Choose a title to view and receive its files:",
    )
    await callback.answer()
    await _workspace_render(callback, panels, text, markup)


@router.callback_query(F.data == "p:recent")
async def panel_recent_callback(
    callback: CallbackQuery,
    query: CatalogQueryService,
    sessions: SearchSessionStore,
    users: UserRepository,
    config: Config,
    panels: PanelManager,
    state: FSMContext,
) -> None:
    if await _authorized_panel_callback(callback, users, config, panels) is None:
        return
    await state.clear()
    contents = query.recently_added()
    if not contents:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="◀️ Workspace", callback_data="p:home"))
        builder.row(InlineKeyboardButton(text="✖️ Close", callback_data="p:close"))
        await callback.answer()
        await _workspace_render(
            callback,
            panels,
            "✨ <b>RECENTLY ADDED</b>\n"
            "<blockquote>Fresh arrivals will appear here.</blockquote>\n"
            f"{DIVIDER}\n"
            "🫙 <i>No titles have been indexed yet.</i>",
            builder.as_markup(),
        )
        return
    session = sessions.create(
        callback.from_user.id,
        "Recently added",
        [content.id for content in contents],
        result_heading="RECENTLY ADDED",
    )
    text, markup = search_results(session, contents, 0)
    await callback.answer()
    await _workspace_render(callback, panels, text, markup)


@router.callback_query(F.data == "p:watchlist")
async def panel_watchlist_callback(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
    panels: PanelManager,
    state: FSMContext,
) -> None:
    profile = await _authorized_panel_callback(callback, users, config, panels)
    if profile is None:
        return
    await state.clear()
    text, markup = watchlist_home(profile)
    await callback.answer()
    await _workspace_render(callback, panels, text, markup)


@router.callback_query(F.data == "p:help")
async def panel_help_callback(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
    panels: PanelManager,
    state: FSMContext,
) -> None:
    if await _authorized_panel_callback(callback, users, config, panels) is None:
        return
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔎 Start searching", callback_data="p:search"))
    builder.row(
        InlineKeyboardButton(text="◀️ Workspace", callback_data="p:home"),
        InlineKeyboardButton(text="✖️ Close", callback_data="p:close"),
    )
    await callback.answer()
    await _workspace_render(
        callback,
        panels,
        "❓ <b>HELP & QUICK GUIDE</b>\n"
        "<blockquote>Clean search, dedicated Watchlists, flat-chat delivery.</blockquote>\n"
        f"{DIVIDER}\n"
        "<b>1.</b> 🔎 Send a title or use Search\n"
        "<b>2.</b> 🎯 Open the matching title and choose its file\n"
        "<b>3.</b> 🔐 Receive the permanent protected file in this chat\n"
        "<b>4.</b> 📚 Use Watchlist—not Search—to save titles\n\n"
        "🧹 Queries and temporary cards are removed; one fresh dashboard follows delivery.\n"
        "⏱ Every workspace interaction restarts its 5-minute timer.",
        builder.as_markup(),
    )


@router.callback_query(F.data == "p:admin")
async def panel_admin_callback(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
    panels: PanelManager,
    state: FSMContext,
) -> None:
    if await _authorized_panel_callback(callback, users, config, panels) is None:
        return
    if not config.is_owner(callback.from_user.id):
        await callback.answer("Owner access required.", show_alert=True)
        return
    await state.clear()
    text, markup = admin_dashboard()
    await callback.answer()
    await _workspace_render(callback, panels, text, markup)


@router.callback_query(
    F.data.startswith("px:") | F.data.startswith("pa:") | F.data.startswith("pw:")
)
async def retired_non_watchlist_selection(callback: CallbackQuery) -> None:
    await callback.answer(
        "Watchlist additions now live only inside the Watchlist tab.",
        show_alert=True,
    )


@router.callback_query(F.data == "p:close")
async def panel_close_callback(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
    panels: PanelManager,
    state: FSMContext,
) -> None:
    if (
        await _authorized_panel_callback(
            callback,
            users,
            config,
            panels,
            workspace_only=True,
        )
        is None
    ):
        return
    await state.clear()
    if callback.message is None:
        return
    message_id = callback.message.message_id
    await callback.answer("Workspace closed. Your pinned dashboard is ready.")
    await panels.close_workspace(callback.from_user.id, message_id)

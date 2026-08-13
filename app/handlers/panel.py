from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Config
from ..guards import access_denied_text, can_use_bot, ensure_registered
from ..models import WatchStatus
from ..panels import PanelManager
from ..presentation import ActionButton as InlineKeyboardButton
from ..repositories import CatalogRepository, UserRepository
from ..services import CatalogQueryService, SearchSession, SearchSessionStore
from ..ui import (
    admin_dashboard,
    bulk_watchlist_status_picker,
    panel_browse_categories,
    panel_workspace_home,
    selectable_results,
    watchlist_home,
)
from ..utils import safe_html

router = Router(name="panel")
router.callback_query.filter(F.message.chat.type == "private")
DIVIDER = "━━━━━━━━━━━━━━━━━━"
STATUS_CODES = {
    "t": WatchStatus.TO_WATCH,
    "h": WatchStatus.ON_HOLD,
    "c": WatchStatus.COMPLETED,
}
STATUS_LABELS = {
    WatchStatus.TO_WATCH: "To watch",
    WatchStatus.ON_HOLD: "On hold",
    WatchStatus.COMPLETED: "Completed",
}


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
            "🔎 <b>SEARCH & MULTI-SELECT</b>\n"
            "<blockquote>No command needed—send a title next.</blockquote>\n"
            f"{DIVIDER}\n"
            "⌨️ Type a movie or series name in your next message.\n\n"
            "☑️ The result screen lets you tick up to 25 titles across pages, "
            "then add them to one Watchlist status together.\n\n"
            "💡 Try <code>Dark</code> or <code>Dune 2021</code>."
        ),
        builder.as_markup(),
    )


def _bulk_complete_markup() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📚 View my Watchlist",
            callback_data="menu:watchlist",
            style="primary",
        )
    )
    builder.row(
        InlineKeyboardButton(text="🔎 Search again", callback_data="p:search"),
        InlineKeyboardButton(text="🧭 Workspace", callback_data="p:home"),
    )
    builder.row(InlineKeyboardButton(text="✖️ Close workspace", callback_data="p:close"))
    return builder.as_markup()


def _session_contents(
    session: SearchSession,
    catalog: CatalogRepository,
):
    return [
        content
        for content_id in session.content_ids
        if (content := catalog.get_content(content_id)) is not None
    ]


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
        selectable=True,
        result_heading=f"BROWSE · {category.name.upper()}",
    )
    text, markup = selectable_results(session, contents, 0)
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
        selectable=True,
        result_heading="RECENTLY ADDED",
    )
    text, markup = selectable_results(session, contents, 0)
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
        "<blockquote>Search, select, save, and download.</blockquote>\n"
        f"{DIVIDER}\n"
        "<b>1.</b> 🔎 Send a title or use Search\n"
        "<b>2.</b> ☑️ Tick titles to save several together\n"
        "<b>3.</b> 🎯 Tap a title name to open its files\n"
        "<b>4.</b> ▶️ Choose a version for protected delivery\n\n"
        "📚 Watchlists support To watch, On hold, and Completed.\n"
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


@router.callback_query(F.data.startswith("px:"))
async def panel_toggle_selection(
    callback: CallbackQuery,
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
    _, token, content_id, page_text = (callback.data or "").split(":", 3)
    session = sessions.get(token, callback.from_user.id)
    if session is None or not session.selectable or content_id not in session.content_ids:
        await callback.answer("This selection expired. Start again.", show_alert=True)
        return
    if content_id in session.selected_content_ids:
        session.selected_content_ids.remove(content_id)
    elif len(session.selected_content_ids) >= 25:
        await callback.answer("You can select up to 25 titles at once.", show_alert=True)
        return
    else:
        session.selected_content_ids.add(content_id)
    contents = _session_contents(session, catalog)
    text, markup = selectable_results(session, contents, int(page_text))
    await callback.answer(
        f"{len(session.selected_content_ids)} selected",
    )
    await _workspace_render(callback, panels, text, markup)


@router.callback_query(F.data.startswith("pa:"))
async def panel_add_selected(
    callback: CallbackQuery,
    sessions: SearchSessionStore,
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
    _, token, page_text = (callback.data or "").split(":", 2)
    session = sessions.get(token, callback.from_user.id)
    if session is None or not session.selectable:
        await callback.answer("This selection expired. Start again.", show_alert=True)
        return
    if not session.selected_content_ids:
        await callback.answer("Select at least one title first.", show_alert=True)
        return
    text, markup = bulk_watchlist_status_picker(session, int(page_text))
    await callback.answer()
    await _workspace_render(callback, panels, text, markup)


@router.callback_query(F.data.startswith("pw:"))
async def panel_bulk_status_selected(
    callback: CallbackQuery,
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
    _, token, code, _page_text = (callback.data or "").split(":", 3)
    session = sessions.get(token, callback.from_user.id)
    status = STATUS_CODES.get(code)
    if session is None or not session.selectable or status is None:
        await callback.answer("This selection expired. Start again.", show_alert=True)
        return
    selected = []
    for content_id in session.content_ids:
        if content_id not in session.selected_content_ids:
            continue
        content = catalog.get_content(content_id)
        category = catalog.get_category(content.category_id) if content else None
        if content is None or category is None or not category.enabled:
            await callback.answer(
                "One selected title is no longer available. Review the selection and try again.",
                show_alert=True,
            )
            return
        selected.append((content, category.name))
    if not selected:
        await callback.answer("Select at least one available title first.", show_alert=True)
        return
    result = await users.bulk_upsert_catalog_watchlist(
        user_id=callback.from_user.id,
        items=selected,
        status=status,
    )
    session.selected_content_ids.clear()
    await callback.answer(f"✅ {len(result.entries)} titles saved.")
    await _workspace_render(
        callback,
        panels,
        "✅ <b>WATCHLIST UPDATED</b>\n"
        f"<blockquote>{len(result.entries)} title{'s' if len(result.entries) != 1 else ''} "
        "saved together</blockquote>\n"
        f"{DIVIDER}\n"
        f"📌 <b>Status</b>  •  {safe_html(STATUS_LABELS[status])}\n"
        f"➕ <b>New entries</b>  •  {result.created}\n"
        f"🔄 <b>Existing entries updated</b>  •  {result.updated}\n\n"
        "All selected titles were committed in one database update.",
        _bulk_complete_markup(),
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

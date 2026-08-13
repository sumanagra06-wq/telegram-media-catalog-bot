from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Config
from ..guards import access_denied_text, can_use_bot, ensure_registered
from ..models import ContentRecord, UserProfile, UserStatus
from ..panels import PanelManager
from ..presentation import ActionButton as InlineKeyboardButton
from ..repositories import CatalogRepository, UserRepository
from ..services import CatalogQueryService, SearchSession, SearchSessionStore
from ..ui import (
    CODE_WATCH,
    public_watchlist_directory,
    watchlist_add_method,
    watchlist_alphabet_picker,
    watchlist_category_picker,
    watchlist_custom_batch_preview,
    watchlist_custom_input,
    watchlist_entries,
    watchlist_entry_detail,
    watchlist_home,
    watchlist_library_filter,
    watchlist_library_results,
    watchlist_library_status_picker,
    watchlist_status_picker,
)
from ..utils import compact_label, normalize_title, safe_html
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
    catalog_filter = State()
    catalog_status = State()
    community_name = State()


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


def _watchlist_library_session(
    sessions: SearchSessionStore,
    token: str,
    user_id: int,
) -> SearchSession | None:
    session = sessions.get(token, user_id)
    if session is None or session.context != "watchlist_library":
        return None
    return session


def _watchlist_library_contents(
    session: SearchSession,
    catalog: CatalogRepository,
) -> list[ContentRecord]:
    return [
        content
        for content_id in session.content_ids
        if (content := catalog.get_content(content_id)) is not None
    ]


def _saved_catalog_ids(profile: UserProfile) -> set[str]:
    return {entry.content_id for entry in profile.watchlist.values() if entry.content_id}


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
    text, markup = watchlist_category_picker(catalog.list_categories(), "wamc", "Custom titles")
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
    text, markup = watchlist_custom_input(category.name)
    await edit_screen(callback, text, markup)


async def _render_custom_preview_message(
    message: Message,
    *,
    titles: list[str],
    selected: set[int],
    panels: PanelManager | None,
) -> None:
    text, markup = watchlist_custom_batch_preview(titles, selected)
    if message.from_user and panels:
        rendered = await panels.render_existing_workspace(
            user_id=message.from_user.id,
            text=text,
            reply_markup=markup,
        )
        if rendered:
            return
    await message.answer(text, reply_markup=markup)


@router.message(WatchlistAddState.manual_title, F.text, ~F.text.startswith("/"))
async def manual_title_received(
    message: Message,
    state: FSMContext,
    panels: PanelManager | None = None,
) -> None:
    titles: list[str] = []
    normalized_seen: set[str] = set()
    for line in message.text.splitlines():
        title = " ".join(line.split()).strip()
        if not title:
            continue
        if len(title) > 160:
            await message.answer("❌ Every title must be 160 characters or fewer.")
            return
        normalized = normalize_title(title)
        if normalized and normalized not in normalized_seen:
            normalized_seen.add(normalized)
            titles.append(title)
    if not titles:
        await message.answer("❌ Send at least one title, with one title per line.")
        return
    if len(titles) > 25:
        await message.answer("❌ Send no more than 25 unique titles at once.")
        return
    selected = set(range(len(titles)))
    await state.update_data(titles=titles, selected_indices=sorted(selected))
    await _render_custom_preview_message(
        message,
        titles=titles,
        selected=selected,
        panels=panels,
    )


def _custom_batch_state(data: dict[str, object]) -> tuple[list[str], set[int]] | None:
    raw_titles = data.get("titles")
    raw_selected = data.get("selected_indices")
    if not isinstance(raw_titles, list) or not all(isinstance(title, str) for title in raw_titles):
        return None
    if not isinstance(raw_selected, list) or not all(
        isinstance(index, int) for index in raw_selected
    ):
        return None
    titles = list(raw_titles)
    selected = {index for index in raw_selected if 0 <= index < len(titles)}
    return titles, selected


@router.callback_query(WatchlistAddState.manual_title, F.data.startswith("wctp:"))
async def manual_title_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    batch = _custom_batch_state(await state.get_data())
    if batch is None:
        await callback.answer("This custom-title batch expired.", show_alert=True)
        return
    titles, selected = batch
    try:
        index = int((callback.data or "").split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid title selection.", show_alert=True)
        return
    if not 0 <= index < len(titles):
        await callback.answer("Invalid title selection.", show_alert=True)
        return
    if index in selected:
        selected.remove(index)
    else:
        selected.add(index)
    await state.update_data(selected_indices=sorted(selected))
    text, markup = watchlist_custom_batch_preview(titles, selected)
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(WatchlistAddState.manual_title, F.data.startswith("wcta:"))
async def manual_title_select_all(callback: CallbackQuery, state: FSMContext) -> None:
    batch = _custom_batch_state(await state.get_data())
    if batch is None:
        await callback.answer("This custom-title batch expired.", show_alert=True)
        return
    titles, _ = batch
    action = (callback.data or "").split(":", 1)[1]
    selected = set(range(len(titles))) if action == "all" else set()
    await state.update_data(selected_indices=sorted(selected))
    text, markup = watchlist_custom_batch_preview(titles, selected)
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(WatchlistAddState.manual_title, F.data == "wct:continue")
async def manual_title_continue(callback: CallbackQuery, state: FSMContext) -> None:
    batch = _custom_batch_state(await state.get_data())
    if batch is None:
        await callback.answer("This custom-title batch expired.", show_alert=True)
        return
    titles, selected = batch
    selected_titles = [title for index, title in enumerate(titles) if index in selected]
    if not selected_titles:
        await callback.answer("Select at least one title.", show_alert=True)
        return
    await state.update_data(selected_titles=selected_titles)
    await state.set_state(WatchlistAddState.manual_status)
    text, markup = watchlist_status_picker(
        f"{len(selected_titles)} custom titles",
        "wams",
        plural=True,
    )
    await callback.answer()
    await edit_screen(callback, text, markup)


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
    titles = data.get("selected_titles")
    if (
        not isinstance(titles, list)
        or not titles
        or not all(isinstance(title, str) for title in titles)
        or not all(key in data for key in ("category_id", "category_name"))
    ):
        await state.clear()
        await callback.answer("This add-title session expired.", show_alert=True)
        return
    category = catalog.get_category(data["category_id"])
    if category is None or not category.enabled:
        await state.clear()
        await callback.answer("Category is no longer available.", show_alert=True)
        return
    result = await users.bulk_upsert_manual_watchlist(
        user_id=callback.from_user.id,
        titles=titles,
        category_id=category.id,
        category_name=category.name,
        status=status,
    )
    await state.clear()
    profile = users.get_user(callback.from_user.id)
    if profile is None:
        raise RuntimeError("Registered watchlist owner disappeared")
    text, markup = watchlist_home(profile)
    await callback.answer(f"✅ {result.created} added • {result.updated} updated")
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
    text, markup = watchlist_category_picker(
        catalog.list_categories(),
        "wlbc",
        "Choose a library collection",
    )
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlbc:"))
async def watchlist_library_category_selected(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    query: CatalogQueryService,
    sessions: SearchSessionStore,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    category_id = (callback.data or "").split(":", 1)[1]
    category = catalog.get_category(category_id)
    if category is None or not category.enabled:
        await callback.answer("Category is unavailable.", show_alert=True)
        return
    contents = query.browse_category(category.id)
    session = sessions.create(
        callback.from_user.id,
        category.name,
        [content.id for content in contents],
        result_heading="WATCHLIST LIBRARY",
        context="watchlist_library",
    )
    text, markup = watchlist_library_results(
        session,
        contents,
        0,
        saved_content_ids=_saved_catalog_ids(profile),
    )
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlbp:"))
async def watchlist_library_page(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    sessions: SearchSessionStore,
    config: Config,
    state: FSMContext,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    if await state.get_state() == WatchlistAddState.catalog_filter.state:
        await state.clear()
    _, token, page_text = (callback.data or "").split(":", 2)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    if session is None:
        await callback.answer(
            "This library selection expired. Open Add Title again.", show_alert=True
        )
        return
    contents = _watchlist_library_contents(session, catalog)
    available_ids = {content.id for content in contents}
    session.selected_content_ids.intersection_update(available_ids)
    text, markup = watchlist_library_results(
        session,
        contents,
        int(page_text),
        saved_content_ids=_saved_catalog_ids(profile),
    )
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlbt:"))
async def watchlist_library_toggle(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    sessions: SearchSessionStore,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    _, token, content_id, page_text = (callback.data or "").split(":", 3)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    if session is None or content_id not in session.content_ids:
        await callback.answer(
            "This library selection expired. Open Add Title again.", show_alert=True
        )
        return
    if catalog.get_content(content_id) is None:
        await callback.answer("That title is no longer available.", show_alert=True)
        return
    if content_id in session.selected_content_ids:
        session.selected_content_ids.remove(content_id)
    elif len(session.selected_content_ids) >= 25:
        await callback.answer("You can select up to 25 titles at once.", show_alert=True)
        return
    else:
        session.selected_content_ids.add(content_id)
    contents = _watchlist_library_contents(session, catalog)
    text, markup = watchlist_library_results(
        session,
        contents,
        int(page_text),
        saved_content_ids=_saved_catalog_ids(profile),
    )
    await callback.answer(f"{len(session.selected_content_ids)} selected")
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlfq:"))
async def watchlist_library_search_start(
    callback: CallbackQuery,
    users: UserRepository,
    sessions: SearchSessionStore,
    config: Config,
    state: FSMContext,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    _, token, page_text = (callback.data or "").split(":", 2)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    if session is None:
        await callback.answer("This library selection expired.", show_alert=True)
        return
    await state.set_state(WatchlistAddState.catalog_filter)
    await state.update_data(library_token=token, library_page=int(page_text))
    builder = InlineKeyboardBuilder()
    if session.text_filter:
        builder.row(
            InlineKeyboardButton(
                text="✖️ Clear current search",
                callback_data=f"wlfc:{token}:{page_text}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Back to titles",
            callback_data=f"wlbp:{token}:{page_text}",
        )
    )
    await callback.answer()
    await edit_screen(
        callback,
        "🔎 <b>SEARCH THIS COLLECTION</b>\n"
        f"<blockquote>{safe_html(session.query)} • selection is preserved</blockquote>\n"
        f"{DIVIDER}\n"
        "Send part of a title. The picker will show matching titles only.",
        builder.as_markup(),
    )


@router.message(WatchlistAddState.catalog_filter, F.text, ~F.text.startswith("/"))
async def watchlist_library_search_input(
    message: Message,
    users: UserRepository,
    catalog: CatalogRepository,
    sessions: SearchSessionStore,
    state: FSMContext,
    panels: PanelManager | None = None,
) -> None:
    data = await state.get_data()
    token = data.get("library_token")
    if message.from_user is None or not isinstance(token, str):
        await state.clear()
        await message.answer("This library search expired.")
        return
    session = _watchlist_library_session(sessions, token, message.from_user.id)
    if session is None:
        await state.clear()
        await message.answer("This library selection expired. Open Add Title again.")
        return
    search_text = " ".join(message.text.split()).strip()
    if not search_text or len(search_text) > 80:
        await message.answer("Send a search between 1 and 80 characters.")
        return
    session.text_filter = search_text
    session.alphabet_filter = None
    session.selected_only = False
    profile = users.get_user(message.from_user.id)
    if profile is None:
        await state.clear()
        await message.answer("Your user session expired.")
        return
    await state.clear()
    text, markup = watchlist_library_results(
        session,
        _watchlist_library_contents(session, catalog),
        0,
        saved_content_ids=_saved_catalog_ids(profile),
    )
    if panels:
        rendered = await panels.render_existing_workspace(
            user_id=message.from_user.id,
            text=text,
            reply_markup=markup,
        )
        if rendered:
            return
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("wlfc:"))
async def watchlist_library_search_clear(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    sessions: SearchSessionStore,
    config: Config,
    state: FSMContext,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    _, token, _page_text = (callback.data or "").split(":", 2)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    if session is None:
        await callback.answer("This library selection expired.", show_alert=True)
        return
    session.text_filter = None
    session.alphabet_filter = None
    await state.clear()
    text, markup = watchlist_library_results(
        session,
        _watchlist_library_contents(session, catalog),
        0,
        saved_content_ids=_saved_catalog_ids(profile),
    )
    await callback.answer("Search cleared.")
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wluo:"))
async def watchlist_library_unsaved_toggle(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    sessions: SearchSessionStore,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    _, token, _page_text = (callback.data or "").split(":", 2)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    if session is None:
        await callback.answer("This library selection expired.", show_alert=True)
        return
    session.only_unsaved = not session.only_unsaved
    text, markup = watchlist_library_results(
        session,
        _watchlist_library_contents(session, catalog),
        0,
        saved_content_ids=_saved_catalog_ids(profile),
    )
    await callback.answer(
        "Showing unsaved titles only." if session.only_unsaved else "Showing all titles."
    )
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlrv:"))
async def watchlist_library_review_toggle(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    sessions: SearchSessionStore,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    _, token, _page_text = (callback.data or "").split(":", 2)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    if session is None:
        await callback.answer("This library selection expired.", show_alert=True)
        return
    session.selected_only = not session.selected_only
    text, markup = watchlist_library_results(
        session,
        _watchlist_library_contents(session, catalog),
        0,
        saved_content_ids=_saved_catalog_ids(profile),
    )
    await callback.answer(
        "Reviewing selected titles." if session.selected_only else "Showing the picker."
    )
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlsp:"))
async def watchlist_library_select_page(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    sessions: SearchSessionStore,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    _, token, page_text = (callback.data or "").split(":", 2)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    if session is None:
        await callback.answer("This library selection expired.", show_alert=True)
        return
    contents = _watchlist_library_contents(session, catalog)
    filtered = watchlist_library_filter(session, contents, _saved_catalog_ids(profile))
    page = max(0, int(page_text))
    visible = filtered[page * 6 : (page + 1) * 6]
    remaining = 25 - len(session.selected_content_ids)
    for content in visible:
        if content.id not in session.selected_content_ids and remaining > 0:
            session.selected_content_ids.add(content.id)
            remaining -= 1
    text, markup = watchlist_library_results(
        session,
        contents,
        page,
        saved_content_ids=_saved_catalog_ids(profile),
    )
    await callback.answer(f"{len(session.selected_content_ids)} selected")
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlcl:"))
async def watchlist_library_clear_selected(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    sessions: SearchSessionStore,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    _, token, page_text = (callback.data or "").split(":", 2)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    if session is None:
        await callback.answer("This library selection expired.", show_alert=True)
        return
    session.selected_content_ids.clear()
    text, markup = watchlist_library_results(
        session,
        _watchlist_library_contents(session, catalog),
        int(page_text),
        saved_content_ids=_saved_catalog_ids(profile),
    )
    await callback.answer("Selection cleared.")
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlba:"))
async def watchlist_library_alphabet(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    sessions: SearchSessionStore,
    config: Config,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    _, token, _page_text = (callback.data or "").split(":", 2)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    if session is None:
        await callback.answer(
            "This library selection expired. Open Add Title again.", show_alert=True
        )
        return
    text, markup = watchlist_alphabet_picker(
        session,
        _watchlist_library_contents(session, catalog),
    )
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlaf:"))
async def watchlist_library_alphabet_selected(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    sessions: SearchSessionStore,
    config: Config,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    _, token, code = (callback.data or "").split(":", 2)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    if session is None:
        await callback.answer(
            "This library selection expired. Open Add Title again.", show_alert=True
        )
        return
    if code == "*":
        session.alphabet_filter = None
    elif code == "0":
        session.alphabet_filter = "#"
    elif len(code) == 1 and "A" <= code <= "Z":
        session.alphabet_filter = code
    else:
        await callback.answer("Invalid alphabet filter.", show_alert=True)
        return
    contents = _watchlist_library_contents(session, catalog)
    text, markup = watchlist_library_results(
        session,
        contents,
        0,
        saved_content_ids=_saved_catalog_ids(profile),
    )
    await callback.answer(f"Alphabet: {session.alphabet_filter or 'All'}")
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlbd:"))
async def watchlist_library_add_selected(
    callback: CallbackQuery,
    users: UserRepository,
    sessions: SearchSessionStore,
    config: Config,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    _, token, page_text = (callback.data or "").split(":", 2)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    if session is None:
        await callback.answer(
            "This library selection expired. Open Add Title again.", show_alert=True
        )
        return
    if not session.selected_content_ids:
        await callback.answer("Select at least one title first.", show_alert=True)
        return
    text, markup = watchlist_library_status_picker(session, int(page_text))
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("wlbs:"))
async def watchlist_library_status_selected(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    sessions: SearchSessionStore,
    config: Config,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    _, token, code, _page_text = (callback.data or "").split(":", 3)
    session = _watchlist_library_session(sessions, token, callback.from_user.id)
    status = CODE_WATCH.get(code)
    if session is None or status is None:
        await callback.answer(
            "This library selection expired. Open Add Title again.", show_alert=True
        )
        return
    selected = []
    for content_id in session.content_ids:
        if content_id not in session.selected_content_ids:
            continue
        content = catalog.get_content(content_id)
        category = catalog.get_category(content.category_id) if content else None
        if content is None or category is None or not category.enabled:
            await callback.answer(
                "One selected title is unavailable. Review the selection and try again.",
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
    profile = users.get_user(callback.from_user.id)
    if profile is None:
        raise RuntimeError("Registered watchlist owner disappeared")
    text, markup = watchlist_home(profile)
    await callback.answer(
        f"✅ {result.created} added · {result.updated} updated",
    )
    await edit_screen(callback, text, markup)


# Legacy typed-search catalog callbacks remain available for existing cards.
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


@router.message(WatchlistAddState.catalog_query, F.text, ~F.text.startswith("/"))
async def catalog_title_query(
    message: Message,
    query: CatalogQueryService,
    catalog: CatalogRepository,
    state: FSMContext,
    panels: PanelManager | None = None,
) -> None:
    data = await state.get_data()
    category_id = data.get("category_id")
    matches = [
        hit.content for hit in query.search(message.text) if hit.content.category_id == category_id
    ][:8]
    if not matches:
        await message.answer(
            "🔍 <b>NO CATALOG MATCH</b>\n"
            "Try fewer words or check the spelling. Use the Cancel button to stop."
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
    text = (
        "🎯 <b>CHOOSE A CATALOG TITLE</b>\n"
        f"<blockquote>{len(matches)} matching title{'s' if len(matches) != 1 else ''}</blockquote>\n"
        f"{DIVIDER}\n"
        "Select the correct title below:"
    )
    markup = builder.as_markup()
    if message.from_user and panels:
        rendered = await panels.render_existing_workspace(
            user_id=message.from_user.id,
            text=text,
            reply_markup=markup,
        )
        if rendered:
            return
    await message.answer(text, reply_markup=markup)


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


@router.callback_query(F.data == "wln:edit")
async def community_name_start(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
    state: FSMContext,
) -> None:
    profile = await _active_callback(callback, users, config)
    if profile is None:
        return
    await state.set_state(WatchlistAddState.community_name)
    builder = InlineKeyboardBuilder()
    if profile.watchlist_display_name:
        builder.row(
            InlineKeyboardButton(
                text="↩️ Use my Telegram name",
                callback_data="wln:reset",
            )
        )
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="menu:watchlist"))
    await callback.answer()
    await edit_screen(
        callback,
        "✏️ <b>CHANGE COMMUNITY NAME</b>\n"
        "<blockquote>This name appears beside your public Watchlist.</blockquote>\n"
        f"{DIVIDER}\n"
        "Send a display name up to 40 characters. It does not change your Telegram profile.",
        builder.as_markup(),
    )


@router.message(WatchlistAddState.community_name, F.text, ~F.text.startswith("/"))
async def community_name_received(
    message: Message,
    users: UserRepository,
    state: FSMContext,
    panels: PanelManager | None = None,
) -> None:
    if message.from_user is None:
        return
    try:
        profile = await users.set_watchlist_display_name(message.from_user.id, message.text)
    except ValueError as exc:
        await message.answer(f"❌ {safe_html(exc)}")
        return
    await state.clear()
    text, markup = watchlist_home(profile)
    if panels and await panels.render_existing_workspace(
        user_id=message.from_user.id,
        text=text,
        reply_markup=markup,
    ):
        return
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "wln:reset")
async def community_name_reset(
    callback: CallbackQuery,
    users: UserRepository,
    config: Config,
    state: FSMContext,
) -> None:
    if await _active_callback(callback, users, config) is None:
        return
    profile = await users.set_watchlist_display_name(callback.from_user.id, None)
    await state.clear()
    text, markup = watchlist_home(profile)
    await callback.answer("Community name reset.")
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
    is_public = (callback.data or "").split(":", 1)[1] == "1"
    try:
        updated = await users.set_watchlist_visibility(profile.telegram_user_id, is_public)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    text, markup = watchlist_home(updated)
    await callback.answer("🌐 Community Watchlists are always public.")
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
    if owner is None or owner.status != UserStatus.ACTIVE:
        await callback.answer("This watchlist is unavailable.", show_alert=True)
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
    if owner is None or owner.status != UserStatus.ACTIVE:
        await callback.answer("This watchlist is unavailable.", show_alert=True)
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
    await message.answer("⌨️ Please send a text title, or tap Cancel to stop.")

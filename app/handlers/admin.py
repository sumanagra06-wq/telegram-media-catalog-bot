from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Config
from ..filters import NotOwnerFilter, OwnerFilter
from ..ingestion import CatalogIngestBatcher, IndexAuditBatcher
from ..models import AccessMode, CategoryMode, RemovedSourceRecord, UserStatus
from ..panels import PanelManager
from ..presentation import ActionButton as InlineKeyboardButton
from ..repositories import CatalogRepository, UserRepository
from ..storage import StorageError
from ..ui import (
    access_mode_panel,
    admin_categories,
    admin_category_detail,
    admin_dashboard,
    page_slice,
    panel_dashboard,
    user_detail,
    users_panel,
)
from ..utils import compact_label, safe_html
from .channel import index_source_message
from .common import edit_screen

LOGGER = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
owner = OwnerFilter()
not_owner = NotOwnerFilter()
DIVIDER = "━━━━━━━━━━━━━━━━━━"
_BROADCASTS_IN_PROGRESS: set[int] = set()


def _default_category_mode(name: str) -> CategoryMode:
    normalized = name.casefold()
    if any(word in normalized for word in ("series", "show", "tv", "anime")):
        return CategoryMode.EPISODIC
    if any(word in normalized for word in ("movie", "film")):
        return CategoryMode.SINGLE
    return CategoryMode.MIXED


class AdminState(StatesGroup):
    category_name = State()
    category_channel = State()
    category_confirm = State()
    category_rename = State()
    category_change_channel = State()
    user_find = State()
    user_community_name = State()
    broadcast_input = State()
    broadcast_confirm = State()


async def _workspace_or_answer(
    message: Message,
    text: str,
    panels: PanelManager | None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if message.from_user and panels:
        rendered = await panels.render_existing_workspace(
            user_id=message.from_user.id,
            text=text,
            reply_markup=reply_markup,
        )
        if rendered:
            return
    await message.answer(text, reply_markup=reply_markup)


async def _validate_private_channel(bot: Bot, channel_id: int, config: Config) -> str:
    if channel_id in {config.file_database_channel_id, config.user_database_channel_id}:
        raise ValueError("A database channel cannot also be a category storage channel")
    try:
        chat = await bot.get_chat(channel_id)
    except Exception as exc:
        raise ValueError("Channel not found. Add the bot as administrator first.") from exc
    if chat.type != "channel":
        raise ValueError("The supplied ID is not a Telegram channel")
    if chat.username:
        raise ValueError("The storage channel must be private and have no public username")
    member = await bot.get_chat_member(channel_id, (await bot.get_me()).id)
    if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        raise ValueError("The bot must be an administrator in the storage channel")
    if member.status == ChatMemberStatus.ADMINISTRATOR and not getattr(
        member, "can_delete_messages", False
    ):
        raise ValueError(
            "The bot needs Delete Messages permission for owner-requested permanent removal"
        )
    return chat.title or str(channel_id)


async def _show_add_confirmation(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    name: str,
    raw_channel_id: str,
    panels: PanelManager | None = None,
) -> None:
    try:
        channel_id = int(raw_channel_id.strip())
        title = await _validate_private_channel(bot, channel_id, config)
    except (ValueError, TypeError) as exc:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="admin:categories"))
        await _workspace_or_answer(
            message,
            "❌ <b>CHANNEL VALIDATION FAILED</b>\n"
            f"<blockquote>{safe_html(exc)}</blockquote>\n"
            f"{DIVIDER}\n"
            "Send a valid private channel ID, or tap Cancel to stop.",
            panels,
            builder.as_markup(),
        )
        return
    mode = _default_category_mode(name)
    await state.set_state(AdminState.category_confirm)
    await state.update_data(
        category_name=name,
        category_channel_id=channel_id,
        channel_title=title,
        category_mode=mode.value,
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Create category", callback_data="aca:confirm"),
        InlineKeyboardButton(text="Cancel", callback_data="aca:cancel"),
    )
    await _workspace_or_answer(
        message,
        "✅ <b>CONFIRM NEW CATEGORY</b>\n"
        "<blockquote>Review the source before creating it.</blockquote>\n"
        f"{DIVIDER}\n"
        f"🗂 <b>Name</b>  •  {safe_html(name)}\n"
        f"📡 <b>Channel</b>  •  {safe_html(title)}\n"
        f"🔢 <b>Channel ID</b>  •  <code>{channel_id}</code>\n"
        f"⚙️ <b>Mode</b>  •  {safe_html(mode.value.title())}",
        panels,
        builder.as_markup(),
    )


@router.callback_query(F.data == "admin:home", owner)
async def admin_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    text, markup = admin_dashboard()
    await edit_screen(callback, text, markup)


def _broadcast_kind(message: Message) -> str | None:
    if message.text:
        return "Text message"
    if message.photo:
        return "Photo"
    if message.video:
        return "Video"
    if message.document:
        return "Document"
    return None


def _broadcast_excerpt(message: Message) -> str:
    value = " ".join((message.text or message.caption or "").split()).strip()
    if not value:
        return "Media without a caption"
    return f"{value[:157]}…" if len(value) > 160 else value


async def _copy_broadcast_message(
    bot: Bot,
    *,
    user_id: int,
    source_chat_id: int,
    source_message_id: int,
) -> bool:
    for attempt in range(3):
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                reply_markup=None,
            )
            return True
        except TelegramRetryAfter as exc:
            if attempt == 2:
                return False
            delay = min(max(float(exc.retry_after), 0.1), 30.0)
        except (TelegramNetworkError, TelegramServerError):
            if attempt == 2:
                return False
            delay = 0.25 * (2**attempt)
        except TelegramAPIError:
            return False
        await asyncio.sleep(delay)
    return False


async def _cleanup_broadcast_draft(bot: Bot, chat_id: int | None, message_id: int | None) -> None:
    if chat_id is None or message_id is None:
        return
    with suppress(TelegramAPIError):
        await bot.delete_message(chat_id, message_id)


@router.callback_query(F.data == "ab:start", owner)
async def broadcast_start(
    callback: CallbackQuery,
    state: FSMContext,
    users: UserRepository,
) -> None:
    await state.clear()
    await state.set_state(AdminState.broadcast_input)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="ab:cancel"))
    await callback.answer()
    await edit_screen(
        callback,
        "📣 <b>NEW BROADCAST</b>\n"
        "<blockquote>Owner-only announcement composer</blockquote>\n"
        f"{DIVIDER}\n"
        f"👥 Registered recipients: <b>{len(users.list_users())}</b>\n\n"
        "Send or forward one text, photo, video, or document. You can also reply to an "
        "existing message with any short trigger text; the replied-to message will be copied.\n\n"
        "Nothing is sent until the confirmation screen.",
        builder.as_markup(),
    )


@router.message(AdminState.broadcast_input, owner, ~F.text.startswith("/"))
async def broadcast_input(
    message: Message,
    state: FSMContext,
    users: UserRepository,
    panels: PanelManager | None = None,
) -> None:
    replied = message.reply_to_message
    source = replied if replied is not None and _broadcast_kind(replied) else message
    kind = _broadcast_kind(source)
    if kind is None:
        await message.answer("Send or reply to one text, photo, video, or document.")
        return
    await state.set_state(AdminState.broadcast_confirm)
    await state.update_data(
        broadcast_source_chat_id=source.chat.id,
        broadcast_source_message_id=source.message_id,
        broadcast_cleanup_chat_id=message.chat.id,
        broadcast_cleanup_message_id=message.message_id,
    )
    recipients = len(users.list_users())
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"📣 Send to {recipients} users",
            callback_data="ab:send",
            style="success",
        )
    )
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="ab:cancel"))
    await _workspace_or_answer(
        message,
        "📣 <b>CONFIRM BROADCAST</b>\n"
        "<blockquote>One announcement • every registered account</blockquote>\n"
        f"{DIVIDER}\n"
        f"🧾 <b>Type</b>  •  {kind}\n"
        f"👥 <b>Recipients</b>  •  {recipients}\n"
        f"👁 <b>Preview</b>  •  {safe_html(_broadcast_excerpt(source))}\n"
        f"{DIVIDER}\n"
        "The message will be copied without action buttons. After each successful copy, "
        "one fresh pinned dashboard will replace that user's previous dashboard.",
        panels,
        builder.as_markup(),
    )


@router.callback_query(F.data == "ab:cancel", owner)
async def broadcast_cancel(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    await state.clear()
    await _cleanup_broadcast_draft(
        bot,
        data.get("broadcast_cleanup_chat_id"),
        data.get("broadcast_cleanup_message_id"),
    )
    await callback.answer("Broadcast cancelled.")
    text, markup = admin_dashboard()
    await edit_screen(callback, text, markup)


@router.callback_query(
    F.data == "ab:send",
    owner,
    StateFilter(AdminState.broadcast_confirm),
)
async def broadcast_send(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
    panels: PanelManager | None = None,
) -> None:
    owner_id = callback.from_user.id
    if owner_id in _BROADCASTS_IN_PROGRESS:
        await callback.answer("A broadcast is already in progress.", show_alert=True)
        return
    _BROADCASTS_IN_PROGRESS.add(owner_id)
    try:
        await _execute_broadcast_send(callback, bot, state, users, catalog, config, panels)
    finally:
        _BROADCASTS_IN_PROGRESS.discard(owner_id)


async def _execute_broadcast_send(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
    panels: PanelManager | None,
) -> None:
    data = await state.get_data()
    source_chat_id = data.get("broadcast_source_chat_id")
    source_message_id = data.get("broadcast_source_message_id")
    cleanup_chat_id = data.get("broadcast_cleanup_chat_id")
    cleanup_message_id = data.get("broadcast_cleanup_message_id")
    if not isinstance(source_chat_id, int) or not isinstance(source_message_id, int):
        await state.clear()
        await callback.answer("This broadcast preview expired.", show_alert=True)
        return

    recipients = sorted(users.list_users(), key=lambda item: item.telegram_user_id)
    await state.clear()
    await callback.answer("Broadcast started…")
    await edit_screen(
        callback,
        "📡 <b>BROADCAST IN PROGRESS</b>\n"
        f"<blockquote>Sending to {len(recipients)} registered users</blockquote>\n"
        f"{DIVIDER}\n"
        "Please keep this workspace open until the final delivery report appears.",
        None,
    )

    delivered = []
    failed = 0
    for index, profile in enumerate(recipients):
        copied = await _copy_broadcast_message(
            bot,
            user_id=profile.telegram_user_id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
        )
        if copied:
            delivered.append(profile)
        else:
            failed += 1
            LOGGER.info("Broadcast copy failed for user %s", profile.telegram_user_id)
        if index + 1 < len(recipients):
            await asyncio.sleep(0.05)

    dashboards_refreshed = 0
    dashboard_failures = 0
    if panels is not None and delivered:
        dashboard_requests = []
        for profile in delivered:
            text, markup = panel_dashboard(
                config.is_owner(profile.telegram_user_id),
                profile.first_name,
            )
            dashboard_requests.append((profile.telegram_user_id, text, markup))
        try:
            dashboards_refreshed, dashboard_failures = await panels.repost_dashboards(
                dashboard_requests
            )
        except StorageError:
            dashboard_failures = len(delivered)
            LOGGER.warning(
                "Broadcast delivered but dashboard batch persistence failed", exc_info=True
            )

    await _cleanup_broadcast_draft(bot, cleanup_chat_id, cleanup_message_id)
    audit_failed = False
    try:
        await catalog.add_audit(
            "broadcast.send",
            f"Broadcast complete: recipients={len(recipients)} sent={len(delivered)} "
            f"failed={failed} dashboards={dashboards_refreshed} "
            f"dashboard_failed={dashboard_failures}",
            callback.from_user.id,
        )
    except StorageError:
        audit_failed = True
        LOGGER.exception("Broadcast completed but its catalog audit could not be persisted")
    icon = "✅" if failed == 0 and dashboard_failures == 0 and not audit_failed else "⚠️"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📣 Send another", callback_data="ab:start"))
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    await edit_screen(
        callback,
        f"{icon} <b>BROADCAST COMPLETE</b>\n"
        "<blockquote>Owner announcement delivery report</blockquote>\n"
        f"{DIVIDER}\n"
        f"✅ <b>Messages delivered</b>  •  {len(delivered)}\n"
        f"❌ <b>Message failures</b>  •  {failed}\n"
        f"📌 <b>Dashboards refreshed</b>  •  {dashboards_refreshed}\n"
        f"⚠️ <b>Dashboard failures</b>  •  {dashboard_failures}\n"
        f"🧾 <b>Audit saved</b>  •  {'No' if audit_failed else 'Yes'}\n"
        f"{DIVIDER}\n"
        "Failures usually mean the user blocked the bot or Telegram rejected the destination.",
        builder.as_markup(),
    )


@router.message(Command("index"), owner)
async def manual_index_command(
    message: Message,
    bot: Bot,
    config: Config,
    catalog: CatalogRepository,
    ingest_batcher: CatalogIngestBatcher,
    index_audit_batcher: IndexAuditBatcher,
) -> None:
    source = message.reply_to_message
    if source is None or not (source.video or source.document):
        await message.answer(
            "Forward a video or document from a registered storage channel, then reply "
            "to the forwarded message with /index."
        )
        return
    origin = source.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        await message.answer(
            "The original channel reference is unavailable. Ensure storage-channel content "
            "protection is disabled before forwarding."
        )
        return
    category = catalog.category_for_channel(origin.chat.id)
    if category is None:
        await message.answer("That source channel is not registered as a storage category.")
        return
    indexed = await index_source_message(
        source,
        bot,
        config,
        catalog,
        source_chat_id=origin.chat.id,
        source_message_id=origin.message_id,
        source_title=origin.chat.title or str(origin.chat.id),
        allow_legacy=True,
        ingest_batcher=ingest_batcher,
        index_audit_batcher=index_audit_batcher,
    )
    if indexed:
        await message.answer("✅ The original channel post has been indexed.")
    else:
        await message.answer(
            "The file could not be indexed. Open Admin Control Center → Failures for details."
        )


@router.callback_query(F.data == "admin:categories", owner)
async def categories_callback(
    callback: CallbackQuery,
    catalog: CatalogRepository,
    state: FSMContext,
) -> None:
    await state.clear()
    await callback.answer()
    text, markup = admin_categories(catalog.list_categories(include_disabled=True))
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("ac:"), owner)
async def category_detail_callback(
    callback: CallbackQuery,
    catalog: CatalogRepository,
    state: FSMContext,
) -> None:
    await state.clear()
    category = catalog.get_category(callback.data.split(":", 1)[1])
    if category is None:
        await callback.answer("Category not found.", show_alert=True)
        return
    await callback.answer()
    text, markup = admin_category_detail(category)
    await edit_screen(callback, text, markup)


@router.callback_query(F.data == "aca:start", owner)
async def category_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AdminState.category_name)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="admin:categories"))
    await callback.answer()
    await edit_screen(
        callback,
        "➕ <b>ADD A CATEGORY</b>\n"
        "<blockquote>Step 1 of 2 • choose a display name</blockquote>\n"
        f"{DIVIDER}\n"
        "Send a short category name, such as <code>Movies</code> or <code>Anime</code>.\n\n"
        "Use the Cancel button to stop.",
        builder.as_markup(),
    )


@router.message(AdminState.category_name, owner, F.text, ~F.text.startswith("/"))
async def category_name_input(
    message: Message,
    state: FSMContext,
    panels: PanelManager | None = None,
) -> None:
    name = " ".join(message.text.split()).strip()
    if not name or len(name) > 60:
        await message.answer("Use a category name between 1 and 60 characters.")
        return
    await state.update_data(category_name=name)
    await state.set_state(AdminState.category_channel)
    await _workspace_or_answer(
        message,
        "📡 <b>CONNECT STORAGE CHANNEL</b>\n"
        "<blockquote>Step 2 of 2 • private source channel</blockquote>\n"
        f"{DIVIDER}\n"
        "Send the private channel’s numeric ID.\n\n"
        "✅ The bot must already be an administrator with permission to delete messages.",
        panels,
    )


@router.message(AdminState.category_channel, owner, F.text, ~F.text.startswith("/"))
async def category_channel_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    config: Config,
    panels: PanelManager | None = None,
) -> None:
    data = await state.get_data()
    await _show_add_confirmation(
        message,
        state,
        bot,
        config,
        data["category_name"],
        message.text,
        panels,
    )


@router.callback_query(F.data == "aca:confirm", owner, StateFilter(AdminState.category_confirm))
async def category_add_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    catalog: CatalogRepository,
) -> None:
    data = await state.get_data()
    try:
        category = await catalog.add_category(
            data["category_name"],
            data["category_channel_id"],
            data["channel_title"],
            CategoryMode(data["category_mode"]),
            callback.from_user.id,
        )
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.clear()
    await callback.answer("Category created.")
    text, markup = admin_category_detail(category)
    await edit_screen(callback, text, markup)


@router.callback_query(F.data == "aca:cancel", owner)
async def category_add_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Cancelled.")
    text, markup = admin_dashboard()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("acr:"), owner)
async def category_rename_start(callback: CallbackQuery, state: FSMContext) -> None:
    category_id = callback.data.split(":", 1)[1]
    await state.set_state(AdminState.category_rename)
    await state.update_data(category_id=category_id)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data=f"ac:{category_id}"))
    await callback.answer()
    await edit_screen(
        callback,
        "✏️ <b>RENAME CATEGORY</b>\n"
        "<blockquote>Update the display name without changing its files.</blockquote>\n"
        f"{DIVIDER}\n"
        "Send the new category name, or tap Cancel to stop.",
        builder.as_markup(),
    )


@router.message(AdminState.category_rename, owner, F.text, ~F.text.startswith("/"))
async def category_rename_input(
    message: Message,
    state: FSMContext,
    catalog: CatalogRepository,
    panels: PanelManager | None = None,
) -> None:
    data = await state.get_data()
    try:
        category = await catalog.rename_category(
            data["category_id"], message.text, message.from_user.id
        )
    except ValueError as exc:
        await message.answer(f"❌ {safe_html(exc)}")
        return
    await state.clear()
    text, markup = admin_category_detail(category)
    await _workspace_or_answer(message, text, panels, markup)


@router.callback_query(F.data.startswith("acc:"), owner)
async def category_channel_start(callback: CallbackQuery, state: FSMContext) -> None:
    category_id = callback.data.split(":", 1)[1]
    await state.set_state(AdminState.category_change_channel)
    await state.update_data(category_id=category_id)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data=f"ac:{category_id}"))
    await callback.answer()
    await edit_screen(
        callback,
        "🔄 <b>CHANGE STORAGE CHANNEL</b>\n"
        "<blockquote>Connect a new private source channel.</blockquote>\n"
        f"{DIVIDER}\n"
        "Send the new private channel ID.\n\n"
        "🗄 The previous channel remains registered as a legacy source for existing files.",
        builder.as_markup(),
    )


@router.message(AdminState.category_change_channel, owner, F.text, ~F.text.startswith("/"))
async def category_channel_change_input(
    message: Message,
    state: FSMContext,
    catalog: CatalogRepository,
    bot: Bot,
    config: Config,
    panels: PanelManager | None = None,
) -> None:
    try:
        channel_id = int(message.text.strip())
        title = await _validate_private_channel(bot, channel_id, config)
        data = await state.get_data()
        category = await catalog.change_category_channel(
            data["category_id"], channel_id, title, message.from_user.id
        )
    except (ValueError, TypeError) as exc:
        await message.answer(f"❌ {safe_html(exc)}")
        return
    await state.clear()
    text, markup = admin_category_detail(category)
    await _workspace_or_answer(message, text, panels, markup)


@router.callback_query(F.data.startswith("acm:"), owner)
async def category_mode_start(callback: CallbackQuery) -> None:
    category_id = callback.data.split(":", 1)[1]
    builder = InlineKeyboardBuilder()
    for mode in CategoryMode:
        builder.row(
            InlineKeyboardButton(
                text=f"⚙️ {mode.value.title()}",
                callback_data=f"acms:{category_id}:{mode.value}",
                style="primary",
            )
        )
    builder.row(InlineKeyboardButton(text="Cancel", callback_data=f"ac:{category_id}"))
    await callback.answer()
    await edit_screen(
        callback,
        "⚙️ <b>CHANGE CATEGORY MODE</b>\n"
        "<blockquote>Choose how filenames in this channel are interpreted.</blockquote>\n"
        f"{DIVIDER}\n"
        "🎬 <b>Single</b>  •  movies and standalone files\n"
        "📺 <b>Episodic</b>  •  seasons and episodes\n"
        "🗂 <b>Mixed</b>  •  automatically detect both",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("acms:"), owner)
async def category_mode_set(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    _, category_id, mode_text = callback.data.split(":", 2)
    category = await catalog.set_category_mode(
        category_id, CategoryMode(mode_text), callback.from_user.id
    )
    await callback.answer("Category mode updated.")
    text, markup = admin_category_detail(category)
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("act:"), owner)
async def category_toggle_confirm(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    category_id = callback.data.split(":", 1)[1]
    category = catalog.get_category(category_id)
    if category is None:
        await callback.answer("Category not found.", show_alert=True)
        return
    action = "disable" if category.enabled else "enable"
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"{'✅' if action == 'enable' else '⏸'} Confirm {action}",
            callback_data=f"actc:{category_id}",
            style="success" if action == "enable" else None,
        ),
        InlineKeyboardButton(text="✖️ Cancel", callback_data=f"ac:{category_id}"),
    )
    impact = (
        "Users will see and search this category."
        if action == "enable"
        else "The category will be hidden from browsing and search until re-enabled."
    )
    await callback.answer()
    await edit_screen(
        callback,
        f"{'✅' if action == 'enable' else '⏸'} <b>CONFIRM {action.upper()}</b>\n"
        f"<blockquote>{safe_html(category.name)}</blockquote>\n"
        f"{DIVIDER}\n"
        f"ℹ️ {impact}",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("actc:"), owner)
async def category_toggle(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    category_id = callback.data.split(":", 1)[1]
    category = catalog.get_category(category_id)
    if category is None:
        await callback.answer("Category not found.", show_alert=True)
        return
    updated = await catalog.set_category_enabled(
        category_id, not category.enabled, callback.from_user.id
    )
    await callback.answer("Category updated.")
    text, markup = admin_category_detail(updated)
    await edit_screen(callback, text, markup)


async def _stats_text(catalog: CatalogRepository, users: UserRepository) -> str:
    cstate = catalog.snapshot()
    ustate = users.snapshot()
    available = sum(1 for item in cstate.files.values() if item.available)
    return (
        "📊 <b>LIBRARY OVERVIEW</b>\n"
        "<blockquote>Live catalog and community totals.</blockquote>\n"
        f"{DIVIDER}\n"
        f"🗂 <b>Categories</b>  •  {len(cstate.categories)}\n"
        f"🎬 <b>Titles</b>  •  {len(cstate.contents)}\n"
        f"🎞 <b>Files</b>  •  {len(cstate.files)}\n"
        f"✅ <b>Available</b>  •  {available}\n"
        f"⚠️ <b>Index failures</b>  •  {len(cstate.failures)}\n"
        f"👥 <b>Users</b>  •  {len(ustate.users)}\n"
        "📚 <b>Watchlist entries</b>  •  "
        f"{sum(len(user.watchlist) for user in ustate.users.values())}"
    )


@router.callback_query(F.data == "admin:stats", owner)
async def stats_callback(
    callback: CallbackQuery, catalog: CatalogRepository, users: UserRepository
) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    await callback.answer()
    await edit_screen(callback, await _stats_text(catalog, users), builder.as_markup())


async def _files_text(catalog: CatalogRepository) -> str:
    state = catalog.snapshot()
    unavailable = [item for item in state.files.values() if not item.available]
    media = Counter(item.media_type.value for item in state.files.values())
    pending_deletions = len(catalog.pending_removed_sources())
    health_icon = "✅" if not unavailable and not pending_deletions else "⚠️"
    return (
        "🎞 <b>CATALOG OPERATIONS</b>\n"
        "<blockquote>File inventory, availability, and safe removal.</blockquote>\n"
        f"{DIVIDER}\n"
        f"🎬 <b>Titles</b>  •  {len(state.contents)}\n"
        f"📦 <b>Total files</b>  •  {len(state.files)}\n"
        f"▶️ <b>Videos</b>  •  {media['video']}\n"
        f"📄 <b>Documents</b>  •  {media['document']}\n"
        f"⚠️ <b>Unavailable</b>  •  {len(unavailable)}\n"
        f"🧹 <b>Pending source deletions</b>  •  {pending_deletions}\n"
        f"{DIVIDER}\n"
        f"{health_icon} Catalog status reviewed"
    )


def _files_markup(catalog: CatalogRepository) -> InlineKeyboardBuilder:
    unavailable = sum(1 for item in catalog.snapshot().files.values() if not item.available)
    pending_deletions = len(catalog.pending_removed_sources())
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗑 Remove a title", callback_data="adr:0"))
    if unavailable:
        builder.row(
            InlineKeyboardButton(text=f"⚠️ Unavailable files — {unavailable}", callback_data="afu:0")
        )
    if pending_deletions:
        builder.row(
            InlineKeyboardButton(
                text=f"🔁 Retry source deletions — {pending_deletions}",
                callback_data="adp:retry",
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="✅ Confirm manually deleted posts",
                callback_data="adp:clear",
            )
        )
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    return builder


@router.callback_query(F.data == "admin:files", owner)
async def files_callback(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    await callback.answer()
    await edit_screen(callback, await _files_text(catalog), _files_markup(catalog).as_markup())


async def _delete_source_posts(
    bot: Bot,
    catalog: CatalogRepository,
    sources: list[RemovedSourceRecord],
) -> tuple[int, int]:
    grouped: dict[int, list[RemovedSourceRecord]] = defaultdict(list)
    for source in sources:
        grouped[source.source_chat_id].append(source)

    deleted: list[tuple[int, int]] = []
    failed = 0
    for chat_id, items in grouped.items():
        for start in range(0, len(items), 100):
            chunk = items[start : start + 100]
            try:
                result = await bot.delete_messages(
                    chat_id=chat_id,
                    message_ids=[item.source_message_id for item in chunk],
                )
                if not result:
                    raise RuntimeError("Telegram did not confirm batch deletion")
                deleted.extend((chat_id, item.source_message_id) for item in chunk)
                continue
            except Exception:
                LOGGER.warning(
                    "Batch source deletion failed in %s; retrying individually",
                    chat_id,
                    exc_info=True,
                )
            for item in chunk:
                try:
                    result = await bot.delete_message(chat_id, item.source_message_id)
                    if not result:
                        raise RuntimeError("Telegram did not confirm message deletion")
                    deleted.append((chat_id, item.source_message_id))
                except Exception:
                    failed += 1
                    LOGGER.warning(
                        "Could not delete removed source %s:%s",
                        chat_id,
                        item.source_message_id,
                        exc_info=True,
                    )
    if deleted:
        await catalog.mark_removed_sources_deleted(deleted)
    return len(deleted), failed


@router.callback_query(F.data.startswith("adr:"), owner)
async def removable_titles(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    page = int(callback.data.split(":", 1)[1])
    contents = sorted(
        catalog.snapshot().contents.values(),
        key=lambda item: (item.title.casefold(), item.year or 0),
    )
    visible, page, pages = page_slice(contents, page, 6)
    builder = InlineKeyboardBuilder()
    for content in visible:
        file_count = len(catalog.files_for_content(content.id, available_only=False))
        builder.row(
            InlineKeyboardButton(
                text=compact_label(f"🗑 {content.title} · {file_count} files", 58),
                callback_data=f"adrt:{content.id}:{page}",
                style="danger",
            )
        )
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="◀️ Previous", callback_data=f"adr:{page - 1}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"adr:{page + 1}"))
    if navigation:
        builder.row(*navigation)
    builder.row(InlineKeyboardButton(text="◀️ Catalog", callback_data="admin:files"))
    text = (
        "🗑 <b>PERMANENT CATALOG REMOVAL</b>\n"
        "<blockquote>Owner-only destructive workspace.</blockquote>\n"
        f"{DIVIDER}\n"
        f"🎬 Catalog titles: <b>{len(contents)}</b>\n"
        f"<code>Page {page + 1}/{pages}</code>\n\n"
        "⚠️ Choose a title only when it must be permanently removed from delivery."
    )
    if not contents:
        text += "\n\n🫙 <i>The delivery catalog is empty.</i>"
    await callback.answer()
    await edit_screen(callback, text, builder.as_markup())


@router.callback_query(F.data.startswith("adrt:"), owner)
async def removable_title_detail(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    _, content_id, page_text = callback.data.split(":", 2)
    content = catalog.get_content(content_id)
    if content is None:
        await callback.answer("Title not found.", show_alert=True)
        return
    files = catalog.files_for_content(content.id, available_only=False)
    category = catalog.get_category(content.category_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="⚠️ Continue to permanent removal",
            callback_data=f"adrc:{content.id}:{page_text}",
            style="danger",
        )
    )
    builder.row(InlineKeyboardButton(text="◀️ Titles", callback_data=f"adr:{page_text}"))
    text = (
        f"🗑 <b>{safe_html(content.title)}</b>\n"
        "<blockquote>Permanent catalog removal review</blockquote>\n"
        f"{DIVIDER}\n"
        f"🗂 <b>Category</b>  •  {safe_html(category.name if category else 'Unknown')}\n"
        f"📦 <b>Catalog files</b>  •  {len(files)}\n"
        f"📡 <b>Source posts</b>  •  up to {len(files)} deletion attempts\n"
        f"{DIVIDER}\n"
        "⚠️ This removes delivery access and attempts to delete associated source posts.\n\n"
        "ℹ️ Telegram normally refuses bot deletion after 48 hours; old posts must be "
        "deleted manually. Watchlist entries are not affected."
    )
    await callback.answer()
    await edit_screen(callback, text, builder.as_markup())


@router.callback_query(F.data.startswith("adrc:"), owner)
async def remove_title_confirm(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    _, content_id, page_text = callback.data.split(":", 2)
    content = catalog.get_content(content_id)
    if content is None:
        await callback.answer("Title not found.", show_alert=True)
        return
    files = catalog.files_for_content(content.id, available_only=False)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"🗑 Permanently delete {len(files)} files",
            callback_data=f"adrx:{content.id}",
            style="danger",
        )
    )
    builder.row(InlineKeyboardButton(text="Cancel", callback_data=f"adrt:{content.id}:{page_text}"))
    await callback.answer()
    await edit_screen(
        callback,
        "🚨 <b>FINAL DELETION CONFIRMATION</b>\n"
        f"<blockquote>{safe_html(content.title)}</blockquote>\n"
        f"{DIVIDER}\n"
        "This will permanently:\n"
        f"• remove <b>{len(files)} catalog file records</b>\n"
        "• block those sources from being re-indexed\n"
        "• attempt deletion of their Telegram source posts\n\n"
        "✅ Watchlist entries remain untouched.\n"
        "⚠️ Old Telegram posts may require manual deletion.\n\n"
        "<b>This action cannot be undone.</b>",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("adrx:"), owner)
async def remove_title_execute(
    callback: CallbackQuery,
    bot: Bot,
    catalog: CatalogRepository,
    config: Config,
) -> None:
    content_id = callback.data.split(":", 1)[1]
    try:
        result = await catalog.remove_content(content_id, callback.from_user.id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    deleted, failed = await _delete_source_posts(bot, catalog, list(result.sources))
    result_icon = "✅" if failed == 0 else "⚠️"
    summary = (
        f"{result_icon} <b>CATALOG TITLE REMOVED</b>\n"
        f"<blockquote>{safe_html(result.content.title)}</blockquote>\n"
        f"{DIVIDER}\n"
        f"🗑 Catalog files removed: {len(result.files)}\n"
        f"✅ Source posts deleted: {deleted}\n"
        f"🕓 Pending manual/retry deletion: {failed}\n"
        f"{DIVIDER}\n"
        "🔒 Delivery and re-indexing are now blocked.\n"
        "📚 Watchlist entries were not changed."
    )
    try:
        await bot.send_message(
            config.file_database_channel_id,
            summary,
            disable_notification=True,
        )
    except Exception:
        LOGGER.warning("Could not write title-removal audit card", exc_info=True)
    await callback.answer("Title removed from file delivery.")
    await edit_screen(callback, summary, _files_markup(catalog).as_markup())


@router.callback_query(F.data == "adp:retry", owner)
async def retry_source_deletions(
    callback: CallbackQuery,
    bot: Bot,
    catalog: CatalogRepository,
) -> None:
    pending = catalog.pending_removed_sources()
    if not pending:
        await callback.answer("No pending source deletions.")
        await edit_screen(callback, await _files_text(catalog), _files_markup(catalog).as_markup())
        return
    deleted, failed = await _delete_source_posts(bot, catalog, pending)
    await callback.answer(f"Deleted {deleted}; pending {failed}.")
    await edit_screen(callback, await _files_text(catalog), _files_markup(catalog).as_markup())


@router.callback_query(F.data == "adp:clear", owner)
async def confirm_manual_source_cleanup(
    callback: CallbackQuery,
    catalog: CatalogRepository,
) -> None:
    pending = catalog.pending_removed_sources()
    if not pending:
        await callback.answer("No pending source deletions.")
        return
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Confirm manually deleted", callback_data="adp:clearc"),
        InlineKeyboardButton(text="Cancel", callback_data="admin:files"),
    )
    lines = [
        "⚠️ <b>MANUAL SOURCE CLEANUP</b>",
        "<blockquote>Only confirm after every listed Telegram post is gone.</blockquote>",
        DIVIDER,
        f"🕓 Pending posts: <b>{len(pending)}</b>",
        "",
        "Delete these posts manually before confirming:",
        "",
    ]
    for item in pending[:20]:
        lines.append(
            f"• {safe_html(item.content_title)} — channel <code>{item.source_chat_id}</code>, "
            f"message <code>{item.source_message_id}</code>"
        )
    if len(pending) > 20:
        lines.append(f"…and {len(pending) - 20} more. Retry automatic deletion first.")
    lines.extend(
        [
            "",
            f"Mark all {len(pending)} pending source posts as manually deleted?",
        ]
    )
    await callback.answer()
    await edit_screen(callback, "\n".join(lines), builder.as_markup())


@router.callback_query(F.data == "adp:clearc", owner)
async def confirm_manual_source_cleanup_execute(
    callback: CallbackQuery,
    catalog: CatalogRepository,
) -> None:
    pending = catalog.pending_removed_sources()
    confirmed = await catalog.mark_removed_sources_deleted(
        (item.source_chat_id, item.source_message_id) for item in pending
    )
    if confirmed:
        await catalog.add_audit(
            "content.sources_manual_confirmation",
            f"Owner confirmed manual deletion of {confirmed} source posts",
            callback.from_user.id,
        )
    await callback.answer(f"Marked {confirmed} source posts as manually deleted.")
    await edit_screen(callback, await _files_text(catalog), _files_markup(catalog).as_markup())


@router.callback_query(F.data.startswith("afu:"), owner)
async def unavailable_files(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    page_text = callback.data.split(":", 1)[1]
    values = sorted(
        (item for item in catalog.snapshot().files.values() if not item.available),
        key=lambda item: item.updated_at,
        reverse=True,
    )
    visible, page, pages = page_slice(values, int(page_text), 6)
    builder = InlineKeyboardBuilder()
    for item in visible:
        builder.row(
            InlineKeyboardButton(
                text=compact_label(f"⚠️ {item.title} • {item.id}", 58),
                callback_data=f"af:{item.id}",
                style="primary",
            )
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Previous", callback_data=f"afu:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"afu:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="◀️ Files", callback_data="admin:files"))
    await callback.answer()
    await edit_screen(
        callback,
        "⚠️ <b>UNAVAILABLE FILES</b>\n"
        "<blockquote>Review files excluded from delivery.</blockquote>\n"
        f"{DIVIDER}\n"
        f"📦 Total unavailable: <b>{len(values)}</b>\n"
        f"<code>Page {page + 1}/{pages}</code>"
        + ("\n\n🫙 <i>No unavailable files.</i>" if not values else ""),
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("af:"), owner)
async def unavailable_file_detail(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    record = catalog.get_file(callback.data.split(":", 1)[1])
    if record is None:
        await callback.answer("File not found.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    if not record.available:
        builder.row(
            InlineKeyboardButton(text="✅ Mark available", callback_data=f"afon:{record.id}")
        )
    builder.row(InlineKeyboardButton(text="◀️ Unavailable files", callback_data="afu:0"))
    status_icon = "✅" if record.available else "⚠️"
    text = (
        f"🎞 <b>{safe_html(record.title)}</b>\n"
        "<blockquote>Catalog file diagnostics</blockquote>\n"
        f"{DIVIDER}\n"
        f"🆔 <b>File ID</b>  •  <code>{record.id}</code>\n"
        f"{status_icon} <b>Status</b>  •  "
        f"{'Available' if record.available else 'Unavailable'}\n"
        f"📡 <b>Source message</b>  •  <code>{record.source_message_id}</code>\n"
        f"💎 <b>Quality</b>  •  {safe_html(record.quality or 'Unknown')}"
    )
    await callback.answer()
    await edit_screen(callback, text, builder.as_markup())


@router.callback_query(F.data.startswith("afon:"), owner)
async def mark_file_available(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    file_id = callback.data.split(":", 1)[1]
    try:
        await catalog.mark_file_available(file_id, True)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("File marked available. Test it from the user catalog.")
    await edit_screen(callback, await _files_text(catalog), _files_markup(catalog).as_markup())


async def _failures_text(catalog: CatalogRepository) -> str:
    failures = sorted(
        catalog.snapshot().failures.values(), key=lambda item: item.updated_at, reverse=True
    )[:10]
    if not failures:
        return (
            "✅ <b>INDEXING HEALTH</b>\n"
            "<blockquote>Automatic channel indexing is clear.</blockquote>\n"
            f"{DIVIDER}\n"
            "🎉 No unresolved indexing failures."
        )
    lines = [
        "❌ <b>RECENT INDEX FAILURES</b>",
        "<blockquote>Review the newest unresolved channel messages.</blockquote>",
        DIVIDER,
        f"⚠️ Showing <b>{len(failures)}</b> recent failures",
        "",
    ]
    for item in failures:
        lines.append(
            f"• Message <code>{item.source_message_id}</code>  •  {safe_html(item.reason)}"
        )
    return "\n".join(lines)


@router.callback_query(F.data == "admin:failures", owner)
async def failures_callback(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    await callback.answer()
    await edit_screen(callback, await _failures_text(catalog), builder.as_markup())


@router.callback_query(F.data == "admin:access", owner)
async def access_callback(callback: CallbackQuery, users: UserRepository) -> None:
    text, markup = access_mode_panel(users.snapshot().access_mode)
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("access:"), owner)
async def access_confirm(callback: CallbackQuery) -> None:
    mode = AccessMode(callback.data.split(":", 1)[1])
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Apply access mode",
            callback_data=f"accessc:{mode.value}",
            style="success",
        ),
        InlineKeyboardButton(text="✖️ Cancel", callback_data="admin:access"),
    )
    descriptions = {
        AccessMode.PUBLIC: "All registered users can access the bot.",
        AccessMode.APPROVAL: "New users wait for owner approval.",
        AccessMode.ALLOWLIST: "Only users activated by the owner can enter.",
    }
    await callback.answer()
    await edit_screen(
        callback,
        "🔐 <b>CONFIRM ACCESS MODE</b>\n"
        f"<blockquote>{safe_html(mode.value.title())}</blockquote>\n"
        f"{DIVIDER}\n"
        f"👥 {descriptions[mode]}\n\n"
        "ℹ️ Existing bans and suspensions remain unchanged.",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("accessc:"), owner)
async def access_set(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
) -> None:
    mode = AccessMode(callback.data.split(":", 1)[1])
    await users.set_access_mode(mode)
    await catalog.add_audit(
        "access.mode", f"Changed access mode to {mode.value}", callback.from_user.id
    )
    text, markup = access_mode_panel(mode)
    await callback.answer("Access mode updated.")
    await edit_screen(callback, text, markup)


@router.callback_query(F.data == "admin:users", owner)
async def users_callback(
    callback: CallbackQuery,
    users: UserRepository,
    state: FSMContext,
) -> None:
    await state.clear()
    text, markup = users_panel(users.list_users())
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("aul:"), owner)
async def user_status_list(callback: CallbackQuery, users: UserRepository) -> None:
    _, status_text, page_text = callback.data.split(":", 2)
    status = UserStatus(status_text)
    values = users.list_users([status])
    visible, page, pages = page_slice(values, int(page_text), 6)
    builder = InlineKeyboardBuilder()
    for user in visible:
        label = f"{user.first_name} • {user.telegram_user_id}"
        builder.row(
            InlineKeyboardButton(
                text=compact_label(f"👤 {label}", 58),
                callback_data=f"au:{user.telegram_user_id}",
                style="primary",
            )
        )
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="◀️ Previous", callback_data=f"aul:{status.value}:{page - 1}")
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(text="Next ▶️", callback_data=f"aul:{status.value}:{page + 1}")
        )
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="◀️ Users", callback_data="admin:users"))
    await callback.answer()
    status_icons = {
        UserStatus.ACTIVE: "✅",
        UserStatus.PENDING: "🕓",
        UserStatus.SUSPENDED: "⏸",
        UserStatus.BANNED: "⛔",
    }
    text = (
        f"{status_icons[status]} <b>{status.value.upper()} USERS</b>\n"
        f"<blockquote>{len(values)} account{'s' if len(values) != 1 else ''}</blockquote>\n"
        f"{DIVIDER}\n"
        f"<code>Page {page + 1}/{pages}</code>"
    )
    if not values:
        text += "\n\n🫙 <i>No users currently have this status.</i>"
    await edit_screen(callback, text, builder.as_markup())


@router.callback_query(F.data.startswith("au:"), owner)
async def user_detail_callback(
    callback: CallbackQuery,
    users: UserRepository,
    state: FSMContext,
) -> None:
    await state.clear()
    user = users.get_user(int(callback.data.split(":", 1)[1]))
    if user is None:
        await callback.answer("User not found.", show_alert=True)
        return
    text, markup = user_detail(user)
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("aucn:"), owner)
async def user_community_name_start(
    callback: CallbackQuery,
    users: UserRepository,
    state: FSMContext,
) -> None:
    user_id = int((callback.data or "").split(":", 1)[1])
    user = users.get_user(user_id)
    if user is None:
        await callback.answer("User not found.", show_alert=True)
        return
    await state.set_state(AdminState.user_community_name)
    await state.update_data(target_user_id=user_id)
    builder = InlineKeyboardBuilder()
    if user.watchlist_display_name:
        builder.row(
            InlineKeyboardButton(
                text="↩️ Reset to Telegram name",
                callback_data=f"aucnr:{user_id}",
            )
        )
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data=f"au:{user_id}"))
    await callback.answer()
    await edit_screen(
        callback,
        "✏️ <b>EDIT COMMUNITY NAME</b>\n"
        f"<blockquote>{safe_html(user.first_name)} • owner moderation</blockquote>\n"
        f"{DIVIDER}\n"
        f"Current public name: <b>{safe_html(user.watchlist_display_name or user.first_name)}</b>\n\n"
        "Send a replacement name up to 40 characters. The user's Telegram profile is unchanged.",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("aucnr:"), owner)
async def user_community_name_reset(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    state: FSMContext,
) -> None:
    user_id = int((callback.data or "").split(":", 1)[1])
    try:
        user = await users.set_watchlist_display_name(user_id, None)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await catalog.add_audit(
        "user.community_name",
        f"Reset community name for user {user_id}",
        callback.from_user.id,
    )
    await state.clear()
    text, markup = user_detail(user)
    await callback.answer("Community name reset.")
    await edit_screen(callback, text, markup)


@router.message(AdminState.user_community_name, owner, F.text, ~F.text.startswith("/"))
async def user_community_name_input(
    message: Message,
    state: FSMContext,
    users: UserRepository,
    catalog: CatalogRepository,
    panels: PanelManager | None = None,
) -> None:
    data = await state.get_data()
    user_id = data.get("target_user_id")
    if not isinstance(user_id, int):
        await state.clear()
        await message.answer("This community-name session expired.")
        return
    try:
        user = await users.set_watchlist_display_name(user_id, message.text)
    except ValueError as exc:
        await message.answer(f"❌ {safe_html(exc)}")
        return
    await catalog.add_audit(
        "user.community_name",
        f"Changed community name for user {user_id}",
        message.from_user.id if message.from_user else None,
    )
    await state.clear()
    text, markup = user_detail(user)
    await _workspace_or_answer(message, text, panels, markup)


@router.callback_query(F.data == "auf:start", owner)
async def user_find_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.user_find)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="admin:users"))
    await callback.answer()
    await edit_screen(
        callback,
        "🔎 <b>FIND A USER</b>\n"
        "<blockquote>Search by an exact Telegram identity.</blockquote>\n"
        f"{DIVIDER}\n"
        "Send a numeric user ID or exact <code>@username</code>.\n\n"
        "Use the Cancel button to stop.",
        builder.as_markup(),
    )


@router.message(AdminState.user_find, owner, F.text, ~F.text.startswith("/"))
async def user_find_input(
    message: Message,
    state: FSMContext,
    users: UserRepository,
    panels: PanelManager | None = None,
) -> None:
    query = message.text.strip().lstrip("@").casefold()
    user = (
        users.get_user(int(query))
        if query.isdigit()
        else next(
            (item for item in users.list_users() if (item.username or "").casefold() == query),
            None,
        )
    )
    if user is None:
        await message.answer(
            "🔍 <b>USER NOT FOUND</b>\n"
            "Check the numeric ID or exact username, then try again. Use the Cancel button to stop."
        )
        return
    await state.clear()
    text, markup = user_detail(user)
    await _workspace_or_answer(message, text, panels, markup)


@router.callback_query(F.data.startswith("aus:"), owner)
async def user_status_set(
    callback: CallbackQuery,
    users: UserRepository,
    catalog: CatalogRepository,
    config: Config,
) -> None:
    _, user_id_text, status_text = callback.data.split(":", 2)
    user_id = int(user_id_text)
    if config.is_owner(user_id):
        await callback.answer(
            "The environment-configured owner cannot be restricted.", show_alert=True
        )
        return
    status = UserStatus(status_text)
    try:
        user = await users.set_user_status(user_id, status)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await catalog.add_audit(
        "user.status", f"Set user {user_id} to {status.value}", callback.from_user.id
    )
    text, markup = user_detail(user)
    await callback.answer("User status updated.")
    await edit_screen(callback, text, markup)
    if status == UserStatus.ACTIVE:
        try:
            await callback.bot.send_message(
                user_id, "✅ Your bot access is now active. Send a title to search."
            )
        except TelegramAPIError:
            LOGGER.info("Could not notify user %s about activation", user_id)


async def _db_status_text(catalog: CatalogRepository, users: UserRepository) -> str:
    catalog_bytes = len(catalog.store.export_gzip())
    users_bytes = len(users.store.export_gzip())
    return (
        "💾 <b>DATABASE & RECOVERY</b>\n"
        "<blockquote>Pinned snapshots in private Telegram channels.</blockquote>\n"
        f"{DIVIDER}\n"
        f"🗂 <b>Catalog revision</b>  •  {catalog.snapshot().revision}\n"
        f"📦 <b>Catalog snapshot</b>  •  {catalog_bytes / 1024:.1f} KiB\n"
        f"👥 <b>Users revision</b>  •  {users.snapshot().revision}\n"
        f"📦 <b>Users snapshot</b>  •  {users_bytes / 1024:.1f} KiB\n"
        f"{DIVIDER}\n"
        "✅ Both states can restore from their private database channels."
    )


@router.callback_query(F.data == "admin:database", owner)
async def database_callback(
    callback: CallbackQuery, catalog: CatalogRepository, users: UserRepository
) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📤 Export protected backups", callback_data="adb:backup", style="success"
        )
    )
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    await callback.answer()
    await edit_screen(callback, await _db_status_text(catalog, users), builder.as_markup())


async def _send_backup(
    target_id: int,
    bot: Bot,
    catalog: CatalogRepository,
    users: UserRepository,
) -> None:
    await bot.send_document(
        target_id,
        BufferedInputFile(catalog.store.export_gzip(), filename="catalog-backup.json.gz"),
        caption=f"Catalog backup • revision {catalog.snapshot().revision}",
        protect_content=True,
    )
    await bot.send_document(
        target_id,
        BufferedInputFile(users.store.export_gzip(), filename="users-backup.json.gz"),
        caption=f"Users backup • revision {users.snapshot().revision}",
        protect_content=True,
    )


@router.callback_query(F.data == "adb:backup", owner)
async def backup_callback(
    callback: CallbackQuery,
    bot: Bot,
    catalog: CatalogRepository,
    users: UserRepository,
) -> None:
    await callback.answer("Preparing backups…")
    await _send_backup(callback.from_user.id, bot, catalog, users)


async def _audit_text(catalog: CatalogRepository) -> str:
    events = catalog.recent_audit(15)
    if not events:
        return (
            "📜 <b>AUDIT TRAIL</b>\n"
            "<blockquote>Recent owner and system changes.</blockquote>\n"
            f"{DIVIDER}\n"
            "🫙 <i>No events recorded.</i>"
        )
    lines = [
        "📜 <b>RECENT AUDIT EVENTS</b>",
        f"<blockquote>Latest {len(events)} recorded changes.</blockquote>",
        DIVIDER,
        "",
    ]
    for event in events:
        actor = f" by {event.actor_id}" if event.actor_id else ""
        lines.append(f"• {safe_html(event.action)}{actor}: {safe_html(event.details)}")
    return "\n".join(lines)


@router.callback_query(F.data == "admin:audit", owner)
async def audit_callback(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    await callback.answer()
    await edit_screen(callback, await _audit_text(catalog), builder.as_markup())


def _settings_text(config: Config, users: UserRepository) -> str:
    protection_icon = "✅" if config.protect_delivered_content else "⚠️"
    return (
        "⚙️ <b>BOT SETTINGS</b>\n"
        "<blockquote>Current runtime configuration.</blockquote>\n"
        f"{DIVIDER}\n"
        f"🔐 <b>Access mode</b>  •  {safe_html(users.snapshot().access_mode.value.title())}\n"
        f"{protection_icon} <b>Protected delivery</b>  •  "
        f"{'Enabled' if config.protect_delivered_content else 'Disabled'}\n"
        "🔎 <b>Search page size</b>  •  4 results\n"
        "📱 <b>Interface</b>  •  dashboard-first native controls"
    )


@router.callback_query(F.data == "admin:settings", owner)
async def bot_settings_callback(
    callback: CallbackQuery, config: Config, users: UserRepository
) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    await callback.answer()
    await edit_screen(callback, _settings_text(config, users), builder.as_markup())


@router.callback_query(F.data.startswith("admin:"), not_owner)
@router.callback_query(F.data.startswith(("ab", "ac", "ad", "au", "access")), not_owner)
async def unauthorized_admin_callback(callback: CallbackQuery) -> None:
    await callback.answer("You are not authorized.", show_alert=True)

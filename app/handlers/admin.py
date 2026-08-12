from __future__ import annotations

import logging
from collections import Counter, defaultdict

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    Message,
    MessageOriginChannel,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Config
from ..filters import NotOwnerFilter, OwnerFilter
from ..models import AccessMode, CategoryMode, RemovedSourceRecord, UserStatus
from ..repositories import CatalogRepository, UserRepository
from ..ui import (
    access_mode_panel,
    admin_categories,
    admin_category_detail,
    admin_dashboard,
    page_slice,
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
) -> None:
    try:
        channel_id = int(raw_channel_id.strip())
        title = await _validate_private_channel(bot, channel_id, config)
    except (ValueError, TypeError) as exc:
        await message.answer(f"❌ {safe_html(exc)}\n\nSend a valid private channel ID or /cancel.")
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
    await message.answer(
        "<b>Confirm new category</b>\n\n"
        f"Name: {safe_html(name)}\n"
        f"Channel: {safe_html(title)}\n"
        f"Channel ID: <code>{channel_id}</code>\n"
        f"Mode: {mode.value}",
        reply_markup=builder.as_markup(),
    )


@router.message(Command("admin"), owner)
async def admin_command(message: Message) -> None:
    text, markup = admin_dashboard()
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin:home", owner)
async def admin_home(callback: CallbackQuery) -> None:
    await callback.answer()
    text, markup = admin_dashboard()
    await edit_screen(callback, text, markup)


@router.message(Command("index"), owner)
async def manual_index_command(
    message: Message,
    bot: Bot,
    config: Config,
    catalog: CatalogRepository,
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
    )
    if indexed:
        await message.answer("✅ The original channel post has been indexed.")
    else:
        await message.answer("The file could not be indexed. Check /failures for details.")


@router.message(Command("categories"), owner)
async def categories_command(message: Message, catalog: CatalogRepository) -> None:
    text, markup = admin_categories(catalog.list_categories(include_disabled=True))
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin:categories", owner)
async def categories_callback(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    await callback.answer()
    text, markup = admin_categories(catalog.list_categories(include_disabled=True))
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("ac:"), owner)
async def category_detail_callback(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    category = catalog.get_category(callback.data.split(":", 1)[1])
    if category is None:
        await callback.answer("Category not found.", show_alert=True)
        return
    await callback.answer()
    text, markup = admin_category_detail(category)
    await edit_screen(callback, text, markup)


@router.message(Command("category_add"), owner)
async def category_add_command(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    bot: Bot,
    config: Config,
) -> None:
    await state.clear()
    if command.args and "|" in command.args:
        name, channel_id = (value.strip() for value in command.args.split("|", 1))
        if not name:
            await message.answer("Category name cannot be empty.")
            return
        await _show_add_confirmation(message, state, bot, config, name, channel_id)
        return
    await state.set_state(AdminState.category_name)
    await message.answer("Send the new category name, or /cancel.")


@router.callback_query(F.data == "aca:start", owner)
async def category_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AdminState.category_name)
    await callback.answer()
    await callback.message.answer("Send the new category name, or /cancel.")


@router.message(AdminState.category_name, owner, F.text)
async def category_name_input(message: Message, state: FSMContext) -> None:
    name = " ".join(message.text.split()).strip()
    if not name or len(name) > 60:
        await message.answer("Use a category name between 1 and 60 characters.")
        return
    await state.update_data(category_name=name)
    await state.set_state(AdminState.category_channel)
    await message.answer(
        "Now send the private storage channel ID.\n\n"
        "The bot must already be an administrator in that channel."
    )


@router.message(AdminState.category_channel, owner, F.text)
async def category_channel_input(
    message: Message, state: FSMContext, bot: Bot, config: Config
) -> None:
    data = await state.get_data()
    await _show_add_confirmation(message, state, bot, config, data["category_name"], message.text)


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
    await callback.answer()
    await callback.message.answer("Send the new category name, or /cancel.")


@router.message(AdminState.category_rename, owner, F.text)
async def category_rename_input(
    message: Message,
    state: FSMContext,
    catalog: CatalogRepository,
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
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("acc:"), owner)
async def category_channel_start(callback: CallbackQuery, state: FSMContext) -> None:
    category_id = callback.data.split(":", 1)[1]
    await state.set_state(AdminState.category_change_channel)
    await state.update_data(category_id=category_id)
    await callback.answer()
    await callback.message.answer(
        "Send the new private channel ID. The old channel will remain as a legacy source."
    )


@router.message(AdminState.category_change_channel, owner, F.text)
async def category_channel_change_input(
    message: Message,
    state: FSMContext,
    catalog: CatalogRepository,
    bot: Bot,
    config: Config,
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
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("acm:"), owner)
async def category_mode_start(callback: CallbackQuery) -> None:
    category_id = callback.data.split(":", 1)[1]
    builder = InlineKeyboardBuilder()
    for mode in CategoryMode:
        builder.row(
            InlineKeyboardButton(
                text=mode.value.title(), callback_data=f"acms:{category_id}:{mode.value}"
            )
        )
    builder.row(InlineKeyboardButton(text="Cancel", callback_data=f"ac:{category_id}"))
    await callback.answer()
    await edit_screen(callback, "Choose the category behaviour:", builder.as_markup())


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
        InlineKeyboardButton(text=f"Confirm {action}", callback_data=f"actc:{category_id}"),
        InlineKeyboardButton(text="Cancel", callback_data=f"ac:{category_id}"),
    )
    await callback.answer()
    await edit_screen(
        callback,
        f"Confirm that you want to <b>{action}</b> {safe_html(category.name)}.",
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
        "📊 <b>Statistics</b>\n\n"
        f"Categories: {len(cstate.categories)}\n"
        f"Titles: {len(cstate.contents)}\n"
        f"Files: {len(cstate.files)}\n"
        f"Available files: {available}\n"
        f"Index failures: {len(cstate.failures)}\n"
        f"Users: {len(ustate.users)}\n"
        f"Watchlist entries: {sum(len(user.watchlist) for user in ustate.users.values())}"
    )


@router.message(Command("stats"), owner)
async def stats_command(
    message: Message, catalog: CatalogRepository, users: UserRepository
) -> None:
    await message.answer(await _stats_text(catalog, users))


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
    return (
        "🎞 <b>Catalog files</b>\n\n"
        f"Titles: {len(state.contents)}\n"
        f"Files: {len(state.files)}\n"
        f"Videos: {media['video']}\n"
        f"Documents: {media['document']}\n"
        f"Unavailable: {len(unavailable)}\n"
        f"Pending source deletions: {pending_deletions}"
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


@router.message(Command("files"), owner)
async def files_command(message: Message, catalog: CatalogRepository) -> None:
    await message.answer(
        await _files_text(catalog), reply_markup=_files_markup(catalog).as_markup()
    )


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
                text=compact_label(f"{content.title} — {file_count} files", 58),
                callback_data=f"adrt:{content.id}:{page}",
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
    text = f"🗑 <b>Remove catalog title</b>\n\nPage {page + 1} of {pages}"
    if not contents:
        text += "\n\nThe catalog is empty."
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
            text="Continue to permanent removal",
            callback_data=f"adrc:{content.id}:{page_text}",
        )
    )
    builder.row(InlineKeyboardButton(text="◀️ Titles", callback_data=f"adr:{page_text}"))
    text = (
        f"🗑 <b>{safe_html(content.title)}</b>\n\n"
        f"Category: {safe_html(category.name if category else 'Unknown')}\n"
        f"Files and source posts: {len(files)}\n\n"
        "This owner-only action removes the title from file delivery and attempts to "
        "delete all associated source-channel posts. Telegram normally refuses bot deletion "
        "after 48 hours; those posts must be deleted manually."
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
            text=f"Permanently delete {len(files)} files",
            callback_data=f"adrx:{content.id}",
        )
    )
    builder.row(InlineKeyboardButton(text="Cancel", callback_data=f"adrt:{content.id}:{page_text}"))
    await callback.answer()
    await edit_screen(
        callback,
        "⚠️ <b>Permanent deletion</b>\n\n"
        f"Delete <b>{safe_html(content.title)}</b>, all catalog file records, and attempt "
        "deletion of all Telegram source posts? This cannot be undone. Posts older than "
        "Telegram’s bot-deletion window require manual deletion.",
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
    summary = (
        "🗑 <b>Catalog title removed</b>\n\n"
        f"Title: {safe_html(result.content.title)}\n"
        f"Catalog files removed: {len(result.files)}\n"
        f"Telegram source posts deleted: {deleted}\n"
        f"Pending source deletions: {failed}"
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
        "⚠️ <b>Pending Telegram source posts</b>",
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
                text=compact_label(f"{item.title} • {item.id}", 58),
                callback_data=f"af:{item.id}",
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
        f"⚠️ <b>Unavailable files</b>\n\nPage {page + 1} of {pages}",
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
    text = (
        f"🎞 <b>{safe_html(record.title)}</b>\n\n"
        f"File ID: <code>{record.id}</code>\n"
        f"Status: {'Available' if record.available else 'Unavailable'}\n"
        f"Source message: <code>{record.source_message_id}</code>\n"
        f"Quality: {safe_html(record.quality or 'Unknown')}"
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
        return "✅ <b>Index failures</b>\n\nNo unresolved indexing failures."
    lines = ["❌ <b>Recent index failures</b>", ""]
    for item in failures:
        lines.append(f"• Message <code>{item.source_message_id}</code>: {safe_html(item.reason)}")
    return "\n".join(lines)


@router.message(Command("failures"), owner)
async def failures_command(message: Message, catalog: CatalogRepository) -> None:
    await message.answer(await _failures_text(catalog))


@router.callback_query(F.data == "admin:failures", owner)
async def failures_callback(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    await callback.answer()
    await edit_screen(callback, await _failures_text(catalog), builder.as_markup())


@router.message(Command("access_mode"), owner)
async def access_command(message: Message, users: UserRepository) -> None:
    text, markup = access_mode_panel(users.snapshot().access_mode)
    await message.answer(text, reply_markup=markup)


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
        InlineKeyboardButton(text="Confirm", callback_data=f"accessc:{mode.value}"),
        InlineKeyboardButton(text="Cancel", callback_data="admin:access"),
    )
    await callback.answer()
    await edit_screen(
        callback,
        f"Change access mode to <b>{mode.value}</b>? Existing bans and suspensions remain.",
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


@router.message(Command("users"), owner)
async def users_command(message: Message, users: UserRepository) -> None:
    text, markup = users_panel(users.list_users())
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "admin:users", owner)
async def users_callback(callback: CallbackQuery, users: UserRepository) -> None:
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
                text=compact_label(label, 58), callback_data=f"au:{user.telegram_user_id}"
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
    await edit_screen(
        callback,
        f"👥 <b>{status.value.title()} users</b>\n\nPage {page + 1} of {pages}",
        builder.as_markup(),
    )


@router.callback_query(F.data.startswith("au:"), owner)
async def user_detail_callback(callback: CallbackQuery, users: UserRepository) -> None:
    user = users.get_user(int(callback.data.split(":", 1)[1]))
    if user is None:
        await callback.answer("User not found.", show_alert=True)
        return
    text, markup = user_detail(user)
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data == "auf:start", owner)
async def user_find_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.user_find)
    await callback.answer()
    await callback.message.answer("Send a Telegram user ID or exact @username, or /cancel.")


@router.message(AdminState.user_find, owner, F.text)
async def user_find_input(
    message: Message,
    state: FSMContext,
    users: UserRepository,
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
        await message.answer("User not found. Try again or /cancel.")
        return
    await state.clear()
    text, markup = user_detail(user)
    await message.answer(text, reply_markup=markup)


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
        "💾 <b>Database status</b>\n\n"
        f"Catalog revision: {catalog.snapshot().revision}\n"
        f"Catalog snapshot: {catalog_bytes / 1024:.1f} KiB\n"
        f"Users revision: {users.snapshot().revision}\n"
        f"Users snapshot: {users_bytes / 1024:.1f} KiB\n\n"
        "Both states are restored from their private Telegram database channels."
    )


@router.message(Command("db_status"), owner)
async def db_status_command(
    message: Message, catalog: CatalogRepository, users: UserRepository
) -> None:
    await message.answer(await _db_status_text(catalog, users))


@router.callback_query(F.data == "admin:database", owner)
async def database_callback(
    callback: CallbackQuery, catalog: CatalogRepository, users: UserRepository
) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📤 Export backup", callback_data="adb:backup"))
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


@router.message(Command("backup"), owner)
async def backup_command(
    message: Message,
    bot: Bot,
    catalog: CatalogRepository,
    users: UserRepository,
) -> None:
    await message.answer("Preparing backups…")
    await _send_backup(message.from_user.id, bot, catalog, users)


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
        return "📜 <b>Audit log</b>\n\nNo events recorded."
    lines = ["📜 <b>Recent audit events</b>", ""]
    for event in events:
        actor = f" by {event.actor_id}" if event.actor_id else ""
        lines.append(f"• {safe_html(event.action)}{actor}: {safe_html(event.details)}")
    return "\n".join(lines)


@router.message(Command("audit"), owner)
async def audit_command(message: Message, catalog: CatalogRepository) -> None:
    await message.answer(await _audit_text(catalog))


@router.callback_query(F.data == "admin:audit", owner)
async def audit_callback(callback: CallbackQuery, catalog: CatalogRepository) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    await callback.answer()
    await edit_screen(callback, await _audit_text(catalog), builder.as_markup())


def _settings_text(config: Config, users: UserRepository) -> str:
    return (
        "⚙️ <b>Bot settings</b>\n\n"
        f"Access mode: {users.snapshot().access_mode.value}\n"
        f"Protected delivery: {'Enabled' if config.protect_delivered_content else 'Disabled'}\n"
        "Search results per page: 4\n"
        "User interface: native commands + inline dashboard"
    )


@router.message(Command("bot_settings"), owner)
async def bot_settings_command(message: Message, config: Config, users: UserRepository) -> None:
    await message.answer(_settings_text(config, users))


@router.callback_query(F.data == "admin:settings", owner)
async def bot_settings_callback(
    callback: CallbackQuery, config: Config, users: UserRepository
) -> None:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    await callback.answer()
    await edit_screen(callback, _settings_text(config, users), builder.as_markup())


@router.callback_query(F.data.startswith("admin:"), not_owner)
@router.callback_query(F.data.startswith(("ac", "ad", "au", "access")), not_owner)
async def unauthorized_admin_callback(callback: CallbackQuery) -> None:
    await callback.answer("You are not authorized.", show_alert=True)

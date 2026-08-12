from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..commands import register_owner_commands
from ..config import Config
from ..guards import access_denied_text, can_use_bot, ensure_registered
from ..repositories import CatalogRepository, UserRepository
from ..services import CatalogQueryService, SearchSessionStore
from ..ui import browse_categories, main_dashboard, search_results

router = Router(name="common")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


async def edit_screen(callback: CallbackQuery, text: str, reply_markup) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=reply_markup)
        except Exception as exc:
            if "message is not modified" not in str(exc).casefold():
                raise


async def show_home_message(message: Message, config: Config) -> None:
    text, markup = main_dashboard(
        config.is_owner(message.from_user.id if message.from_user else None)
    )
    await message.answer(text, reply_markup=markup)


async def show_home_callback(callback: CallbackQuery, config: Config) -> None:
    text, markup = main_dashboard(config.is_owner(callback.from_user.id))
    await edit_screen(callback, text, markup)


@router.message(CommandStart())
async def start_command(
    message: Message,
    command: CommandObject,
    bot: Bot,
    config: Config,
    users: UserRepository,
    catalog: CatalogRepository,
    query: CatalogQueryService,
) -> None:
    if message.chat.type != "private" or message.from_user is None:
        return
    profile, _ = await ensure_registered(message.from_user, users, config, bot)
    if config.is_owner(message.from_user.id):
        await register_owner_commands(bot, message.from_user.id)
    if not can_use_bot(profile, config):
        await message.answer(access_denied_text(profile))
        return
    if command.args and command.args.startswith("c_"):
        content_id = command.args
        content = catalog.get_content(content_id)
        if content:
            category = catalog.get_category(content.category_id)
            if category:
                from ..ui import content_screen

                text, markup = content_screen(
                    content=content,
                    category=category,
                    query=query,
                )
                await message.answer(text, reply_markup=markup)
                return
    await show_home_message(message, config)


@router.message(Command("menu"))
async def menu_command(message: Message, bot: Bot, config: Config, users: UserRepository) -> None:
    if message.chat.type != "private" or message.from_user is None:
        return
    profile, _ = await ensure_registered(message.from_user, users, config, bot)
    if not can_use_bot(profile, config):
        await message.answer(access_denied_text(profile))
        return
    await show_home_message(message, config)


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "❓ <b>How to use the bot</b>\n\n"
        "1. Type a movie or series title.\n"
        "2. Select the best matching result.\n"
        "3. For a series, choose its season and episode.\n"
        "4. Choose a language/quality only when multiple versions exist.\n\n"
        "Use /watchlist to add manual or catalog titles and browse shared community lists."
    )


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext, config: Config) -> None:
    await state.clear()
    await message.answer("Cancelled.")
    await show_home_message(message, config)


@router.callback_query(F.data == "menu:home")
async def home_callback(callback: CallbackQuery, config: Config) -> None:
    await callback.answer()
    await show_home_callback(callback, config)


@router.callback_query(F.data == "menu:search")
async def search_help_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await edit_screen(
        callback,
        "🔎 <b>Search</b>\n\nType the name of a movie or series now. "
        "You can optionally include a year, for example <code>Dune 2021</code>.",
        None,
    )


@router.callback_query(F.data == "menu:help")
async def help_callback(callback: CallbackQuery, config: Config) -> None:
    await callback.answer()
    text = (
        "❓ <b>How to use the bot</b>\n\n"
        "Type a title, choose a result, then choose a season/episode or movie version. "
        "Delivered files are protected from normal forwarding and saving."
    )
    _, markup = main_dashboard(config.is_owner(callback.from_user.id))
    await edit_screen(callback, text, markup)


@router.callback_query(F.data == "menu:browse")
async def browse_callback(
    callback: CallbackQuery, catalog: CatalogRepository, users: UserRepository, config: Config
) -> None:
    profile, _ = await ensure_registered(callback.from_user, users, config)
    if not can_use_bot(profile, config):
        await callback.answer(access_denied_text(profile), show_alert=True)
        return
    await callback.answer()
    text, markup = browse_categories(catalog.list_categories())
    await edit_screen(callback, text, markup)


@router.callback_query(F.data.startswith("browse:"))
async def browse_category_callback(
    callback: CallbackQuery,
    query: CatalogQueryService,
    sessions: SearchSessionStore,
    catalog: CatalogRepository,
    users: UserRepository,
    config: Config,
) -> None:
    profile, _ = await ensure_registered(callback.from_user, users, config)
    if not can_use_bot(profile, config):
        await callback.answer(access_denied_text(profile), show_alert=True)
        return
    category_id = callback.data.split(":", 1)[1]
    category = catalog.get_category(category_id)
    if category is None or not category.enabled:
        await callback.answer("Category is unavailable.", show_alert=True)
        return
    contents = query.browse_category(category_id)
    if not contents:
        await callback.answer("This category is empty.", show_alert=True)
        return
    session = sessions.create(callback.from_user.id, category.name, [item.id for item in contents])
    text, markup = search_results(session, contents, 0)
    await callback.answer()
    await edit_screen(callback, text, markup)


@router.callback_query(F.data == "menu:recent")
async def recent_callback(
    callback: CallbackQuery,
    query: CatalogQueryService,
    sessions: SearchSessionStore,
    users: UserRepository,
    config: Config,
) -> None:
    profile, _ = await ensure_registered(callback.from_user, users, config)
    if not can_use_bot(profile, config):
        await callback.answer(access_denied_text(profile), show_alert=True)
        return
    contents = query.recently_added()
    if not contents:
        await callback.answer("No titles have been indexed yet.", show_alert=True)
        return
    session = sessions.create(
        callback.from_user.id, "Recently added", [item.id for item in contents]
    )
    text, markup = search_results(session, contents, 0)
    await callback.answer()
    await edit_screen(callback, text, markup)

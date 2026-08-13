from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ..commands import register_owner_commands
from ..config import Config
from ..guards import access_denied_text, can_use_bot, ensure_registered
from ..presentation import ActionButton
from ..repositories import CatalogRepository, UserRepository
from ..services import CatalogQueryService, SearchSessionStore
from ..ui import browse_categories, main_dashboard, search_results
from ..utils import safe_html

router = Router(name="common")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
DIVIDER = "━━━━━━━━━━━━━━━━━━"


def home_button_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ActionButton(text="🏠 Back to main menu", callback_data="menu:home")]]
    )


async def edit_screen(callback: CallbackQuery, text: str, reply_markup) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=reply_markup)
        except Exception as exc:
            if "message is not modified" not in str(exc).casefold():
                raise


async def show_home_message(message: Message, config: Config) -> None:
    text, markup = main_dashboard(
        config.is_owner(message.from_user.id if message.from_user else None),
        message.from_user.first_name if message.from_user else None,
    )
    await message.answer(text, reply_markup=markup)


async def show_home_callback(callback: CallbackQuery, config: Config) -> None:
    text, markup = main_dashboard(
        config.is_owner(callback.from_user.id), callback.from_user.first_name
    )
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
        "❓ <b>HELP & QUICK GUIDE</b>\n"
        "<blockquote>Everything you need to find and save a title.</blockquote>\n"
        f"{DIVIDER}\n"
        "<b>1.</b> 🔎 Send a movie or series title\n"
        "<b>2.</b> 🎯 Choose the best matching result\n"
        "<b>3.</b> 📺 Pick a season and episode when needed\n"
        "<b>4.</b> ▶️ Select a version to receive the protected file\n\n"
        "📚 <b>Watchlists</b>\n"
        "Use /watchlist to save catalog or custom titles and explore public community lists.\n\n"
        "💡 <b>Search tip:</b> Add a year to narrow results, for example "
        "<code>Dune 2021</code>.",
        reply_markup=home_button_markup(),
    )


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext, config: Config) -> None:
    await state.clear()
    await message.answer("↩️ <b>Action cancelled</b>\nNo changes were made.")
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
        "🔎 <b>SEARCH THE LIBRARY</b>\n"
        "<blockquote>No command needed—just send a title.</blockquote>\n"
        f"{DIVIDER}\n"
        "⌨️ Type the name of a movie or series in your next message.\n\n"
        "💡 <b>Search tips</b>\n"
        "• Keep it simple: <code>Dark</code>\n"
        "• Add a year: <code>Dune 2021</code>\n"
        "• The closest match always appears first.",
        home_button_markup(),
    )


@router.callback_query(F.data == "menu:help")
async def help_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    text = (
        "❓ <b>HELP & QUICK GUIDE</b>\n"
        "<blockquote>Search, choose, and watch in a few taps.</blockquote>\n"
        f"{DIVIDER}\n"
        "<b>1.</b> 🔎 Send any movie or series title\n"
        "<b>2.</b> 🎯 Open the best match\n"
        "<b>3.</b> 📺 Choose season, episode, or movie version\n"
        "<b>4.</b> ▶️ Tap the green delivery button\n\n"
        "📚 Use <b>Watchlist</b> to save catalog or custom titles.\n"
        "🔐 Delivered files use Telegram content protection."
    )
    await edit_screen(callback, text, home_button_markup())


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
        await callback.answer()
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [ActionButton(text="◀️ Choose another category", callback_data="menu:browse")],
                [ActionButton(text="🏠 Main menu", callback_data="menu:home")],
            ]
        )
        await edit_screen(
            callback,
            f"🫙 <b>{safe_html(category.name.upper())} IS EMPTY</b>\n"
            f"<blockquote>No indexed titles are available here yet.</blockquote>\n{DIVIDER}\n"
            "✨ Check Recently Added or choose another collection.",
            markup,
        )
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
        await callback.answer()
        await edit_screen(
            callback,
            "✨ <b>RECENTLY ADDED</b>\n"
            "<blockquote>Fresh arrivals will appear here.</blockquote>\n"
            f"{DIVIDER}\n"
            "🫙 <i>No titles have been indexed yet.</i>",
            home_button_markup(),
        )
        return
    session = sessions.create(
        callback.from_user.id, "Recently added", [item.id for item in contents]
    )
    text, markup = search_results(session, contents, 0)
    await callback.answer()
    await edit_screen(callback, text, markup)

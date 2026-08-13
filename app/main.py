from __future__ import annotations

import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from .commands import register_commands
from .config import Config, ConfigError
from .handlers import admin, channel, common, panel, search, watchlist
from .models import CatalogState, UsersState
from .panels import PanelActivityMiddleware, PanelManager
from .repositories import CatalogRepairResult, CatalogRepository, UserRepository
from .services import CatalogQueryService, SearchSessionStore
from .storage import StateStore, StorageError, TelegramSnapshotBackend
from .utils import safe_html

LOGGER = logging.getLogger(__name__)


async def _validate_database_channel(bot: Bot, channel_id: int, name: str) -> None:
    chat = await bot.get_chat(channel_id)
    if chat.type != ChatType.CHANNEL:
        raise RuntimeError(f"{name} database ID does not refer to a channel")
    if chat.username:
        raise RuntimeError(f"{name} database channel must be private")
    member = await bot.get_chat_member(channel_id, (await bot.get_me()).id)
    if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        raise RuntimeError(f"Bot must be an administrator in the {name} database channel")
    if member.status == ChatMemberStatus.ADMINISTRATOR:
        if not getattr(member, "can_post_messages", False):
            raise RuntimeError(f"Bot needs Post Messages permission in the {name} database")
        if not getattr(member, "can_edit_messages", False):
            raise RuntimeError(
                f"Bot needs Edit Messages permission in the {name} database to maintain its manifest"
            )


async def _write_repair_cards(
    bot: Bot,
    config: Config,
    catalog: CatalogRepository,
    repair: CatalogRepairResult,
) -> None:
    """Append corrected human-readable cards after the authoritative repair commit."""

    for file_id in repair.repaired_file_ids:
        record = catalog.get_file(file_id)
        if record is None:
            continue
        category = catalog.get_category(record.category_id)
        languages = ", ".join(record.languages) if record.languages else "Unknown"
        details = [
            "🔧 FILE INDEX REPAIRED",
            "",
            f"File ID: <code>{record.id}</code>",
            f"Content ID: <code>{record.content_id}</code>",
            f"Title: {safe_html(record.title)}",
            f"Category: {safe_html(category.name if category else record.category_id)}",
            f"Year: {record.year or 'Unknown'}",
            f"Language: {safe_html(languages)}",
            f"Quality: {safe_html(record.quality or 'Unknown')}",
        ]
        if record.season is not None:
            details.append(f"Season: {record.season}")
        if record.episode is not None:
            details.append(f"Episode: {record.episode}")
        if record.pack_part is not None:
            details.append(f"Season pack part: {record.pack_part}")
        details.append(f"Source message: <code>{record.source_message_id}</code>")
        try:
            await bot.send_message(
                config.file_database_channel_id,
                "\n".join(details),
                disable_notification=True,
            )
        except Exception:
            # The Telegram snapshot is already committed; a display-only card cannot roll it back.
            LOGGER.warning("Could not write corrected audit card for %s", file_id, exc_info=True)


def create_application(config: Config) -> web.Application:
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())

    catalog_backend = TelegramSnapshotBackend(bot, config.file_database_channel_id, "catalog")
    users_backend = TelegramSnapshotBackend(bot, config.user_database_channel_id, "users")
    catalog_store = StateStore(catalog_backend, CatalogState, CatalogState)
    users_store = StateStore(users_backend, UsersState, UsersState)
    catalog = CatalogRepository(catalog_store)
    users = UserRepository(users_store)
    query = CatalogQueryService(catalog)
    sessions = SearchSessionStore()
    panels = PanelManager(bot, users)

    dispatcher.callback_query.outer_middleware(PanelActivityMiddleware())
    dispatcher.message.outer_middleware(PanelActivityMiddleware())

    # Stateful admin handlers must run before the plain-text search handler.
    dispatcher.include_router(admin.router)
    dispatcher.include_router(panel.router)
    dispatcher.include_router(common.router)
    dispatcher.include_router(watchlist.router)
    dispatcher.include_router(search.router)
    dispatcher.include_router(channel.router)

    dispatcher["config"] = config
    dispatcher["catalog"] = catalog
    dispatcher["users"] = users
    dispatcher["query"] = query
    dispatcher["sessions"] = sessions
    dispatcher["panels"] = panels

    async def on_startup(bot: Bot) -> None:
        await _validate_database_channel(bot, config.file_database_channel_id, "file/catalog")
        await _validate_database_channel(bot, config.user_database_channel_id, "user")
        await catalog_store.initialize()
        await users_store.initialize()
        await catalog.migrate_schema()
        await users.migrate_schema()
        cleaned_workspaces = await panels.cleanup_stale_workspaces()
        if cleaned_workspaces:
            LOGGER.info("Removed %s stale workspace references after restart", cleaned_workspaces)
        repair = await catalog.repair_episodic_grouping()
        if repair.changed:
            LOGGER.warning(
                "Repaired episodic grouping: %s files canonicalized, %s duplicate titles merged",
                repair.updated_files,
                repair.merged_contents,
            )
            await _write_repair_cards(bot, config, catalog, repair)
        await register_commands(bot, config)
        await bot.set_webhook(
            url=config.webhook_url,
            secret_token=config.webhook_secret_token,
            allowed_updates=dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        identity = await bot.get_me()
        LOGGER.info(
            "Started @%s with catalog r%s and users r%s",
            identity.username,
            catalog.snapshot().revision,
            users.snapshot().revision,
        )

    async def on_shutdown() -> None:
        await panels.shutdown()

    dispatcher.startup.register(on_startup)
    dispatcher.shutdown.register(on_shutdown)

    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        try:
            data = {
                "status": "ok",
                "catalog_revision": catalog.snapshot().revision,
                "users_revision": users.snapshot().revision,
            }
            return web.json_response(data)
        except StorageError:
            return web.json_response({"status": "starting"}, status=503)

    async def root(_: web.Request) -> web.Response:
        return web.json_response({"service": "telegram-media-catalog-bot"})

    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        handle_in_background=False,
        secret_token=config.webhook_secret_token,
    ).register(app, path=config.webhook_path)
    setup_application(app, dispatcher, bot=bot)
    return app


def run() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    web.run_app(create_application(config), host=config.host, port=config.port)

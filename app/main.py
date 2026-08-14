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
from .ingestion import CatalogIngestBatcher, IndexAuditBatcher
from .models import CatalogState, RemovedSourceRecord, UsersState
from .panels import PanelActivityMiddleware, PanelManager
from .repositories import CatalogRepairResult, CatalogRepository, UserRepository
from .services import CatalogQueryService, SearchSessionStore
from .storage import (
    MAX_COMPRESSED_SNAPSHOT_BYTES,
    StateStore,
    StorageError,
    TelegramSnapshotBackend,
)
from .utils import normalize_title, safe_html

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
        if record.episode_start is not None and record.episode_end is not None:
            details.append(f"Combined episodes: {record.episode_start}–{record.episode_end}")
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


async def _purge_movie_nobita_doraemon(
    bot: Bot,
    config: Config,
    catalog: CatalogRepository,
) -> dict[str, object]:
    """One-release maintenance: purge matching Movie/Movies titles, never Cartoon Movie."""

    snapshot = catalog.snapshot()
    target_categories = [
        category
        for category in snapshot.categories.values()
        if normalize_title(category.name) in {"movie", "movies"}
        or normalize_title(category.slug) in {"movie", "movies"}
    ]
    if not target_categories:
        LOGGER.error(
            "Movie/Nobita maintenance found no exact Movie or Movies category; available=%s",
            sorted(category.name for category in snapshot.categories.values()),
        )
        return {
            "status": "category_not_found",
            "categories": 0,
            "titles_removed": 0,
            "files_removed": 0,
            "source_posts_deleted": 0,
            "source_posts_pending": 0,
        }

    target_category_ids = {category.id for category in target_categories}
    target_source_chats = {
        channel_id
        for category in target_categories
        for channel_id in (category.active_channel_id, *category.legacy_channel_ids)
    }
    title_terms = {"nobita", "doraemon", "doremon"}

    matching_contents = sorted(
        (
            content
            for content in snapshot.contents.values()
            if content.category_id in target_category_ids
            and title_terms.intersection(normalize_title(content.title).split())
        ),
        key=lambda content: (content.title.casefold(), content.id),
    )
    removed_titles: list[str] = []
    removed_file_count = 0
    source_candidates: dict[tuple[int, int], RemovedSourceRecord] = {
        (source.source_chat_id, source.source_message_id): source
        for source in catalog.pending_removed_sources()
        if source.source_chat_id in target_source_chats
        and title_terms.intersection(normalize_title(source.content_title).split())
    }
    actor_id = min(config.owner_ids)
    for content in matching_contents:
        try:
            result = await catalog.remove_content(content.id, actor_id)
        except ValueError:
            # A retry or overlapping channel update may already have removed this exact title.
            continue
        removed_titles.append(result.content.title)
        removed_file_count += len(result.files)
        source_candidates.update(
            {(source.source_chat_id, source.source_message_id): source for source in result.sources}
        )

    deleted, failed = await admin._delete_source_posts(
        bot,
        catalog,
        list(source_candidates.values()),
    )
    status = {
        "status": "complete",
        "categories": len(target_categories),
        "titles_removed": len(removed_titles),
        "files_removed": removed_file_count,
        "source_posts_deleted": deleted,
        "source_posts_pending": failed,
    }
    LOGGER.warning(
        "Completed Movie-only Nobita/Doraemon purge: %s; titles=%s; categories=%s",
        status,
        removed_titles,
        [category.name for category in target_categories],
    )
    try:
        await bot.send_message(
            config.file_database_channel_id,
            "🧹 <b>MOVIE CATEGORY CLEANUP COMPLETE</b>\n"
            "<blockquote>Nobita + Doraemon/Doremon • Cartoon Movie excluded</blockquote>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🎬 <b>Titles removed</b>  •  {len(removed_titles)}\n"
            f"📦 <b>Files removed</b>  •  {removed_file_count}\n"
            f"✅ <b>Source posts deleted</b>  •  {deleted}\n"
            f"🕓 <b>Pending manual/retry deletion</b>  •  {failed}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📚 Watchlists and every non-Movie category were left unchanged.",
            disable_notification=True,
        )
    except Exception:
        LOGGER.warning("Could not write Movie-category cleanup card", exc_info=True)
    return status


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
    ingest_batcher = CatalogIngestBatcher(catalog)
    index_audit_batcher = IndexAuditBatcher(bot, config.file_database_channel_id)
    runtime_status: dict[str, object] = {
        "threaded_mode_enabled": None,
        "movie_nobita_cleanup": {"status": "pending"},
    }

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
    dispatcher["ingest_batcher"] = ingest_batcher
    dispatcher["index_audit_batcher"] = index_audit_batcher

    async def on_startup(bot: Bot) -> None:
        await _validate_database_channel(bot, config.file_database_channel_id, "file/catalog")
        await _validate_database_channel(bot, config.user_database_channel_id, "user")
        await catalog_store.initialize()
        await users_store.initialize()
        await catalog.migrate_schema()
        await users.migrate_schema()
        recovered_deliveries = await panels.recover_temporary_deliveries()
        if recovered_deliveries:
            LOGGER.warning(
                "Recovered %s temporary file deliveries for restart-safe expiry",
                recovered_deliveries,
            )
        retired_topics, failed_topics = await panels.cleanup_delivery_topics()
        if retired_topics:
            LOGGER.warning(
                "Permanently retired %s legacy delivery topics during flat-chat migration",
                retired_topics,
            )
        if failed_topics:
            LOGGER.warning(
                "%s legacy delivery topics could not be retired and will be retried on restart",
                failed_topics,
            )
        else:
            LOGGER.info(
                "Legacy delivery-topic cleanup complete (%s retired this startup; none pending)",
                retired_topics,
            )
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
        runtime_status["movie_nobita_cleanup"] = await _purge_movie_nobita_doraemon(
            bot,
            config,
            catalog,
        )
        await register_commands(bot, config)
        await bot.set_webhook(
            url=config.webhook_url,
            secret_token=config.webhook_secret_token,
            allowed_updates=dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        identity = await bot.get_me()
        threaded_mode_enabled = bool(identity.has_topics_enabled)
        runtime_status["threaded_mode_enabled"] = threaded_mode_enabled
        if threaded_mode_enabled:
            LOGGER.warning(
                "Flat-chat delivery is active but BotFather Threaded Mode is still enabled; "
                "disable it to restore Telegram's normal private-chat interface"
            )
        else:
            LOGGER.info("BotFather Threaded Mode is disabled; flat-chat delivery is active")
        LOGGER.info(
            "Started @%s with catalog r%s and users r%s",
            identity.username,
            catalog.revision(),
            users.snapshot().revision,
        )

    async def on_shutdown() -> None:
        await ingest_batcher.shutdown()
        await index_audit_batcher.shutdown()
        await panels.shutdown()

    dispatcher.startup.register(on_startup)
    dispatcher.shutdown.register(on_shutdown)

    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        try:
            data = {
                "status": "ok",
                "catalog_revision": catalog.revision(),
                "catalog_files": catalog.file_count(),
                "catalog_snapshot_bytes": catalog_backend.current_compressed_size(),
                "catalog_snapshot_limit_bytes": MAX_COMPRESSED_SNAPSHOT_BYTES,
                "users_revision": users.snapshot().revision,
                "threaded_mode_enabled": runtime_status["threaded_mode_enabled"],
                "delivery_mode": "flat_chat_temporary",
                "delivery_expiry_seconds": panels.delivery_expiry_seconds,
                "pending_temporary_deliveries": panels.pending_temporary_delivery_count(),
                "pending_legacy_topics": panels.pending_delivery_topic_count(),
                "pending_catalog_ingest": ingest_batcher.pending_count(),
                "movie_nobita_cleanup": runtime_status["movie_nobita_cleanup"],
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

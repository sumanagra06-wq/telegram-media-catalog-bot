from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.types import Message

from ..config import Config
from ..metadata import MetadataParseError, ParsedMetadata, parse_metadata
from ..models import CategoryMode, MediaType
from ..repositories import CatalogRepository
from ..utils import safe_html

LOGGER = logging.getLogger(__name__)
router = Router(name="channel-indexing")


def _media(message: Message) -> tuple[MediaType, str, str, str | None] | None:
    if message.video:
        return (
            MediaType.VIDEO,
            message.video.file_id,
            message.video.file_unique_id,
            message.video.file_name,
        )
    if message.document:
        return (
            MediaType.DOCUMENT,
            message.document.file_id,
            message.document.file_unique_id,
            message.document.file_name,
        )
    return None


def _validate_mode(mode: CategoryMode, metadata: ParsedMetadata) -> None:
    is_series = metadata.season is not None or metadata.episode is not None
    if mode == CategoryMode.SINGLE and is_series:
        raise MetadataParseError("Episode metadata was posted in a single-title category")
    if mode == CategoryMode.EPISODIC and metadata.season is None:
        raise MetadataParseError("Series entries require a season number")


async def _notify_failure(
    bot: Bot,
    config: Config,
    catalog: CatalogRepository,
    source_chat_id: int,
    source_message_id: int,
    source_title: str,
    category_id: str,
    reason: str,
) -> None:
    await catalog.record_failure(
        source_chat_id,
        source_message_id,
        category_id,
        reason,
    )
    text = (
        "❌ <b>Indexing failed</b>\n\n"
        f"Channel: {safe_html(source_title)}\n"
        f"Message: <code>{source_message_id}</code>\n"
        f"Reason: {safe_html(reason)}"
    )
    try:
        await bot.send_message(config.file_database_channel_id, text, disable_notification=True)
    except Exception:
        LOGGER.warning("Could not write indexing failure audit", exc_info=True)
    for owner_id in config.owner_ids:
        try:
            await bot.send_message(owner_id, text)
        except Exception:
            LOGGER.debug("Could not notify owner %s", owner_id, exc_info=True)


async def index_source_message(
    message: Message,
    bot: Bot,
    config: Config,
    catalog: CatalogRepository,
    *,
    source_chat_id: int | None = None,
    source_message_id: int | None = None,
    source_title: str | None = None,
    allow_legacy: bool = False,
) -> bool:
    source_chat_id = source_chat_id if source_chat_id is not None else message.chat.id
    source_message_id = source_message_id if source_message_id is not None else message.message_id
    category = catalog.category_for_channel(source_chat_id)
    if category is None:
        return False
    # Legacy channels remain readable for existing files. Only an explicit owner import may
    # add a previously missed post from a legacy channel.
    if (source_chat_id != category.active_channel_id and not allow_legacy) or not category.enabled:
        return False
    media = _media(message)
    if media is None:
        return False
    media_type, file_id, unique_id, filename = media
    try:
        metadata = parse_metadata(message.caption, filename)
        _validate_mode(category.mode, metadata)
    except MetadataParseError as exc:
        await _notify_failure(
            bot,
            config,
            catalog,
            source_chat_id,
            source_message_id,
            source_title or message.chat.title or str(source_chat_id),
            category.id,
            str(exc),
        )
        return False

    record, content, created = await catalog.upsert_file(
        category_id=category.id,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        telegram_file_id=file_id,
        telegram_file_unique_id=unique_id,
        media_type=media_type,
        metadata=metadata,
    )
    languages = ", ".join(record.languages) if record.languages else "Unknown"
    year = str(record.year) if record.year else "Unknown"
    details = [
        f"{'✅ FILE INDEXED' if created else '✏️ FILE INDEX UPDATED'}",
        "",
        f"File ID: <code>{record.id}</code>",
        f"Content ID: <code>{content.id}</code>",
        f"Title: {safe_html(record.title)}",
        f"Category: {safe_html(category.name)}",
        f"Year: {year}",
        f"Language: {safe_html(languages)}",
        f"Quality: {safe_html(record.quality or 'Unknown')}",
    ]
    if record.season is not None:
        details.append(f"Season: {record.season}")
    if record.episode is not None:
        details.append(f"Episode: {record.episode}")
    if record.pack_part is not None:
        details.append(f"Season pack part: {record.pack_part}")
    details.append(f"Source message: <code>{source_message_id}</code>")
    try:
        await bot.send_message(
            config.file_database_channel_id,
            "\n".join(details),
            disable_notification=True,
        )
    except Exception:
        # Index commit is authoritative; a human-readable audit card is optional.
        LOGGER.warning("Index succeeded but its audit card could not be sent", exc_info=True)
    return True


@router.channel_post()
async def channel_post(
    message: Message,
    bot: Bot,
    config: Config,
    catalog: CatalogRepository,
) -> None:
    await index_source_message(message, bot, config, catalog)


@router.edited_channel_post()
async def edited_channel_post(
    message: Message,
    bot: Bot,
    config: Config,
    catalog: CatalogRepository,
) -> None:
    await index_source_message(message, bot, config, catalog)

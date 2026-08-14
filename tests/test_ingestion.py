import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from app.config import Config
from app.handlers.channel import index_source_message
from app.ingestion import CatalogIngestBatcher, IndexAuditBatcher, IndexAuditEntry
from app.metadata import parse_metadata
from app.models import CatalogState, CategoryMode, MediaType
from app.repositories import CatalogRepository, FileUpsertRequest
from app.services import CatalogQueryService
from app.storage import MemorySnapshotBackend, StateStore, StorageError
from app.ui import content_screen, season_screen


class FakeBot:
    def __init__(self):
        self.sent_messages = []
        self.send_attempts = 0
        self.send_floods = 0

    async def send_message(self, chat_id, text, **kwargs):
        self.send_attempts += 1
        if self.send_floods:
            self.send_floods -= 1
            raise TelegramRetryAfter(
                SendMessage(chat_id=chat_id, text=text),
                "flood control",
                retry_after=34,
            )
        self.sent_messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(self.sent_messages))


def _config():
    return Config(
        bot_token="123:test",
        owner_ids=frozenset({999}),
        file_database_channel_id=-1001,
        user_database_channel_id=-1002,
        webhook_base_url="https://example.test",
        webhook_path="/telegram/webhook",
        webhook_secret_token="safe_secret_123456789",
        host="0.0.0.0",
        port=8080,
        log_level="INFO",
    )


def _message(message_id: int, filename: str):
    return SimpleNamespace(
        chat=SimpleNamespace(id=-10010, title="Large Series"),
        message_id=message_id,
        caption=filename,
        video=SimpleNamespace(
            file_id=f"file-{message_id}",
            file_unique_id=f"unique-{message_id}",
            file_name=filename,
        ),
        document=None,
    )


async def test_one_hundred_file_burst_uses_one_snapshot_and_one_audit_summary():
    backend = MemorySnapshotBackend("catalog")
    store = StateStore(backend, CatalogState, CatalogState)
    await store.initialize()
    catalog = CatalogRepository(store)
    category = await catalog.add_category(
        "Large Series",
        -10010,
        "Large Series",
        CategoryMode.EPISODIC,
    )
    bot = FakeBot()
    ingest = CatalogIngestBatcher(catalog, delay_seconds=10, max_batch_size=100)
    audits = IndexAuditBatcher(bot, -1001, delay_seconds=10, max_batch_size=100)

    filenames = [f"Flexible Show S16E{episode:03d} 1080p Hindi.mkv" for episode in range(1, 56)]
    filenames.extend(
        f"Flexible.Show.S{season:02d}.Ep.1to5.Combined.Hindi.mkv"
        for season in range(1, 19)
        if season != 16
    )
    filenames.extend(
        f"Flexible Show S18E{episode:03d} 720p English.mkv" for episode in range(1, 29)
    )
    assert len(filenames) == 100
    revision = catalog.snapshot().revision
    commits = len(backend.commits)

    indexed = await asyncio.gather(
        *[
            index_source_message(
                _message(message_id, filename),
                bot,
                _config(),
                catalog,
                ingest_batcher=ingest,
                index_audit_batcher=audits,
            )
            for message_id, filename in enumerate(filenames, start=1)
        ]
    )
    await ingest.shutdown()
    await audits.shutdown()

    assert all(indexed)
    assert catalog.snapshot().revision == revision + 1
    assert len(backend.commits) == commits + 1
    assert len(catalog.snapshot().files) == 100
    assert len(bot.sent_messages) == 1
    assert "CATALOG BURST INDEXED" in bot.sent_messages[0][1]
    assert "Files processed</b>  •  100" in bot.sent_messages[0][1]

    query = CatalogQueryService(catalog)
    content = query.search("Flexible Show")[0].content
    assert query.seasons(content.id) == list(range(1, 19))
    assert query.episodes(content.id, 16) == list(range(1, 56))

    content_text, content_markup = content_screen(
        content=content,
        category=category,
        query=query,
    )
    assert "18 seasons available" in content_text
    assert "Season 18" in str(content_markup)

    first_text, first_markup = season_screen(content, 16, query, "0", 0, 0)
    last_text, last_markup = season_screen(content, 16, query, "0", 0, 2)
    assert "Episodes available: <b>55</b>" in first_text
    assert "Page 1/3" in first_text
    assert "E01" in str(first_markup)
    assert "Page 3/3" in last_text
    assert "E55" in str(last_markup)


async def test_delayed_ingest_batch_rolls_back_together_and_can_retry_after_storage_failure():
    backend = MemorySnapshotBackend("catalog")
    store = StateStore(backend, CatalogState, CatalogState)
    await store.initialize()
    catalog = CatalogRepository(store)
    category = await catalog.add_category(
        "Series",
        -10010,
        "Series",
        CategoryMode.EPISODIC,
    )
    ingest = CatalogIngestBatcher(catalog, delay_seconds=0.01, max_batch_size=100)

    def request(message_id: int) -> FileUpsertRequest:
        return FileUpsertRequest(
            category_id=category.id,
            source_chat_id=-10010,
            source_message_id=message_id,
            telegram_file_id=f"file-{message_id}",
            telegram_file_unique_id=f"unique-{message_id}",
            media_type=MediaType.VIDEO,
            metadata=parse_metadata(f"Recovery Show S20E{message_id:02d} 1080p Hindi.mkv"),
        )

    revision = catalog.snapshot().revision
    backend.fail_next_commit = True
    failed = await asyncio.gather(
        ingest.submit(request(1)),
        ingest.submit(request(2)),
        return_exceptions=True,
    )

    assert all(isinstance(item, StorageError) for item in failed)
    assert catalog.snapshot().revision == revision
    assert catalog.snapshot().files == {}

    record, content, created = await ingest.submit(request(1))
    await ingest.shutdown()

    assert created is True
    assert record.episode == 1
    assert content.title == "Recovery Show"
    assert catalog.snapshot().revision == revision + 1


async def test_batched_audit_summary_honors_telegram_flood_wait(monkeypatch):
    bot = FakeBot()
    bot.send_floods = 1
    audits = IndexAuditBatcher(bot, -1001, delay_seconds=10, max_batch_size=1)
    delays = []

    async def no_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("app.ingestion.asyncio.sleep", no_sleep)
    await audits.submit(
        IndexAuditEntry(
            detail_text="✅ FILE INDEXED",
            created=True,
            category_name="Series",
            title="Flexible Show",
            source_message_id=1,
            season=16,
            episode=55,
        )
    )
    await audits.shutdown()

    assert bot.send_attempts == 2
    assert delays == [34.1]
    assert bot.sent_messages[0][1] == "✅ FILE INDEXED"

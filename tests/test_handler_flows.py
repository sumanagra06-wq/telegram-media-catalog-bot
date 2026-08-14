from types import SimpleNamespace

from app.config import Config
from app.handlers.channel import index_source_message
from app.handlers.search import content_callback, episode_callback, plain_title_search
from app.main import _write_repair_cards
from app.metadata import parse_metadata
from app.models import CatalogState, CategoryMode, MediaType, UsersState
from app.repositories import CatalogRepairResult, CatalogRepository, UserRepository
from app.services import CatalogQueryService, SearchSessionStore
from app.storage import MemorySnapshotBackend, StateStore


class FakeBot:
    def __init__(self):
        self.sent_messages = []
        self.copied_messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text, kwargs))

    async def copy_message(self, **kwargs):
        self.copied_messages.append(kwargs)


class FakePrivateMessage:
    def __init__(self, user, text=None):
        self.chat = SimpleNamespace(type="private")
        self.from_user = user
        self.text = text
        self.answers = []
        self.edits = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeCallback:
    def __init__(self, user, data, message):
        self.from_user = user
        self.data = data
        self.message = message
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


async def _repositories():
    catalog_store = StateStore(MemorySnapshotBackend("catalog"), CatalogState, CatalogState)
    users_store = StateStore(MemorySnapshotBackend("users"), UsersState, UsersState)
    await catalog_store.initialize()
    await users_store.initialize()
    return CatalogRepository(catalog_store), UserRepository(users_store)


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
        protect_delivered_content=True,
    )


async def test_plain_search_to_episode_delivery_flow():
    catalog, users = await _repositories()
    category = await catalog.add_category("Series", -10010, "Series")
    record, content, _ = await catalog.upsert_file(
        category_id=category.id,
        source_chat_id=-10010,
        source_message_id=55,
        telegram_file_id="telegram-file",
        telegram_file_unique_id="unique-file",
        media_type=MediaType.VIDEO,
        metadata=parse_metadata("Dark S01E01 1080p Hindi mkv"),
    )
    query = CatalogQueryService(catalog)
    sessions = SearchSessionStore()
    bot = FakeBot()
    user = SimpleNamespace(
        id=42,
        first_name="User",
        last_name=None,
        username=None,
        language_code="en",
    )

    search_message = FakePrivateMessage(user, "Dark")
    await plain_title_search(search_message, bot, _config(), users, query, sessions)
    assert "Results for" in search_message.answers[0][0]
    result_markup = search_message.answers[0][1]["reply_markup"]
    content_data = result_markup.inline_keyboard[0][0].callback_data

    screen = FakePrivateMessage(user)
    callback = FakeCallback(user, content_data, screen)
    await content_callback(callback, catalog, query, users, _config())
    assert "Dark" in screen.edits[-1][0]
    assert "Season 1" in str(screen.edits[-1][1]["reply_markup"])

    episode = FakeCallback(user, f"ep:{content.id}:1:1:0:0", screen)
    await episode_callback(episode, bot, catalog, query, users, _config())
    assert bot.copied_messages == [
        {
            "chat_id": 42,
            "from_chat_id": -10010,
            "message_id": 55,
            "caption": (
                "📺 <b>Dark</b>\n"
                "<blockquote>Series collection • protected delivery</blockquote>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "▶️ <b>Episode</b>  •  Season 1, Episode 1\n"
                "📅 <b>Year</b>  •  Unknown\n"
                "🗣 <b>Language</b>  •  Hindi\n"
                "💎 <b>Quality</b>  •  1080p\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🔐 Protected delivery • kept in your category archive."
            ),
            "parse_mode": "HTML",
            "protect_content": True,
        }
    ]
    assert catalog.get_file(record.id).available is True


async def test_channel_post_indexing_uses_allowlisted_metadata_only():
    catalog, _ = await _repositories()
    category = await catalog.add_category("Series", -10010, "Series")
    bot = FakeBot()
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-10010, title="Series"),
        message_id=7,
        caption=(
            "@UHDPrime Game of Thrones S03 E01 BluRay 720p Hindi 2 0 English mkv\n"
            "Please forward in another chat"
        ),
        video=SimpleNamespace(file_id="video-file", file_unique_id="video-unique", file_name=None),
        document=None,
    )
    indexed = await index_source_message(message, bot, _config(), catalog)
    assert indexed is True
    state = catalog.snapshot()
    record = next(iter(state.files.values()))
    assert record.category_id == category.id
    assert record.title == "Game of Thrones"
    assert record.languages == ["Hindi", "English"]
    assert record.quality == "720p"
    assert record.season == 3
    assert record.episode == 1
    assert "forward" not in record.model_dump_json().casefold()


async def test_six_separate_labeled_episode_messages_share_one_content_record():
    catalog, _ = await _repositories()
    await catalog.add_category("Series", -10010, "Series", CategoryMode.EPISODIC)
    bot = FakeBot()
    warning = (
        "⚠️ ❌👉This file automatically❗delete after 1 minute❗so please forward "
        "in another chat👈❌"
    )

    for episode in range(1, 7):
        filename = f"Operation Safed Sagar The Highest Air Force Mission S01E{episode:02d} 1 mkv"
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10010, title="Series"),
            message_id=episode + 2,
            caption=f"Name: {filename}\n\n{warning}",
            video=SimpleNamespace(
                file_id=f"video-{episode}",
                file_unique_id=f"video-unique-{episode}",
                file_name=filename,
            ),
            document=None,
        )
        assert await index_source_message(message, bot, _config(), catalog) is True

    state = catalog.snapshot()
    assert len(state.contents) == 1
    assert len(state.files) == 6
    content = next(iter(state.contents.values()))
    assert content.title == "Operation Safed Sagar The Highest Air Force Mission"
    assert content.file_ids == list(state.files)
    assert {record.content_id for record in state.files.values()} == {content.id}
    assert {(record.season, record.episode) for record in state.files.values()} == {
        (1, episode) for episode in range(1, 7)
    }
    audit_content_lines = {
        next(line for line in text.splitlines() if line.startswith("Content ID:"))
        for _, text, _ in bot.sent_messages
    }
    assert audit_content_lines == {f"Content ID: <code>{content.id}</code>"}

    repair = CatalogRepairResult(
        updated_files=6,
        repaired_file_ids=tuple(state.files),
    )
    await _write_repair_cards(bot, _config(), catalog, repair)
    repair_cards = [text for _, text, _ in bot.sent_messages if "FILE INDEX REPAIRED" in text]
    assert len(repair_cards) == 6
    assert {
        next(line for line in text.splitlines() if line.startswith("Content ID:"))
        for text in repair_cards
    } == {f"Content ID: <code>{content.id}</code>"}

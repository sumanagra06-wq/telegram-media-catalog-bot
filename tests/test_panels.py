import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from app.handlers.panel import (
    panel_admin_callback,
    panel_browse_callback,
    panel_browse_category_callback,
    panel_recent_callback,
    retired_non_watchlist_selection,
)
from app.handlers.search import file_callback, plain_title_search
from app.metadata import parse_metadata
from app.models import CatalogState, MediaType, UsersState, WatchStatus
from app.panels import PanelManager
from app.repositories import CatalogRepository, UserRepository
from app.services import CatalogQueryService, SearchSessionStore
from app.storage import MemorySnapshotBackend, StateStore, StorageError
from app.ui import panel_dashboard


class FakePanelBot:
    def __init__(self):
        self.next_message_id = 100
        self.sent = []
        self.edited = []
        self.pinned = []
        self.unpinned = []
        self.deleted = []
        self.copied = []
        self.sent_videos = []
        self.sent_documents = []
        self.events = []
        self.unavailable_edits = set()
        self.topics_enabled = True
        self.next_topic_id = 900
        self.created_topics = []
        self.reopened_topics = []
        self.deleted_topics = []
        self.invalid_topic_ids = set()
        self.copy_fail_once_topic_ids = set()
        self.copy_closed_once_topic_ids = set()
        self.invalidate_created_topics = False
        self.fail_topic_creation = False
        self.source_copy_missing = False

    async def get_me(self):
        return SimpleNamespace(has_topics_enabled=self.topics_enabled)

    async def create_forum_topic(self, chat_id, name, **kwargs):
        if self.fail_topic_creation:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=chat_id, message_id=1, text=name),
                message="Bad Request: topics are not enabled",
            )
        topic = SimpleNamespace(message_thread_id=self.next_topic_id)
        self.next_topic_id += 1
        self.created_topics.append((chat_id, name, kwargs, topic.message_thread_id))
        if self.invalidate_created_topics:
            self.invalid_topic_ids.add(topic.message_thread_id)
        return topic

    async def reopen_forum_topic(self, chat_id, message_thread_id):
        if message_thread_id in self.invalid_topic_ids:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=chat_id, message_id=1, text="reopen"),
                message="Bad Request: message thread not found",
            )
        self.reopened_topics.append((chat_id, message_thread_id))
        return True

    async def delete_forum_topic(self, chat_id, message_thread_id):
        self.deleted_topics.append((chat_id, message_thread_id))
        return True

    async def send_message(self, chat_id, text, **kwargs):
        message = SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        self.sent.append((chat_id, text, kwargs, message.message_id))
        self.events.append(("send", message.message_id))
        return message

    async def copy_message(self, **kwargs):
        thread_id = kwargs.get("message_thread_id")
        if self.source_copy_missing:
            raise TelegramBadRequest(
                method=EditMessageText(
                    chat_id=kwargs["chat_id"],
                    message_id=1,
                    text="copy",
                ),
                message="Bad Request: message to copy not found",
            )
        if thread_id in self.copy_closed_once_topic_ids:
            self.copy_closed_once_topic_ids.remove(thread_id)
            raise TelegramBadRequest(
                method=EditMessageText(
                    chat_id=kwargs["chat_id"],
                    message_id=1,
                    text="copy",
                ),
                message="Bad Request: topic closed",
            )
        if thread_id in self.copy_fail_once_topic_ids:
            self.copy_fail_once_topic_ids.remove(thread_id)
            raise TelegramBadRequest(
                method=EditMessageText(
                    chat_id=kwargs["chat_id"],
                    message_id=1,
                    text="copy",
                ),
                message="Bad Request: message thread not found",
            )
        if thread_id in self.invalid_topic_ids:
            raise TelegramBadRequest(
                method=EditMessageText(
                    chat_id=kwargs["chat_id"],
                    message_id=1,
                    text="copy",
                ),
                message="Bad Request: message thread not found",
            )
        message = SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        self.copied.append(kwargs)
        self.events.append(("copy", message.message_id))
        return message

    async def send_video(self, **kwargs):
        message = SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        self.sent_videos.append(kwargs)
        self.events.append(("video", message.message_id))
        return message

    async def send_document(self, **kwargs):
        message = SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        self.sent_documents.append(kwargs)
        self.events.append(("document", message.message_id))
        return message

    async def edit_message_text(self, *, chat_id, message_id, text, **kwargs):
        if message_id in self.unavailable_edits:
            raise TelegramBadRequest(
                method=EditMessageText(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                ),
                message="Bad Request: message to edit not found",
            )
        self.edited.append((chat_id, message_id, text, kwargs))
        return True

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self.pinned.append((chat_id, message_id, kwargs))
        return True

    async def unpin_chat_message(self, chat_id, message_id):
        self.unpinned.append((chat_id, message_id))
        return True

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    async def edit_message_reply_markup(self, **kwargs):
        return True


class FakeCallback:
    def __init__(self, user, data, message_id):
        self.from_user = user
        self.data = data
        self.message = SimpleNamespace(message_id=message_id)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class FakeState:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


class FakePrivateMessage:
    def __init__(self, user, text):
        self.chat = SimpleNamespace(type="private")
        self.from_user = user
        self.text = text
        self.answers = []
        self.message_id = 500

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


async def _repositories():
    catalog_store = StateStore(MemorySnapshotBackend("catalog"), CatalogState, CatalogState)
    users_store = StateStore(MemorySnapshotBackend("users"), UsersState, UsersState)
    await catalog_store.initialize()
    await users_store.initialize()
    return CatalogRepository(catalog_store), UserRepository(users_store)


async def _register(users, user_id=42):
    await users.ensure_user(
        user_id=user_id,
        first_name="Alice",
        last_name=None,
        username="alice",
        language_code="en",
    )


async def _seed_movie(catalog, *, media_type=MediaType.VIDEO):
    category = await catalog.add_category("Movies", -10010, "Movies")
    record, _, _ = await catalog.upsert_file(
        category_id=category.id,
        source_chat_id=-10010,
        source_message_id=1,
        telegram_file_id="file-1",
        telegram_file_unique_id="unique-1",
        media_type=media_type,
        metadata=parse_metadata("Arrival 2016 1080p English mkv"),
    )
    return record


def _user(user_id=42):
    return SimpleNamespace(
        id=user_id,
        first_name="Alice",
        last_name=None,
        username="alice",
        language_code="en",
    )


def _config():
    from app.config import Config

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


async def test_pinned_dashboard_is_persisted_reused_and_recovered():
    _, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    text, markup = panel_dashboard(False, "Alice")

    first_id = await panels.ensure_dashboard(user_id=42, text=text, reply_markup=markup)
    second_id = await panels.ensure_dashboard(user_id=42, text=text, reply_markup=markup)

    assert first_id == second_id == 100
    assert len(bot.sent) == 1
    assert bot.edited[-1][1] == 100
    assert bot.pinned == [
        (42, 100, {"disable_notification": True}),
        (42, 100, {"disable_notification": True}),
    ]
    assert users.get_user(42).panel_dashboard_message_id == 100

    bot.unavailable_edits.add(100)
    recovered_id = await panels.ensure_dashboard(user_id=42, text=text, reply_markup=markup)
    assert recovered_id == 101
    assert users.get_user(42).panel_dashboard_message_id == 101
    assert (42, 100) in bot.deleted


async def test_emergency_dashboard_repost_retires_the_previous_pinned_card():
    _, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 77)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    text, markup = panel_dashboard(False, "Alice")

    replacement_id = await panels.repost_dashboard(
        user_id=42,
        text=text,
        reply_markup=markup,
    )

    assert replacement_id == 100
    assert users.get_user(42).panel_dashboard_message_id == 100
    assert bot.pinned == [(42, 100, {"disable_notification": True})]
    assert bot.unpinned == [(42, 77)]
    assert bot.deleted == [(42, 77)]


async def test_emergency_dashboard_repost_rolls_back_when_snapshot_commit_fails():
    _, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 77)
    backend = users.store.backend
    assert isinstance(backend, MemorySnapshotBackend)
    backend.fail_next_commit = True
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    text, markup = panel_dashboard(False, "Alice")

    with pytest.raises(StorageError):
        await panels.repost_dashboard(user_id=42, text=text, reply_markup=markup)

    assert users.get_user(42).panel_dashboard_message_id == 77
    assert bot.unpinned == [(42, 100)]
    assert bot.deleted == [(42, 100)]
    assert (42, 77) not in bot.deleted


async def test_delivery_topic_is_created_persisted_and_reused():
    _, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)

    first = await panels.ensure_delivery_topic(42)
    second = await panels.ensure_delivery_topic(42)

    assert first == second == 900
    assert users.get_user(42).delivery_topic_id == 900
    assert bot.created_topics == [(42, "📦 Deliveries", {"icon_color": 7_322_096}, 900)]
    assert bot.reopened_topics == []
    assert len(bot.sent) == 1
    assert bot.sent[0][2]["message_thread_id"] == 900
    assert "never removed" in bot.sent[0][1]


async def test_deleted_delivery_topic_is_replaced_without_deleting_old_content():
    _, users = await _repositories()
    await _register(users)
    await users.set_delivery_topic(42, 899)
    bot = FakePanelBot()
    bot.invalid_topic_ids.add(899)
    panels = PanelManager(bot, users)

    replacement = await panels.ensure_delivery_topic(42, replace=True)

    assert replacement == 900
    assert users.get_user(42).delivery_topic_id == 900
    assert bot.created_topics == [(42, "📦 Deliveries", {"icon_color": 7_322_096}, 900)]
    assert bot.deleted_topics == []


async def test_new_empty_delivery_topic_is_deleted_if_persistence_fails():
    _, users = await _repositories()
    await _register(users)
    backend = users.store.backend
    assert isinstance(backend, MemorySnapshotBackend)
    backend.fail_next_commit = True
    bot = FakePanelBot()
    panels = PanelManager(bot, users)

    with pytest.raises(StorageError):
        await panels.ensure_delivery_topic(42)

    assert users.get_user(42).delivery_topic_id is None
    assert bot.deleted_topics == [(42, 900)]
    assert bot.sent == []


async def test_delivery_topic_unavailable_when_threaded_mode_is_disabled():
    _, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    bot.topics_enabled = False
    panels = PanelManager(bot, users)

    assert await panels.ensure_delivery_topic(42) is None
    assert users.get_user(42).delivery_topic_id is None
    assert bot.created_topics == []


async def test_workspace_reuses_one_message_and_sliding_expiry_deletes_it():
    _, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    panels = PanelManager(bot, users, expiry_seconds=0.08)
    _, markup = panel_dashboard(False)

    first_id, second_id = await asyncio.gather(
        panels.render_workspace(
            user_id=42,
            text="First workspace screen",
            reply_markup=markup,
        ),
        panels.render_workspace(
            user_id=42,
            text="Second workspace screen",
            reply_markup=markup,
        ),
    )
    assert first_id == second_id == 100
    assert len(bot.sent) == 1
    assert bot.edited[-1][1:3] == (100, "Second workspace screen")

    await asyncio.sleep(0.04)
    assert panels.touch(42, 100) is True
    await asyncio.sleep(0.05)
    assert users.get_user(42).panel_workspace_message_id == 100
    await asyncio.sleep(0.05)
    assert users.get_user(42).panel_workspace_message_id is None
    assert bot.deleted[-1] == (42, 100)
    await panels.shutdown()


async def test_successful_delivery_uses_permanent_topic_and_closes_workspace():
    catalog, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 50)
    category = await catalog.add_category("Movies", -10010, "Movies")
    record, _, _ = await catalog.upsert_file(
        category_id=category.id,
        source_chat_id=-10010,
        source_message_id=1,
        telegram_file_id="file-1",
        telegram_file_unique_id="unique-1",
        media_type=MediaType.VIDEO,
        metadata=parse_metadata("Arrival 2016 1080p English mkv"),
    )
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    _, markup = panel_dashboard(False)
    first_workspace = await panels.render_workspace(
        user_id=42,
        text="File details",
        reply_markup=markup,
    )
    user = SimpleNamespace(
        id=42,
        first_name="Alice",
        last_name=None,
        username="alice",
        language_code="en",
    )
    callback = FakeCallback(user, f"fl:{record.id}", first_workspace)
    revision_before_delivery = users.snapshot().revision

    await file_callback(callback, bot, catalog, users, _config(), panels)

    profile = users.get_user(42)
    assert users.snapshot().revision == revision_before_delivery + 2
    assert bot.events == [("send", 100), ("send", 101), ("copy", 102)]
    assert bot.created_topics == [(42, "📦 Deliveries", {"icon_color": 7_322_096}, 900)]
    assert bot.copied[-1]["message_thread_id"] == 900
    assert profile.delivery_topic_id == 900
    assert profile.panel_dashboard_message_id == 50
    assert profile.panel_workspace_message_id is None
    assert bot.deleted == [(42, 100)]
    assert (42, 102) not in bot.deleted
    assert "DELIVERY INBOX" in bot.sent[-1][1]
    await panels.shutdown()


async def test_missing_source_post_uses_document_file_id_inside_delivery_topic():
    catalog, users = await _repositories()
    await _register(users)
    record = await _seed_movie(catalog, media_type=MediaType.DOCUMENT)
    bot = FakePanelBot()
    bot.source_copy_missing = True
    panels = PanelManager(bot, users)
    callback = FakeCallback(_user(), f"fl:{record.id}", 77)

    await file_callback(callback, bot, catalog, users, _config(), panels)

    assert bot.sent_documents[-1]["document"] == "file-1"
    assert bot.sent_documents[-1]["message_thread_id"] == 900
    delivered_id = bot.events[-1][1]
    assert catalog.get_file(record.id).available is True
    assert bot.deleted == [(42, 77)]
    assert (42, delivered_id) not in bot.deleted


async def test_closed_delivery_topic_is_reopened_before_retry():
    catalog, users = await _repositories()
    await _register(users)
    record = await _seed_movie(catalog)
    await users.set_delivery_topic(42, 899)
    bot = FakePanelBot()
    bot.copy_closed_once_topic_ids.add(899)
    panels = PanelManager(bot, users)
    callback = FakeCallback(_user(), f"fl:{record.id}", 77)

    await file_callback(callback, bot, catalog, users, _config(), panels)

    assert bot.reopened_topics == [(42, 899)]
    assert bot.copied[-1]["message_thread_id"] == 899
    delivered_id = bot.events[-1][1]
    assert bot.deleted == [(42, 77)]
    assert (42, delivered_id) not in bot.deleted


async def test_invalid_delivery_topic_is_replaced_and_delivery_retried():
    catalog, users = await _repositories()
    await _register(users)
    record = await _seed_movie(catalog)
    await users.set_delivery_topic(42, 899)
    bot = FakePanelBot()
    bot.copy_fail_once_topic_ids.add(899)
    panels = PanelManager(bot, users)
    callback = FakeCallback(_user(), f"fl:{record.id}", 77)

    await file_callback(callback, bot, catalog, users, _config(), panels)

    assert users.get_user(42).delivery_topic_id == 900
    assert bot.created_topics[-1][-1] == 900
    assert bot.copied[-1]["message_thread_id"] == 900
    assert bot.deleted_topics == []
    assert bot.deleted == [(42, 77)]


async def test_disabled_threaded_mode_falls_back_to_general_delivery():
    catalog, users = await _repositories()
    await _register(users)
    record = await _seed_movie(catalog)
    bot = FakePanelBot()
    bot.topics_enabled = False
    panels = PanelManager(bot, users)
    callback = FakeCallback(_user(), f"fl:{record.id}", 77)

    await file_callback(callback, bot, catalog, users, _config(), panels)

    assert users.get_user(42).delivery_topic_id is None
    assert bot.created_topics == []
    assert bot.copied[-1]["message_thread_id"] is None
    assert bot.deleted == [(42, 77)]


async def test_topic_persistence_failure_falls_back_to_general_delivery():
    catalog, users = await _repositories()
    await _register(users)
    record = await _seed_movie(catalog)
    backend = users.store.backend
    assert isinstance(backend, MemorySnapshotBackend)
    backend.fail_next_commit = True
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    callback = FakeCallback(_user(), f"fl:{record.id}", 77)

    await file_callback(callback, bot, catalog, users, _config(), panels)

    assert users.get_user(42).delivery_topic_id is None
    assert bot.deleted_topics == [(42, 900)]
    assert bot.copied[-1]["message_thread_id"] is None
    assert bot.deleted == [(42, 77)]


async def test_topic_replacement_failure_falls_back_to_general_delivery():
    catalog, users = await _repositories()
    await _register(users)
    record = await _seed_movie(catalog)
    await users.set_delivery_topic(42, 899)
    bot = FakePanelBot()
    bot.copy_fail_once_topic_ids.add(899)
    bot.invalidate_created_topics = True
    panels = PanelManager(bot, users)
    callback = FakeCallback(_user(), f"fl:{record.id}", 77)

    await file_callback(callback, bot, catalog, users, _config(), panels)

    assert users.get_user(42).delivery_topic_id == 900
    assert bot.copied[-1]["message_thread_id"] is None
    delivered_id = bot.events[-1][1]
    assert bot.deleted == [(42, 77)]
    assert (42, delivered_id) not in bot.deleted


async def test_restart_cleanup_removes_only_workspace_reference():
    _, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 10)
    await users.set_panel_workspace_message(42, 11)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)

    assert await panels.cleanup_stale_workspaces() == 1
    profile = users.get_user(42)
    assert profile.panel_dashboard_message_id == 10
    assert profile.panel_workspace_message_id is None
    assert bot.deleted == [(42, 11)]


async def test_owner_dashboard_is_unified_and_discovery_has_no_watchlist_mutations():
    owner_text, owner_markup = panel_dashboard(True)
    user_text, user_markup = panel_dashboard(False)
    owner_callbacks = {
        button.callback_data for row in owner_markup.inline_keyboard for button in row
    }
    user_callbacks = {button.callback_data for row in user_markup.inline_keyboard for button in row}
    assert "p:admin" in owner_callbacks
    assert "p:admin" not in user_callbacks
    assert {"p:search", "p:browse", "p:recent", "p:watchlist"} <= owner_callbacks
    assert "Deliveries topic" in owner_text
    assert "Watchlist additions stay inside" in user_text
    assert not any(
        callback and callback.startswith(("px:", "pa:", "pw:"))
        for callback in owner_callbacks | user_callbacks
    )


async def test_admin_control_center_rejects_non_owner_even_from_valid_dashboard():
    _, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 77)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    user = SimpleNamespace(
        id=42,
        first_name="Alice",
        last_name=None,
        username="alice",
        language_code="en",
    )
    callback = FakeCallback(user, "p:admin", 77)
    state = FakeState()

    await panel_admin_callback(callback, users, _config(), panels, state)

    assert callback.answers[-1] == ("Owner access required.", {"show_alert": True})
    assert state.cleared is False
    assert bot.sent == []


async def test_browse_and_recent_flows_render_delivery_only_results_in_one_workspace():
    catalog, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 77)
    category = await catalog.add_category("Movies", -10010, "Movies")
    _, content, _ = await catalog.upsert_file(
        category_id=category.id,
        source_chat_id=-10010,
        source_message_id=1,
        telegram_file_id="file-1",
        telegram_file_unique_id="unique-1",
        media_type=MediaType.VIDEO,
        metadata=parse_metadata("Arrival 2016 1080p English mkv"),
    )
    query = CatalogQueryService(catalog)
    sessions = SearchSessionStore()
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    user = SimpleNamespace(
        id=42,
        first_name="Alice",
        last_name=None,
        username="alice",
        language_code="en",
    )
    state = FakeState()

    browse = FakeCallback(user, "p:browse", 77)
    await panel_browse_callback(
        browse,
        catalog,
        users,
        _config(),
        panels,
        state,
    )
    workspace_id = users.get_user(42).panel_workspace_message_id
    assert workspace_id == 100
    assert len(bot.sent) == 1
    assert "BROWSE FOR FILES" in bot.sent[-1][1]

    category_callback = FakeCallback(user, f"pb:{category.id}", workspace_id)
    await panel_browse_category_callback(
        category_callback,
        query,
        sessions,
        catalog,
        users,
        _config(),
        panels,
    )
    browse_buttons = [
        button for row in bot.edited[-1][3]["reply_markup"].inline_keyboard for button in row
    ]
    assert not any(button.text in {"☐", "✅"} for button in browse_buttons)
    assert not any(
        button.callback_data and button.callback_data.startswith(("px:", "pa:", "pw:"))
        for button in browse_buttons
    )
    assert any(
        button.callback_data and button.callback_data.startswith("ct:") for button in browse_buttons
    )

    recent = FakeCallback(user, "p:recent", workspace_id)
    await panel_recent_callback(
        recent,
        query,
        sessions,
        users,
        _config(),
        panels,
        state,
    )
    assert "RECENTLY ADDED" in bot.edited[-1][2]
    assert content.title in str(bot.edited[-1][3]["reply_markup"])
    assert len(bot.sent) == 1
    assert users.get_user(42).panel_workspace_message_id == workspace_id
    await panels.shutdown()


async def test_stale_discovery_bulk_callbacks_cannot_mutate_watchlist():
    _, users = await _repositories()
    await _register(users)
    user = SimpleNamespace(
        id=42,
        first_name="Alice",
        last_name=None,
        username="alice",
        language_code="en",
    )

    for data in ("px:old:content:0", "pa:old:0", "pw:old:t:0"):
        callback = FakeCallback(user, data, 100)
        await retired_non_watchlist_selection(callback)
        assert callback.answers == [
            (
                "Watchlist additions now live only inside the Watchlist tab.",
                {"show_alert": True},
            )
        ]

    assert users.get_user(42).watchlist == {}


async def test_bulk_watchlist_insert_is_atomic_and_updates_existing_entries():
    catalog, users = await _repositories()
    await _register(users)
    category = await catalog.add_category("Movies", -10010, "Movies")
    contents = []
    for index, title in enumerate(("Arrival 2016", "Dune 2021"), start=1):
        _, content, _ = await catalog.upsert_file(
            category_id=category.id,
            source_chat_id=-10010,
            source_message_id=index,
            telegram_file_id=f"file-{index}",
            telegram_file_unique_id=f"unique-{index}",
            media_type=MediaType.VIDEO,
            metadata=parse_metadata(f"{title} 1080p English mkv"),
        )
        contents.append(content)

    first = await users.bulk_upsert_catalog_watchlist(
        user_id=42,
        items=[(content, category.name) for content in contents],
        status=WatchStatus.TO_WATCH,
    )
    second = await users.bulk_upsert_catalog_watchlist(
        user_id=42,
        items=[(content, category.name) for content in contents],
        status=WatchStatus.COMPLETED,
    )
    backend = users.store.backend
    assert isinstance(backend, MemorySnapshotBackend)
    backend.fail_next_commit = True
    with pytest.raises(StorageError):
        await users.bulk_upsert_catalog_watchlist(
            user_id=42,
            items=[(content, category.name) for content in contents],
            status=WatchStatus.ON_HOLD,
        )

    profile = users.get_user(42)
    assert first.created == 2 and first.updated == 0
    assert second.created == 0 and second.updated == 2
    assert len(profile.watchlist) == 2
    assert {entry.status for entry in profile.watchlist.values()} == {WatchStatus.COMPLETED}


async def test_manual_watchlist_batch_is_deduplicated_atomic_and_updates_existing():
    _, users = await _repositories()
    await _register(users)
    first = await users.bulk_upsert_manual_watchlist(
        user_id=42,
        titles=["Arrival", " Dune ", "arrival", ""],
        category_id="cat_movies",
        category_name="Movies",
        status=WatchStatus.TO_WATCH,
    )
    second = await users.bulk_upsert_manual_watchlist(
        user_id=42,
        titles=["Arrival", "Dune"],
        category_id="cat_movies",
        category_name="Movies",
        status=WatchStatus.COMPLETED,
    )
    backend = users.store.backend
    assert isinstance(backend, MemorySnapshotBackend)
    backend.fail_next_commit = True
    with pytest.raises(StorageError):
        await users.bulk_upsert_manual_watchlist(
            user_id=42,
            titles=["Arrival", "Dune", "Heat"],
            category_id="cat_movies",
            category_name="Movies",
            status=WatchStatus.ON_HOLD,
        )

    profile = users.get_user(42)
    assert first.created == 2 and first.updated == 0
    assert second.created == 0 and second.updated == 2
    assert {entry.title for entry in profile.watchlist.values()} == {"Arrival", "Dune"}
    assert {entry.status for entry in profile.watchlist.values()} == {WatchStatus.COMPLETED}
    with pytest.raises(ValueError, match="25"):
        await users.bulk_upsert_manual_watchlist(
            user_id=42,
            titles=[f"Title {index}" for index in range(26)],
            category_id="cat_movies",
            category_name="Movies",
            status=WatchStatus.TO_WATCH,
        )
    assert len(users.get_user(42).watchlist) == 2


async def test_plain_search_edits_active_workspace_instead_of_creating_result_card():
    catalog, users = await _repositories()
    await _register(users)
    category = await catalog.add_category("Movies", -10010, "Movies")
    await catalog.upsert_file(
        category_id=category.id,
        source_chat_id=-10010,
        source_message_id=1,
        telegram_file_id="file-1",
        telegram_file_unique_id="unique-1",
        media_type=MediaType.VIDEO,
        metadata=parse_metadata("Arrival 2016 1080p English mkv"),
    )
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    _, markup = panel_dashboard(False)
    await panels.render_workspace(user_id=42, text="Search prompt", reply_markup=markup)
    user = SimpleNamespace(
        id=42,
        first_name="Alice",
        last_name=None,
        username="alice",
        language_code="en",
    )
    message = FakePrivateMessage(user, "Arrival")

    await plain_title_search(
        message,
        bot,
        _config(),
        users,
        CatalogQueryService(catalog),
        SearchSessionStore(),
        panels,
    )

    assert message.answers == []
    assert "Showing <b>1</b> title" in bot.edited[-1][2]
    assert "Selected" not in bot.edited[-1][2]
    result_buttons = [
        button for row in bot.edited[-1][3]["reply_markup"].inline_keyboard for button in row
    ]
    assert not any(button.text in {"☐", "✅"} for button in result_buttons)
    assert not any(
        button.callback_data and button.callback_data.startswith(("px:", "pa:", "pw:"))
        for button in result_buttons
    )
    assert bot.deleted == [(42, 500)]
    assert len(bot.sent) == 1
    await panels.shutdown()


async def test_plain_search_ignores_messages_inside_delivery_topics():
    catalog, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    message = FakePrivateMessage(_user(), "Arrival")
    message.message_thread_id = 900

    await plain_title_search(
        message,
        bot,
        _config(),
        users,
        CatalogQueryService(catalog),
        SearchSessionStore(),
        panels,
    )

    assert message.answers == []
    assert bot.sent == []
    assert bot.edited == []
    assert bot.deleted == []


async def test_plain_search_validation_screen_also_cleans_typed_query():
    catalog, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    message = FakePrivateMessage(_user(), "x")

    await plain_title_search(
        message,
        bot,
        _config(),
        users,
        CatalogQueryService(catalog),
        SearchSessionStore(),
        panels,
    )

    assert "SEARCH NEEDS A LITTLE MORE" in bot.sent[-1][1]
    assert bot.deleted == [(42, 500)]
    assert users.get_user(42).panel_workspace_message_id == 100
    await panels.shutdown()

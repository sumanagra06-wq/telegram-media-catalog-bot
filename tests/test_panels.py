import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
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
from app.models import (
    CatalogState,
    CategoryMode,
    DeliveryTopicRef,
    MediaType,
    UserProfile,
    UsersState,
    UserStatus,
    WatchStatus,
)
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
        self.failed_delete_message_ids = set()
        self.missing_delete_message_ids = set()
        self.transient_delete_message_ids = set()
        self.delete_message_attempts = []
        self.deleted_topics = []
        self.failed_delete_topic_ids = set()
        self.missing_delete_topic_ids = set()
        self.disabled_delete_topic_ids = set()
        self.transient_delete_topic_ids = set()
        self.delete_topic_attempts = []
        self.source_copy_missing = False
        self.fail_next_send = False
        self.send_delay_seconds = 0.0

    async def delete_forum_topic(self, chat_id, message_thread_id):
        self.delete_topic_attempts.append((chat_id, message_thread_id))
        if message_thread_id in self.transient_delete_topic_ids:
            self.transient_delete_topic_ids.remove(message_thread_id)
            raise TelegramNetworkError(
                method=EditMessageText(chat_id=chat_id, message_id=1, text="delete"),
                message="temporary network failure",
            )
        if message_thread_id in self.disabled_delete_topic_ids:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=chat_id, message_id=1, text="delete"),
                message="Bad Request: chat is not a forum",
            )
        if message_thread_id in self.missing_delete_topic_ids:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=chat_id, message_id=1, text="delete"),
                message="Bad Request: message thread not found",
            )
        if message_thread_id in self.failed_delete_topic_ids:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=chat_id, message_id=1, text="delete"),
                message="Bad Request: not enough rights",
            )
        self.deleted_topics.append((chat_id, message_thread_id))
        return True

    async def send_message(self, chat_id, text, **kwargs):
        if self.send_delay_seconds:
            await asyncio.sleep(self.send_delay_seconds)
        if self.fail_next_send:
            self.fail_next_send = False
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=chat_id, message_id=1, text=text),
                message="Bad Request: send failed",
            )
        message = SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        self.sent.append((chat_id, text, kwargs, message.message_id))
        self.events.append(("send", message.message_id))
        return message

    async def copy_message(self, **kwargs):
        if self.source_copy_missing:
            raise TelegramBadRequest(
                method=EditMessageText(
                    chat_id=kwargs["chat_id"],
                    message_id=1,
                    text="copy",
                ),
                message="Bad Request: message to copy not found",
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
        self.delete_message_attempts.append((chat_id, message_id))
        if message_id in self.transient_delete_message_ids:
            self.transient_delete_message_ids.remove(message_id)
            raise TelegramNetworkError(
                method=EditMessageText(chat_id=chat_id, message_id=message_id, text="delete"),
                message="temporary network failure",
            )
        if message_id in self.missing_delete_message_ids:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=chat_id, message_id=message_id, text="delete"),
                message="Bad Request: message to delete not found",
            )
        if message_id in self.failed_delete_message_ids:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=chat_id, message_id=message_id, text="delete"),
                message="Bad Request: not enough rights",
            )
        self.deleted.append((chat_id, message_id))
        return True

    async def edit_message_reply_markup(self, **kwargs):
        return True


class FakeCallback:
    def __init__(self, user, data, message_id):
        self.from_user = user
        self.data = data
        self.message_answers = []

        async def answer_message(text, **kwargs):
            self.message_answers.append((text, kwargs))

        self.message = SimpleNamespace(message_id=message_id, answer=answer_message)
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


async def _set_delivery_topic_history(users, *, legacy=None, categories=None):
    def mutate(state):
        profile = state.users["42"]
        profile.delivery_topic_id = legacy
        profile.delivery_topics = categories or {}

    await users.store.mutate(mutate)


async def _seed_movie(catalog, *, media_type=MediaType.VIDEO):
    category = await catalog.add_category(
        "Movies",
        -10010,
        "Movies",
        mode=CategoryMode.SINGLE,
    )
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


async def test_bulk_dashboard_repost_uses_one_atomic_snapshot_commit():
    _, users = await _repositories()
    await _register(users, 42)
    await _register(users, 43)
    await users.set_panel_dashboard_message(42, 50)
    await users.set_panel_dashboard_message(43, 60)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    text_42, markup_42 = panel_dashboard(False, "Alice")
    text_43, markup_43 = panel_dashboard(False, "Bob")
    revision = users.snapshot().revision

    refreshed, failed = await panels.repost_dashboards(
        [(42, text_42, markup_42), (43, text_43, markup_43)]
    )

    assert (refreshed, failed) == (2, 0)
    assert users.snapshot().revision == revision + 1
    assert users.get_user(42).panel_dashboard_message_id == 100
    assert users.get_user(43).panel_dashboard_message_id == 101
    assert bot.pinned == [
        (42, 100, {"disable_notification": True}),
        (43, 101, {"disable_notification": True}),
    ]
    assert bot.unpinned == [(42, 50), (43, 60)]
    assert bot.deleted == [(42, 50), (43, 60)]


async def test_bulk_dashboard_repost_rolls_back_only_a_compare_and_set_conflict(monkeypatch):
    _, users = await _repositories()
    await _register(users, 42)
    await _register(users, 43)
    await users.set_panel_dashboard_message(42, 50)
    await users.set_panel_dashboard_message(43, 60)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    text, markup = panel_dashboard(False, "Alice")
    changed = False

    async def concurrent_dashboard_change(_delay):
        nonlocal changed
        if not changed:
            changed = True
            await users.set_panel_dashboard_message(42, 70)

    monkeypatch.setattr("app.panels.asyncio.sleep", concurrent_dashboard_change)
    refreshed, failed = await panels.repost_dashboards([(42, text, markup), (43, text, markup)])

    assert (refreshed, failed) == (1, 1)
    assert users.get_user(42).panel_dashboard_message_id == 70
    assert users.get_user(43).panel_dashboard_message_id == 101
    assert bot.unpinned == [(42, 100), (43, 60)]
    assert bot.deleted == [(42, 100), (43, 60)]
    assert (42, 50) not in bot.deleted


async def test_bulk_dashboard_repost_rolls_back_every_new_card_on_commit_failure():
    _, users = await _repositories()
    await _register(users, 42)
    await _register(users, 43)
    await users.set_panel_dashboard_message(42, 50)
    await users.set_panel_dashboard_message(43, 60)
    backend = users.store.backend
    assert isinstance(backend, MemorySnapshotBackend)
    backend.fail_next_commit = True
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    text, markup = panel_dashboard(False, "Alice")

    with pytest.raises(StorageError):
        await panels.repost_dashboards([(42, text, markup), (43, text, markup)])

    assert users.get_user(42).panel_dashboard_message_id == 50
    assert users.get_user(43).panel_dashboard_message_id == 60
    assert bot.unpinned == [(42, 100), (43, 101)]
    assert bot.deleted == [(42, 100), (43, 101)]


async def test_flat_chat_migration_deletes_all_recorded_topics_and_clears_references():
    _, users = await _repositories()
    await _register(users)
    await _set_delivery_topic_history(
        users,
        legacy=899,
        categories={
            "cat_movies": DeliveryTopicRef(message_thread_id=899, name="🎬 Movies"),
            "cat_series": DeliveryTopicRef(message_thread_id=900, name="📺 Series"),
        },
    )
    bot = FakePanelBot()
    panels = PanelManager(bot, users)

    retired, failed = await panels.cleanup_delivery_topics()

    profile = users.get_user(42)
    assert (retired, failed) == (2, 0)
    assert set(bot.deleted_topics) == {(42, 899), (42, 900)}
    assert profile.delivery_topic_id is None
    assert profile.delivery_topics == {}


async def test_topic_cleanup_treats_missing_as_retired_but_retains_real_failures():
    _, users = await _repositories()
    await _register(users)
    await _set_delivery_topic_history(
        users,
        categories={
            "cat_movies": DeliveryTopicRef(message_thread_id=899, name="🎬 Movies"),
            "cat_series": DeliveryTopicRef(message_thread_id=900, name="📺 Series"),
        },
    )
    bot = FakePanelBot()
    bot.missing_delete_topic_ids.add(899)
    bot.failed_delete_topic_ids.add(900)
    panels = PanelManager(bot, users)

    retired, failed = await panels.cleanup_delivery_topics()

    profile = users.get_user(42)
    assert (retired, failed) == (1, 1)
    assert "cat_movies" not in profile.delivery_topics
    assert profile.delivery_topics["cat_series"].message_thread_id == 900


async def test_topic_cleanup_retains_reference_when_threaded_mode_is_disabled():
    _, users = await _repositories()
    await _register(users)
    await _set_delivery_topic_history(users, legacy=899)
    bot = FakePanelBot()
    bot.disabled_delete_topic_ids.add(899)
    panels = PanelManager(bot, users)

    retired, failed = await panels.cleanup_delivery_topics()

    assert (retired, failed) == (0, 1)
    assert panels.pending_delivery_topic_count() == 1
    assert users.get_user(42).delivery_topic_id == 899


async def test_topic_cleanup_retries_transient_telegram_failure(monkeypatch):
    _, users = await _repositories()
    await _register(users)
    await _set_delivery_topic_history(users, legacy=899)
    bot = FakePanelBot()
    bot.transient_delete_topic_ids.add(899)
    panels = PanelManager(bot, users)

    async def no_wait(_delay):
        return None

    monkeypatch.setattr("app.panels.asyncio.sleep", no_wait)
    retired, failed = await panels.cleanup_delivery_topics()

    assert (retired, failed) == (1, 0)
    assert bot.delete_topic_attempts == [(42, 899), (42, 899)]
    assert bot.deleted_topics == [(42, 899)]
    assert users.get_user(42).delivery_topic_id is None


async def test_topic_cleanup_retries_safely_if_reference_commit_fails():
    _, users = await _repositories()
    await _register(users)
    await _set_delivery_topic_history(users, legacy=899)
    backend = users.store.backend
    assert isinstance(backend, MemorySnapshotBackend)
    backend.fail_next_commit = True
    bot = FakePanelBot()
    panels = PanelManager(bot, users)

    with pytest.raises(StorageError):
        await panels.cleanup_delivery_topics()

    assert bot.deleted_topics == [(42, 899)]
    assert users.get_user(42).delivery_topic_id == 899


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


async def test_successful_flat_delivery_is_unprotected_temporary_and_keeps_dashboard():
    catalog, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 50)
    record = await _seed_movie(catalog)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    _, markup = panel_dashboard(False)
    workspace_id = await panels.render_workspace(
        user_id=42,
        text="File details",
        reply_markup=markup,
    )
    callback = FakeCallback(_user(), f"fl:{record.id}", workspace_id)
    revision_before_delivery = users.snapshot().revision

    await file_callback(callback, bot, catalog, users, _config(), panels)

    profile = users.get_user(42)
    # Provisional media registration, reminder attachment, then workspace cleanup are durable.
    assert users.snapshot().revision == revision_before_delivery + 3
    assert bot.copied[-1].get("message_thread_id") is None
    assert bot.copied[-1]["protect_content"] is False
    assert bot.events == [("send", 100), ("copy", 101), ("send", 102)]
    assert "SAVE WITHIN 5 MINUTES" in bot.sent[-1][1]
    assert profile.panel_dashboard_message_id == 50
    assert profile.panel_workspace_message_id is None
    assert [
        (item.media_message_id, item.notice_message_id) for item in profile.temporary_deliveries
    ] == [(101, 102)]
    remaining = datetime.fromisoformat(profile.temporary_deliveries[0].expires_at) - datetime.now(
        UTC
    )
    assert 298 < remaining.total_seconds() <= 300
    assert bot.pinned == []
    assert bot.unpinned == []
    assert bot.deleted == [(42, 100)]
    await panels.shutdown()


async def test_repeated_flat_deliveries_are_independently_tracked_without_dashboard_reposts():
    catalog, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 50)
    record = await _seed_movie(catalog)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    _, markup = panel_dashboard(False)

    for _ in range(2):
        workspace_id = await panels.render_workspace(
            user_id=42,
            text="File details",
            reply_markup=markup,
        )
        await file_callback(
            FakeCallback(_user(), f"fl:{record.id}", workspace_id),
            bot,
            catalog,
            users,
            _config(),
            panels,
        )

    profile = users.get_user(42)
    assert profile.panel_dashboard_message_id == 50
    assert profile.panel_workspace_message_id is None
    assert [
        (item.media_message_id, item.notice_message_id) for item in profile.temporary_deliveries
    ] == [
        (101, 102),
        (104, 105),
    ]
    assert bot.deleted == [(42, 100), (42, 103)]
    assert bot.pinned == []
    assert bot.unpinned == []
    await panels.shutdown()


async def test_missing_source_post_uses_unprotected_document_file_id_and_tracks_expiry():
    catalog, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 50)
    record = await _seed_movie(catalog, media_type=MediaType.DOCUMENT)
    bot = FakePanelBot()
    bot.source_copy_missing = True
    panels = PanelManager(bot, users)
    callback = FakeCallback(_user(), f"fl:{record.id}", 77)

    await file_callback(callback, bot, catalog, users, _config(), panels)

    assert bot.sent_documents[-1]["document"] == "file-1"
    assert bot.sent_documents[-1]["protect_content"] is False
    assert "message_thread_id" not in bot.sent_documents[-1]
    assert catalog.get_file(record.id).available is True
    profile = users.get_user(42)
    assert profile.panel_dashboard_message_id == 50
    assert [
        (item.media_message_id, item.notice_message_id) for item in profile.temporary_deliveries
    ] == [(100, 101)]
    assert bot.deleted == [(42, 77)]
    await panels.shutdown()


async def test_missing_source_post_uses_unprotected_video_file_id_fallback():
    catalog, users = await _repositories()
    await _register(users)
    record = await _seed_movie(catalog, media_type=MediaType.VIDEO)
    bot = FakePanelBot()
    bot.source_copy_missing = True
    panels = PanelManager(bot, users)

    await file_callback(
        FakeCallback(_user(), f"fl:{record.id}", 77),
        bot,
        catalog,
        users,
        _config(),
        panels,
    )

    assert bot.sent_videos[-1]["video"] == "file-1"
    assert bot.sent_videos[-1]["protect_content"] is False
    assert [
        (item.media_message_id, item.notice_message_id)
        for item in users.get_user(42).temporary_deliveries
    ] == [(100, 101)]
    await panels.shutdown()


async def test_provisional_registration_failure_deletes_file_before_sending_notice():
    catalog, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 50)
    record = await _seed_movie(catalog)
    backend = users.store.backend
    assert isinstance(backend, MemorySnapshotBackend)
    backend.fail_next_commit = True
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    callback = FakeCallback(_user(), f"fl:{record.id}", 77)

    await file_callback(callback, bot, catalog, users, _config(), panels)

    profile = users.get_user(42)
    assert profile.panel_dashboard_message_id == 50
    assert profile.temporary_deliveries == []
    assert bot.sent == []
    assert bot.deleted == [(42, 100)]
    assert bot.unpinned == []
    assert callback.message_answers
    assert "cleanup could not be saved" in callback.message_answers[-1][0]


async def test_notice_attachment_persistence_failure_deletes_both_messages_and_reference():
    catalog, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 50)
    record = await _seed_movie(catalog)
    backend = users.store.backend
    assert isinstance(backend, MemorySnapshotBackend)

    class FailAttachBot(FakePanelBot):
        async def send_message(self, chat_id, text, **kwargs):
            message = await super().send_message(chat_id, text, **kwargs)
            backend.fail_next_commit = True
            return message

    bot = FailAttachBot()
    panels = PanelManager(bot, users)
    callback = FakeCallback(_user(), f"fl:{record.id}", 77)

    await file_callback(callback, bot, catalog, users, _config(), panels)

    profile = users.get_user(42)
    assert profile.panel_dashboard_message_id == 50
    assert profile.temporary_deliveries == []
    assert bot.deleted == [(42, 101), (42, 100)]
    assert callback.message_answers
    assert "reminder could not be safely tracked" in callback.message_answers[-1][0]
    await panels.shutdown()


async def test_delivery_notice_failure_removes_provisionally_registered_file():
    catalog, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 50)
    record = await _seed_movie(catalog)
    bot = FakePanelBot()
    bot.fail_next_send = True
    panels = PanelManager(bot, users)

    await file_callback(
        FakeCallback(_user(), f"fl:{record.id}", 77),
        bot,
        catalog,
        users,
        _config(),
        panels,
    )

    profile = users.get_user(42)
    assert profile.panel_dashboard_message_id == 50
    assert profile.temporary_deliveries == []
    assert bot.deleted == [(42, 100)]


async def test_notice_and_immediate_delete_failure_persists_recoverable_file_reference():
    catalog, users = await _repositories()
    await _register(users)
    record = await _seed_movie(catalog)
    bot = FakePanelBot()
    bot.fail_next_send = True
    bot.failed_delete_message_ids.add(100)
    panels = PanelManager(
        bot,
        users,
        delivery_expiry_seconds=0.04,
        delivery_retry_seconds=0.02,
    )

    await file_callback(
        FakeCallback(_user(), f"fl:{record.id}", 77),
        bot,
        catalog,
        users,
        _config(),
        panels,
    )
    await asyncio.sleep(0.02)
    profile = users.get_user(42)
    assert [
        (item.media_message_id, item.notice_message_id) for item in profile.temporary_deliveries
    ] == [(100, None)]

    bot.failed_delete_message_ids.clear()
    await asyncio.sleep(0.05)
    assert users.get_user(42).temporary_deliveries == []
    assert (42, 100) in bot.deleted
    await panels.shutdown()


async def test_temporary_delivery_expires_file_and_notice_but_not_dashboard():
    catalog, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 50)
    record = await _seed_movie(catalog)
    bot = FakePanelBot()
    panels = PanelManager(
        bot,
        users,
        delivery_expiry_seconds=0.03,
        delivery_retry_seconds=0.02,
    )

    await file_callback(
        FakeCallback(_user(), f"fl:{record.id}", 77),
        bot,
        catalog,
        users,
        _config(),
        panels,
    )
    assert panels.pending_temporary_delivery_count() == 1
    await asyncio.sleep(0.08)

    profile = users.get_user(42)
    assert profile.panel_dashboard_message_id == 50
    assert profile.temporary_deliveries == []
    assert bot.deleted == [(42, 77), (42, 101), (42, 100)]
    assert bot.pinned == []
    assert bot.unpinned == []
    await panels.shutdown()


async def test_notice_arriving_after_media_expiry_is_deleted_instead_of_orphaned():
    catalog, users = await _repositories()
    await _register(users)
    record = await _seed_movie(catalog)
    bot = FakePanelBot()
    bot.send_delay_seconds = 0.06
    panels = PanelManager(bot, users, delivery_expiry_seconds=0.02)
    callback = FakeCallback(_user(), f"fl:{record.id}", 77)

    await file_callback(callback, bot, catalog, users, _config(), panels)
    await asyncio.sleep(0.02)

    assert users.get_user(42).temporary_deliveries == []
    assert (42, 100) in bot.deleted
    assert (42, 101) in bot.deleted
    assert callback.message_answers
    await panels.shutdown()


async def test_five_minute_deadline_is_anchored_to_media_copy_before_notice_delay():
    catalog, users = await _repositories()
    await _register(users)
    record = await _seed_movie(catalog)
    bot = FakePanelBot()
    bot.send_delay_seconds = 0.20
    panels = PanelManager(bot, users, delivery_expiry_seconds=0.30)

    await file_callback(
        FakeCallback(_user(), f"fl:{record.id}", 77),
        bot,
        catalog,
        users,
        _config(),
        panels,
    )
    await asyncio.sleep(0.15)

    assert users.get_user(42).temporary_deliveries == []
    assert (42, 101) in bot.deleted
    assert (42, 100) in bot.deleted
    await panels.shutdown()


async def test_restart_recovers_already_expired_delivery_reference():
    _, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    first = PanelManager(bot, users, delivery_expiry_seconds=0.03)
    await first.register_temporary_delivery(
        user_id=42,
        media_message_id=200,
        notice_message_id=201,
    )
    await first.shutdown()
    await asyncio.sleep(0.04)

    backend = users.store.backend
    reloaded_store = StateStore(backend, UsersState, UsersState)
    await reloaded_store.initialize()
    reloaded_users = UserRepository(reloaded_store)
    recovered = PanelManager(bot, reloaded_users, delivery_retry_seconds=0.02)
    assert await recovered.recover_temporary_deliveries() == 1
    await asyncio.sleep(0.04)

    assert reloaded_users.get_user(42).temporary_deliveries == []
    assert bot.deleted == [(42, 201), (42, 200)]
    await recovered.shutdown()


async def test_restart_recovers_provisional_media_when_process_stops_before_notice():
    _, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    first = PanelManager(bot, users, delivery_expiry_seconds=0.03)
    await first.register_temporary_delivery(
        user_id=42,
        media_message_id=250,
        notice_message_id=None,
    )
    await first.shutdown()
    await asyncio.sleep(0.04)

    backend = users.store.backend
    reloaded_store = StateStore(backend, UsersState, UsersState)
    await reloaded_store.initialize()
    reloaded_users = UserRepository(reloaded_store)
    recovered = PanelManager(bot, reloaded_users)
    assert await recovered.recover_temporary_deliveries() == 1
    await asyncio.sleep(0.04)

    assert reloaded_users.get_user(42).temporary_deliveries == []
    assert bot.deleted == [(42, 250)]
    await recovered.shutdown()


async def test_restart_preserves_future_expiry_deadline():
    _, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    first = PanelManager(bot, users, delivery_expiry_seconds=0.12)
    await first.register_temporary_delivery(
        user_id=42,
        media_message_id=300,
        notice_message_id=301,
    )
    await first.shutdown()

    recovered = PanelManager(bot, users, delivery_retry_seconds=0.02)
    assert await recovered.recover_temporary_deliveries() == 1
    await asyncio.sleep(0.03)
    assert bot.deleted == []
    assert len(users.get_user(42).temporary_deliveries) == 1
    await asyncio.sleep(0.12)
    assert bot.deleted == [(42, 301), (42, 300)]
    assert users.get_user(42).temporary_deliveries == []
    await recovered.shutdown()


async def test_failed_expiry_is_retained_and_retried_until_both_messages_are_deleted():
    _, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    bot.failed_delete_message_ids.add(400)
    panels = PanelManager(
        bot,
        users,
        delivery_expiry_seconds=0.02,
        delivery_retry_seconds=0.03,
    )
    await panels.register_temporary_delivery(
        user_id=42,
        media_message_id=400,
        notice_message_id=401,
    )

    await asyncio.sleep(0.04)
    assert len(users.get_user(42).temporary_deliveries) == 1
    assert (42, 401) in bot.deleted
    assert (42, 400) not in bot.deleted

    bot.failed_delete_message_ids.clear()
    await asyncio.sleep(0.06)
    assert users.get_user(42).temporary_deliveries == []
    assert (42, 400) in bot.deleted
    await panels.shutdown()


async def test_stale_provisional_cleanup_cannot_remove_attached_notice_record():
    _, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    await panels.register_temporary_delivery(
        user_id=42,
        media_message_id=450,
        notice_message_id=None,
    )
    await panels.attach_temporary_delivery_notice(
        user_id=42,
        media_message_id=450,
        notice_message_id=451,
    )

    assert not await users.remove_temporary_delivery(
        42,
        450,
        expected_notice_message_id=None,
        match_notice=True,
    )
    assert users.get_user(42).temporary_deliveries[0].notice_message_id == 451
    assert await users.remove_temporary_delivery(
        42,
        450,
        expected_notice_message_id=451,
        match_notice=True,
    )
    await panels.shutdown()


async def test_already_missing_delivery_messages_clear_durable_expiry_reference():
    _, users = await _repositories()
    await _register(users)
    bot = FakePanelBot()
    bot.missing_delete_message_ids.update({500, 501})
    panels = PanelManager(bot, users, delivery_expiry_seconds=0.01)
    await panels.register_temporary_delivery(
        user_id=42,
        media_message_id=500,
        notice_message_id=501,
    )

    await asyncio.sleep(0.04)
    assert users.get_user(42).temporary_deliveries == []
    assert bot.deleted == []
    await panels.shutdown()


async def test_same_user_deliveries_remain_serialized_with_independent_expiries():
    class SlowCopyBot(FakePanelBot):
        def __init__(self):
            super().__init__()
            self.active_copies = 0
            self.maximum_active_copies = 0

        async def copy_message(self, **kwargs):
            self.active_copies += 1
            self.maximum_active_copies = max(self.maximum_active_copies, self.active_copies)
            try:
                await asyncio.sleep(0.02)
                return await super().copy_message(**kwargs)
            finally:
                self.active_copies -= 1

    catalog, users = await _repositories()
    await _register(users)
    await users.set_panel_dashboard_message(42, 50)
    record = await _seed_movie(catalog)
    bot = SlowCopyBot()
    panels = PanelManager(bot, users)

    await asyncio.gather(
        file_callback(
            FakeCallback(_user(), f"fl:{record.id}", 77),
            bot,
            catalog,
            users,
            _config(),
            panels,
        ),
        file_callback(
            FakeCallback(_user(), f"fl:{record.id}", 78),
            bot,
            catalog,
            users,
            _config(),
            panels,
        ),
    )

    assert bot.maximum_active_copies == 1
    profile = users.get_user(42)
    assert profile.panel_dashboard_message_id == 50
    assert len(profile.temporary_deliveries) == 2
    assert bot.events == [("copy", 100), ("send", 101), ("copy", 102), ("send", 103)]
    await panels.shutdown()


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


async def test_schema_v8_adds_empty_delivery_expiry_state_and_cleans_legacy_workspace():
    initial = UsersState(
        schema_version=6,
        users={
            "42": UserProfile(
                telegram_user_id=42,
                first_name="Alice",
                status=UserStatus.ACTIVE,
                panel_dashboard_message_id=50,
                panel_workspace_message_id=77,
                delivery_topic_id=899,
                delivery_topics={
                    "cat_movies": DeliveryTopicRef(
                        message_thread_id=900,
                        name="🎬 Movies",
                    )
                },
            )
        },
    ).model_dump(mode="json")
    initial["users"]["42"]["panel_workspace_is_receipt"] = True
    backend = MemorySnapshotBackend("users", initial)
    store = StateStore(backend, UsersState, UsersState)
    await store.initialize()
    users = UserRepository(store)

    assert await users.migrate_schema() is True
    migrated = users.get_user(42)
    assert users.snapshot().schema_version == 8
    assert migrated.temporary_deliveries == []
    assert migrated.panel_workspace_message_id == 77
    assert "panel_workspace_is_receipt" not in migrated.model_dump(mode="json")
    assert migrated.delivery_topic_id == 899
    assert migrated.delivery_topics["cat_movies"].message_thread_id == 900

    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    # Pre-release file messages are intentionally untracked and cannot be retroactively deleted.
    assert await panels.recover_temporary_deliveries() == 0
    assert bot.deleted == []
    assert await panels.cleanup_stale_workspaces() == 1
    assert users.get_user(42).panel_dashboard_message_id == 50
    assert users.get_user(42).panel_workspace_message_id is None
    assert bot.deleted == [(42, 77)]


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
    assert "Save or forward each file within its 5-minute window" in owner_text
    assert "this pinned dashboard stays in place" in user_text
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

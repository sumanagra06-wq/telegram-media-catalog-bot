from types import SimpleNamespace

import pytest
from aiogram.enums import ChatMemberStatus

from app.config import Config
from app.handlers.admin import (
    _validate_private_channel,
    confirm_manual_source_cleanup_execute,
    remove_title_execute,
    retry_source_deletions,
)
from app.handlers.channel import index_source_message
from app.handlers.watchlist import (
    WatchlistAddState,
    catalog_category_selected,
    catalog_status_selected,
    catalog_title_query,
    catalog_title_selected,
    manual_category_selected,
    manual_status_selected,
    manual_title_continue,
    manual_title_received,
    manual_title_toggle,
    shared_watchlist,
    update_entry_status,
)
from app.metadata import parse_metadata
from app.models import (
    CatalogState,
    MediaType,
    UserProfile,
    UsersState,
    UserStatus,
    WatchlistEntry,
    WatchStatus,
)
from app.repositories import CatalogRepository, UserRepository
from app.services import CatalogQueryService
from app.storage import MemorySnapshotBackend, StateStore, StorageError


class FakeState:
    def __init__(self):
        self.value = None
        self.data = {}

    async def set_state(self, value):
        self.value = value

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.value = None
        self.data = {}


class FakeScreen:
    def __init__(self):
        self.edits = []
        self.answers = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, user, data):
        self.from_user = user
        self.data = data
        self.message = FakeScreen()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class FakeMessage:
    def __init__(self, user, text):
        self.from_user = user
        self.text = text
        self.chat = SimpleNamespace(type="private")
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class PermissionBot:
    def __init__(self, can_delete_messages):
        self.can_delete_messages = can_delete_messages

    async def get_chat(self, channel_id):
        return SimpleNamespace(type="channel", username=None, title="Storage")

    async def get_me(self):
        return SimpleNamespace(id=123)

    async def get_chat_member(self, channel_id, user_id):
        return SimpleNamespace(
            status=ChatMemberStatus.ADMINISTRATOR,
            can_delete_messages=self.can_delete_messages,
        )


class DeleteBot:
    def __init__(self, fail_deletes=False):
        self.fail_deletes = fail_deletes
        self.deleted_batches = []
        self.deleted_single = []
        self.sent_messages = []

    async def delete_messages(self, chat_id, message_ids):
        if self.fail_deletes:
            raise RuntimeError("delete failed")
        self.deleted_batches.append((chat_id, list(message_ids)))
        return True

    async def delete_message(self, chat_id, message_id):
        if self.fail_deletes:
            raise RuntimeError("delete failed")
        self.deleted_single.append((chat_id, message_id))
        return True

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text, kwargs))


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


def _user(user_id, name):
    return SimpleNamespace(
        id=user_id,
        first_name=name,
        last_name=None,
        username=name.casefold(),
        language_code="en",
    )


async def _register(users, user):
    await users.ensure_user(
        user_id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
        language_code=user.language_code,
    )


async def _add_catalog_file(catalog, category_id, message_id, title):
    return await catalog.upsert_file(
        category_id=category_id,
        source_chat_id=-10010,
        source_message_id=message_id,
        telegram_file_id=f"file-{message_id}",
        telegram_file_unique_id=f"unique-{message_id}",
        media_type=MediaType.VIDEO,
        metadata=parse_metadata(title),
    )


async def test_manual_and_catalog_watchlist_panel_flows_and_public_read_only_view():
    catalog, users = await _repositories()
    category = await catalog.add_category("Movies", -10010, "Movies")
    _, content, _ = await _add_catalog_file(
        catalog, category.id, 1, "Interstellar 2014 1080p English mkv"
    )
    owner = _user(42, "Alice")
    viewer = _user(43, "Bob")
    await _register(users, owner)
    await _register(users, viewer)

    manual_state = FakeState()
    category_callback = FakeCallback(owner, f"wamc:{category.id}")
    await manual_category_selected(category_callback, users, catalog, _config(), manual_state)
    assert manual_state.value == WatchlistAddState.manual_title

    title_message = FakeMessage(owner, "Arrival")
    await manual_title_received(title_message, manual_state)
    assert manual_state.value == WatchlistAddState.manual_title
    continue_callback = FakeCallback(owner, "wct:continue")
    await manual_title_continue(continue_callback, manual_state)
    assert manual_state.value == WatchlistAddState.manual_status
    status_callback = FakeCallback(owner, "wams:c")
    await manual_status_selected(status_callback, users, catalog, _config(), manual_state)

    catalog_state = FakeState()
    catalog_category = FakeCallback(owner, f"wacc:{category.id}")
    await catalog_category_selected(catalog_category, users, catalog, _config(), catalog_state)
    query_message = FakeMessage(owner, "Interstellar")
    await catalog_title_query(query_message, CatalogQueryService(catalog), catalog, catalog_state)
    result_markup = query_message.answers[-1][1]["reply_markup"]
    select_data = result_markup.inline_keyboard[0][0].callback_data
    select_callback = FakeCallback(owner, select_data)
    await catalog_title_selected(select_callback, users, catalog, _config(), catalog_state)
    add_catalog_callback = FakeCallback(owner, f"wacs:{content.id}:h")
    await catalog_status_selected(add_catalog_callback, users, catalog, _config(), catalog_state)

    profile = users.get_user(owner.id)
    assert profile.watchlist_public is True
    assert {
        (entry.title, entry.status, entry.content_id) for entry in profile.watchlist.values()
    } == {
        ("Arrival", WatchStatus.COMPLETED, None),
        ("Interstellar", WatchStatus.ON_HOLD, content.id),
    }

    shared = FakeCallback(viewer, f"wlv:{owner.id}:0")
    await shared_watchlist(shared, users, _config())
    assert "Alice’s watchlist" in shared.message.edits[-1][0]
    assert len(users.get_user(owner.id).watchlist) == 2

    owner_entry = next(
        entry for entry in users.get_user(owner.id).watchlist.values() if entry.title == "Arrival"
    )
    forged_update = FakeCallback(viewer, f"wlu:{owner_entry.id}:t")
    await update_entry_status(forged_update, users, catalog, _config())
    assert forged_update.answers[-1][1]["show_alert"] is True
    unchanged = users.get_watchlist_entry(owner.id, owner_entry.id)
    assert unchanged.status == WatchStatus.COMPLETED

    with pytest.raises(ValueError, match="always public"):
        await users.set_watchlist_visibility(owner.id, False)
    public_attempt = FakeCallback(viewer, f"wlv:{owner.id}:0")
    await shared_watchlist(public_attempt, users, _config())
    assert "Alice’s watchlist" in public_attempt.message.edits[-1][0]
    assert users.get_user(owner.id).watchlist_public is True
    assert len(users.get_user(owner.id).watchlist) == 2


async def test_custom_watchlist_batch_preview_ticks_and_atomic_commit():
    catalog, users = await _repositories()
    category = await catalog.add_category("Movies", -10010, "Movies")
    user = _user(42, "Alice")
    await _register(users, user)
    state = FakeState()
    await manual_category_selected(
        FakeCallback(user, f"wamc:{category.id}"),
        users,
        catalog,
        _config(),
        state,
    )
    message = FakeMessage(user, "Arrival\nDune\n arrival \n\n")
    await manual_title_received(message, state)
    assert state.data["titles"] == ["Arrival", "Dune"]
    assert state.data["selected_indices"] == [0, 1]
    preview_buttons = [
        button for row in message.answers[-1][1]["reply_markup"].inline_keyboard for button in row
    ]
    assert any(button.text == "Continue with 2 ›" for button in preview_buttons)

    await manual_title_toggle(FakeCallback(user, "wctp:1"), state)
    assert state.data["selected_indices"] == [0]
    await manual_title_continue(FakeCallback(user, "wct:continue"), state)
    await manual_status_selected(
        FakeCallback(user, "wams:t"),
        users,
        catalog,
        _config(),
        state,
    )
    profile = users.get_user(user.id)
    assert {(entry.title, entry.status) for entry in profile.watchlist.values()} == {
        ("Arrival", WatchStatus.TO_WATCH)
    }

    too_many = FakeState()
    too_many.value = WatchlistAddState.manual_title
    oversized = FakeMessage(user, "\n".join(f"Title {index}" for index in range(26)))
    await manual_title_received(oversized, too_many)
    assert "no more than 25" in oversized.answers[-1][0]
    assert too_many.data == {}


async def test_new_storage_categories_require_delete_messages_permission():
    with pytest.raises(ValueError, match="Delete Messages"):
        await _validate_private_channel(PermissionBot(False), -10010, _config())
    assert await _validate_private_channel(PermissionBot(True), -10010, _config()) == "Storage"


async def test_permanent_catalog_removal_deletes_sources_and_blocks_reindexing():
    catalog, users = await _repositories()
    category = await catalog.add_category("Series", -10010, "Series")
    for episode in (1, 2):
        await _add_catalog_file(
            catalog,
            category.id,
            episode,
            f"Dark S01E{episode:02d} 1080p English mkv",
        )
    content = next(iter(catalog.snapshot().contents.values()))
    watcher = _user(42, "Alice")
    await _register(users, watcher)
    await users.upsert_watchlist_entry(
        user_id=watcher.id,
        content_id=content.id,
        title=content.title,
        category_id=category.id,
        category_name=category.name,
        status=WatchStatus.TO_WATCH,
    )
    callback = FakeCallback(_user(999, "Owner"), f"adrx:{content.id}")
    bot = DeleteBot()

    await remove_title_execute(callback, bot, catalog, _config())

    state = catalog.snapshot()
    assert state.contents == {}
    assert state.files == {}
    assert state.source_lookup == {}
    assert len(state.removed_sources) == 2
    assert all(item.telegram_deleted for item in state.removed_sources.values())
    assert bot.deleted_batches == [(-10010, [1, 2])]
    assert "Catalog files removed: 2" in callback.message.edits[-1][0]
    preserved_entry = next(iter(users.get_user(watcher.id).watchlist.values()))
    assert preserved_entry.title == "Dark"
    assert preserved_entry.content_id == content.id

    old_post = SimpleNamespace(
        chat=SimpleNamespace(id=-10010, title="Series"),
        message_id=1,
        caption="Dark S01E01 1080p English mkv",
        video=SimpleNamespace(file_id="new", file_unique_id="new-unique", file_name=None),
        document=None,
    )
    assert await index_source_message(old_post, bot, _config(), catalog) is False
    assert catalog.snapshot().contents == {}


async def test_failed_telegram_delete_stays_blocked_and_can_be_retried():
    catalog, _ = await _repositories()
    category = await catalog.add_category("Movies", -10010, "Movies")
    _, content, _ = await _add_catalog_file(catalog, category.id, 7, "Dune 2021 1080p English mkv")
    callback = FakeCallback(_user(999, "Owner"), f"adrx:{content.id}")
    failing_bot = DeleteBot(fail_deletes=True)

    await remove_title_execute(callback, failing_bot, catalog, _config())
    assert catalog.get_content(content.id) is None
    assert len(catalog.pending_removed_sources()) == 1
    assert catalog.is_source_removed(-10010, 7) is True

    retry_callback = FakeCallback(_user(999, "Owner"), "adp:retry")
    working_bot = DeleteBot()
    await retry_source_deletions(retry_callback, working_bot, catalog)
    assert catalog.pending_removed_sources() == []
    assert working_bot.deleted_batches == [(-10010, [7])]


async def test_owner_can_confirm_old_source_post_was_deleted_manually():
    catalog, _ = await _repositories()
    category = await catalog.add_category("Movies", -10010, "Movies")
    _, content, _ = await _add_catalog_file(
        catalog, category.id, 8, "The Matrix 1999 1080p English mkv"
    )
    await catalog.remove_content(content.id, actor_id=999)
    assert len(catalog.pending_removed_sources()) == 1

    callback = FakeCallback(_user(999, "Owner"), "adp:clearc")
    await confirm_manual_source_cleanup_execute(callback, catalog)

    assert catalog.pending_removed_sources() == []
    actions = [event.action for event in catalog.snapshot().audit_events]
    assert "content.sources_manual_confirmation" in actions


async def test_schema_migration_rekeys_legacy_watchlist_entries_once():
    legacy_entry = WatchlistEntry(
        content_id="c_legacy",
        title="Legacy title",
        category_id="cat_legacy",
        category_name="Movies",
        status=WatchStatus.TO_WATCH,
    )
    initial = UsersState(
        schema_version=1,
        users={
            "42": UserProfile(
                telegram_user_id=42,
                first_name="Legacy",
                status=UserStatus.ACTIVE,
                watchlist_public=False,
                watchlist={"c_legacy": legacy_entry},
            )
        },
    )
    backend = MemorySnapshotBackend("users", initial.model_dump(mode="json"))
    store = StateStore(backend, UsersState, UsersState)
    await store.initialize()
    users = UserRepository(store)

    assert await users.migrate_schema() is True
    migrated = users.get_user(42)
    assert migrated is not None
    assert migrated.watchlist_public is True
    assert migrated.watchlist_display_name is None
    assert migrated.panel_dashboard_message_id is None
    assert migrated.panel_workspace_message_id is None
    assert migrated.delivery_topic_id is None
    assert users.snapshot().schema_version == 5
    assert len(migrated.watchlist) == 1
    entry_id, entry = next(iter(migrated.watchlist.items()))
    assert entry_id == entry.id
    assert entry_id.startswith("w_")
    assert entry.content_id == "c_legacy"
    revision = users.snapshot().revision
    assert await users.migrate_schema() is False
    assert users.snapshot().revision == revision


async def test_failed_catalog_removal_commit_keeps_title_and_sources_intact():
    backend = MemorySnapshotBackend("catalog")
    store = StateStore(backend, CatalogState, CatalogState)
    await store.initialize()
    catalog = CatalogRepository(store)
    category = await catalog.add_category("Movies", -10010, "Movies")
    _, content, _ = await _add_catalog_file(
        catalog, category.id, 9, "Arrival 2016 1080p English mkv"
    )
    backend.fail_next_commit = True

    with pytest.raises(StorageError):
        await catalog.remove_content(content.id, actor_id=999)

    state = catalog.snapshot()
    assert content.id in state.contents
    assert len(state.files) == 1
    assert state.removed_sources == {}
    assert state.source_lookup["-10010:9"] in state.files

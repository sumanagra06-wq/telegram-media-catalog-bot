import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from app.handlers.panel import (
    panel_add_selected,
    panel_admin_callback,
    panel_browse_callback,
    panel_browse_category_callback,
    panel_bulk_status_selected,
    panel_recent_callback,
    panel_toggle_selection,
)
from app.handlers.search import file_callback, plain_title_search
from app.metadata import parse_metadata
from app.models import CatalogState, MediaType, UsersState, WatchStatus
from app.panels import PanelManager
from app.repositories import CatalogRepository, UserRepository
from app.services import CatalogQueryService, SearchSessionStore
from app.storage import MemorySnapshotBackend, StateStore, StorageError
from app.ui import panel_dashboard, selectable_results


class FakePanelBot:
    def __init__(self):
        self.next_message_id = 100
        self.sent = []
        self.edited = []
        self.pinned = []
        self.deleted = []
        self.copied = []
        self.events = []
        self.unavailable_edits = set()

    async def send_message(self, chat_id, text, **kwargs):
        message = SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        self.sent.append((chat_id, text, kwargs, message.message_id))
        self.events.append(("send", message.message_id))
        return message

    async def copy_message(self, **kwargs):
        message = SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        self.copied.append(kwargs)
        self.events.append(("copy", message.message_id))
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


async def test_successful_delivery_moves_dashboard_controls_below_the_file():
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
    assert users.snapshot().revision == revision_before_delivery + 1
    assert bot.events == [("send", 100), ("copy", 101), ("send", 102)]
    assert profile.panel_dashboard_message_id == 50
    assert profile.panel_workspace_message_id == 102
    assert (42, 100) in bot.deleted
    assert "MEDIA LIBRARY DASHBOARD" in bot.sent[-1][1]
    assert "file is above" in bot.sent[-1][1]
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


async def test_selectable_results_keep_checks_across_pages_and_owner_dashboard_is_unified():
    catalog, _ = await _repositories()
    category = await catalog.add_category("Movies", -10010, "Movies")
    contents = []
    for index in range(5):
        _, content, _ = await catalog.upsert_file(
            category_id=category.id,
            source_chat_id=-10010,
            source_message_id=index + 1,
            telegram_file_id=f"file-{index}",
            telegram_file_unique_id=f"unique-{index}",
            media_type=MediaType.DOCUMENT,
            metadata=parse_metadata(f"Film {index} 202{index} 1080p English mkv"),
        )
        contents.append(content)
    session = SearchSessionStore().create(
        42,
        "Film",
        [content.id for content in contents],
        selectable=True,
    )
    session.selected_content_ids.update({contents[0].id, contents[4].id})

    _, first_markup = selectable_results(session, contents, 0)
    _, second_markup = selectable_results(session, contents, 1)
    first_buttons = [button for row in first_markup.inline_keyboard for button in row]
    second_buttons = [button for row in second_markup.inline_keyboard for button in row]
    assert sum(button.text == "✅" for button in first_buttons) == 1
    assert sum(button.text == "✅" for button in second_buttons) == 1
    assert any(button.text == "➕ Add Selected · 2" for button in first_buttons)
    assert all(
        button.callback_data is None or len(button.callback_data.encode()) <= 64
        for button in first_buttons + second_buttons
    )

    _, owner_markup = panel_dashboard(True)
    _, user_markup = panel_dashboard(False)
    owner_callbacks = {
        button.callback_data for row in owner_markup.inline_keyboard for button in row
    }
    user_callbacks = {button.callback_data for row in user_markup.inline_keyboard for button in row}
    assert "p:admin" in owner_callbacks
    assert "p:admin" not in user_callbacks
    assert {"p:search", "p:browse", "p:recent", "p:watchlist"} <= owner_callbacks


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


async def test_browse_and_recent_flows_render_selectable_results_in_one_workspace():
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
    assert "BROWSE & MULTI-SELECT" in bot.sent[-1][1]

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
    assert any(button.text == "☐" for button in browse_buttons)
    assert any(button.callback_data.startswith("px:") for button in browse_buttons)

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


async def test_checkbox_callbacks_select_and_bulk_add_in_the_same_workspace():
    catalog, users = await _repositories()
    await _register(users)
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
    sessions = SearchSessionStore()
    session = sessions.create(42, "Arrival", [content.id], selectable=True)
    bot = FakePanelBot()
    panels = PanelManager(bot, users)
    _, markup = panel_dashboard(False)
    workspace_id = await panels.render_workspace(
        user_id=42,
        text="Results",
        reply_markup=markup,
    )
    user = SimpleNamespace(
        id=42,
        first_name="Alice",
        last_name=None,
        username="alice",
        language_code="en",
    )

    toggle = FakeCallback(user, f"px:{session.token}:{content.id}:0", workspace_id)
    await panel_toggle_selection(
        toggle,
        sessions,
        catalog,
        users,
        _config(),
        panels,
    )
    assert session.selected_content_ids == {content.id}
    assert "Selected: <b>1/25</b>" in bot.edited[-1][2]

    add_selected = FakeCallback(user, f"pa:{session.token}:0", workspace_id)
    await panel_add_selected(add_selected, sessions, users, _config(), panels)
    assert "ADD SELECTED TITLES" in bot.edited[-1][2]

    choose_status = FakeCallback(user, f"pw:{session.token}:t:0", workspace_id)
    await panel_bulk_status_selected(
        choose_status,
        sessions,
        catalog,
        users,
        _config(),
        panels,
    )
    profile = users.get_user(42)
    assert len(profile.watchlist) == 1
    assert next(iter(profile.watchlist.values())).status == WatchStatus.TO_WATCH
    assert "WATCHLIST UPDATED" in bot.edited[-1][2]
    assert users.get_user(42).panel_workspace_message_id == workspace_id
    assert len(bot.sent) == 1
    await panels.shutdown()


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
    assert "Selected" in bot.edited[-1][2]
    result_buttons = [
        button for row in bot.edited[-1][3]["reply_markup"].inline_keyboard for button in row
    ]
    assert any(button.text == "☐" for button in result_buttons)
    assert len(bot.sent) == 1
    await panels.shutdown()

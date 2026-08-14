from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import CopyMessage, SendMessage

from app.config import Config
from app.filters import OwnerFilter
from app.handlers import admin as admin_handlers
from app.handlers.admin import (
    AdminState,
    _copy_broadcast_message,
    broadcast_input,
    broadcast_send,
)
from app.models import CatalogState, UsersState, UserStatus
from app.panels import PanelManager
from app.repositories import CatalogRepository, UserRepository
from app.storage import MemorySnapshotBackend, StateStore


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
    def __init__(self, message_id=500):
        self.message_id = message_id
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeCallback:
    def __init__(self):
        self.from_user = _user(999, "Owner")
        self.data = "ab:send"
        self.message = FakeScreen()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class FakeInputMessage:
    def __init__(self, *, message_id, text=None, caption=None, reply_to_message=None):
        self.message_id = message_id
        self.chat = SimpleNamespace(id=999, type="private")
        self.from_user = _user(999, "Owner")
        self.text = text
        self.caption = caption
        self.photo = None
        self.video = None
        self.document = None
        self.reply_to_message = reply_to_message
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class BroadcastBot:
    def __init__(self, *, copy_fail_ids=(), dashboard_fail_ids=()):
        self.next_message_id = 100
        self.copy_fail_ids = set(copy_fail_ids)
        self.dashboard_fail_ids = set(dashboard_fail_ids)
        self.copied = []
        self.sent = []
        self.pinned = []
        self.unpinned = []
        self.deleted = []

    async def copy_message(self, **kwargs):
        self.copied.append(kwargs)
        if kwargs["chat_id"] in self.copy_fail_ids:
            raise TelegramForbiddenError(
                CopyMessage(**kwargs),
                "recipient blocked the bot",
            )
        message = SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        return message

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        if chat_id in self.dashboard_fail_ids:
            raise TelegramForbiddenError(
                SendMessage(chat_id=chat_id, text=text, **kwargs),
                "recipient blocked the bot",
            )
        message = SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        return message

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self.pinned.append((chat_id, message_id, kwargs))
        return True

    async def unpin_chat_message(self, chat_id, message_id):
        self.unpinned.append((chat_id, message_id))
        return True

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True


def _user(user_id, name):
    return SimpleNamespace(
        id=user_id,
        first_name=name,
        last_name=None,
        username=name.casefold(),
        language_code="en",
    )


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


async def _repositories():
    catalog_store = StateStore(MemorySnapshotBackend("catalog"), CatalogState, CatalogState)
    users_store = StateStore(MemorySnapshotBackend("users"), UsersState, UsersState)
    await catalog_store.initialize()
    await users_store.initialize()
    return CatalogRepository(catalog_store), UserRepository(users_store)


async def _register(users, user_id, name):
    user = _user(user_id, name)
    await users.ensure_user(
        user_id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
        language_code=user.language_code,
        is_owner=user_id == 999,
    )


async def test_broadcast_input_can_select_a_replied_to_media_message():
    _, users = await _repositories()
    await _register(users, 999, "Owner")
    source = FakeInputMessage(message_id=70, caption="Scheduled maintenance")
    source.photo = [SimpleNamespace(file_id="photo")]
    trigger = FakeInputMessage(message_id=71, text="send this", reply_to_message=source)
    state = FakeState()

    await broadcast_input(trigger, state, users)

    assert state.value == AdminState.broadcast_confirm
    assert state.data["broadcast_source_message_id"] == 70
    assert state.data["broadcast_cleanup_message_id"] == 71
    assert "Photo" in trigger.answers[-1][0]
    assert "Scheduled maintenance" in trigger.answers[-1][0]


async def test_owner_broadcast_reaches_every_status_and_atomically_refreshes_dashboards():
    catalog, users = await _repositories()
    for user_id, name in (
        (999, "Owner"),
        (42, "Pending"),
        (43, "Banned"),
        (44, "Suspended"),
    ):
        await _register(users, user_id, name)
    await users.set_user_status(42, UserStatus.PENDING)
    await users.set_user_status(43, UserStatus.BANNED)
    await users.set_user_status(44, UserStatus.SUSPENDED)
    await users.set_panel_dashboard_message(999, 10)
    await users.set_panel_dashboard_message(42, 20)
    await users.set_panel_dashboard_message(43, 30)
    await users.set_panel_dashboard_message(44, 40)
    state = FakeState()
    state.value = AdminState.broadcast_confirm
    state.data = {
        "broadcast_source_chat_id": 999,
        "broadcast_source_message_id": 77,
        "broadcast_cleanup_chat_id": 999,
        "broadcast_cleanup_message_id": 78,
    }
    callback = FakeCallback()
    bot = BroadcastBot()
    panels = PanelManager(bot, users)
    users_revision = users.snapshot().revision

    await broadcast_send(
        callback,
        bot,
        state,
        users,
        catalog,
        _config(),
        panels,
    )

    assert [item["chat_id"] for item in bot.copied] == [42, 43, 44, 999]
    assert all(item["reply_markup"] is None for item in bot.copied)
    assert users.snapshot().revision == users_revision + 1
    assert users.get_user(42).panel_dashboard_message_id == 104
    assert users.get_user(43).panel_dashboard_message_id == 105
    assert users.get_user(44).panel_dashboard_message_id == 106
    assert users.get_user(999).panel_dashboard_message_id == 107
    assert bot.unpinned == [(42, 20), (43, 30), (44, 40), (999, 10)]
    assert (999, 78) in bot.deleted
    assert "Messages delivered</b>  •  4" in callback.message.edits[-1][0]
    assert "Dashboards refreshed</b>  •  4" in callback.message.edits[-1][0]
    assert state.data == {}
    assert catalog.recent_audit(1)[0].action == "broadcast.send"


async def test_broadcast_routes_are_owner_only():
    owner_filter = OwnerFilter()
    event = SimpleNamespace(from_user=_user(999, "Owner"))
    stranger = SimpleNamespace(from_user=_user(42, "User"))

    assert await owner_filter(event, _config()) is True
    assert await owner_filter(stranger, _config()) is False


async def test_broadcast_copy_retries_one_flood_wait(monkeypatch):
    class RetryBot:
        def __init__(self):
            self.attempts = 0

        async def copy_message(self, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise TelegramRetryAfter(
                    CopyMessage(**kwargs),
                    "flood control",
                    retry_after=1,
                )
            return SimpleNamespace(message_id=80)

    bot = RetryBot()

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.handlers.admin.asyncio.sleep", no_sleep)
    copied = await _copy_broadcast_message(
        bot,
        user_id=42,
        source_chat_id=999,
        source_message_id=77,
    )

    assert copied is True
    assert bot.attempts == 2


async def test_broadcast_reports_copy_and_dashboard_partial_failures():
    catalog, users = await _repositories()
    for user_id, name in ((999, "Owner"), (42, "Pending"), (43, "Banned")):
        await _register(users, user_id, name)
    await users.set_user_status(42, UserStatus.PENDING)
    await users.set_user_status(43, UserStatus.BANNED)
    await users.set_panel_dashboard_message(999, 10)
    await users.set_panel_dashboard_message(42, 20)
    await users.set_panel_dashboard_message(43, 30)
    state = FakeState()
    state.value = AdminState.broadcast_confirm
    state.data = {
        "broadcast_source_chat_id": 999,
        "broadcast_source_message_id": 77,
        "broadcast_cleanup_chat_id": 999,
        "broadcast_cleanup_message_id": 78,
    }
    callback = FakeCallback()
    bot = BroadcastBot(copy_fail_ids={43}, dashboard_fail_ids={42})
    panels = PanelManager(bot, users)
    users_revision = users.snapshot().revision

    await broadcast_send(
        callback,
        bot,
        state,
        users,
        catalog,
        _config(),
        panels,
    )

    assert [item["chat_id"] for item in bot.copied] == [42, 43, 999]
    assert users.snapshot().revision == users_revision + 1
    assert users.get_user(42).panel_dashboard_message_id == 20
    assert users.get_user(43).panel_dashboard_message_id == 30
    assert users.get_user(999).panel_dashboard_message_id == 102
    report = callback.message.edits[-1][0]
    assert "Messages delivered</b>  •  2" in report
    assert "Message failures</b>  •  1" in report
    assert "Dashboards refreshed</b>  •  1" in report
    assert "Dashboard failures</b>  •  1" in report
    assert "sent=2 failed=1 dashboards=1 dashboard_failed=1" in catalog.recent_audit(1)[0].details


async def test_duplicate_broadcast_confirmation_is_rejected_while_running():
    catalog, users = await _repositories()
    await _register(users, 999, "Owner")
    state = FakeState()
    state.value = AdminState.broadcast_confirm
    state.data = {
        "broadcast_source_chat_id": 999,
        "broadcast_source_message_id": 77,
    }
    callback = FakeCallback()
    bot = BroadcastBot()
    admin_handlers._BROADCASTS_IN_PROGRESS.add(999)
    try:
        await broadcast_send(
            callback,
            bot,
            state,
            users,
            catalog,
            _config(),
            PanelManager(bot, users),
        )
    finally:
        admin_handlers._BROADCASTS_IN_PROGRESS.discard(999)

    assert bot.copied == []
    assert state.value == AdminState.broadcast_confirm
    assert callback.answers[-1] == ("A broadcast is already in progress.", {"show_alert": True})


async def test_broadcast_still_reports_when_audit_snapshot_commit_fails():
    catalog, users = await _repositories()
    await _register(users, 999, "Owner")
    await users.set_panel_dashboard_message(999, 10)
    state = FakeState()
    state.value = AdminState.broadcast_confirm
    state.data = {
        "broadcast_source_chat_id": 999,
        "broadcast_source_message_id": 77,
    }
    callback = FakeCallback()
    bot = BroadcastBot()
    catalog.store.backend.fail_next_commit = True

    await broadcast_send(
        callback,
        bot,
        state,
        users,
        catalog,
        _config(),
        PanelManager(bot, users),
    )

    report = callback.message.edits[-1][0]
    assert "Messages delivered</b>  •  1" in report
    assert "Audit saved</b>  •  No" in report
    assert catalog.recent_audit(1) == []
    assert admin_handlers._BROADCASTS_IN_PROGRESS == set()

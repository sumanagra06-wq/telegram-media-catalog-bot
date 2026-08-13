from types import SimpleNamespace

from app.commands import OWNER_COMMANDS, USER_COMMANDS
from app.handlers.admin import (
    AdminState,
    user_community_name_input,
    user_community_name_reset,
    user_community_name_start,
)
from app.models import CatalogState, UsersState
from app.repositories import CatalogRepository, UserRepository
from app.storage import MemorySnapshotBackend, StateStore
from app.ui import user_detail


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

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


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
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


async def _repositories():
    catalog_store = StateStore(MemorySnapshotBackend("catalog"), CatalogState, CatalogState)
    users_store = StateStore(MemorySnapshotBackend("users"), UsersState, UsersState)
    await catalog_store.initialize()
    await users_store.initialize()
    return CatalogRepository(catalog_store), UserRepository(users_store)


async def test_admin_can_edit_and_reset_another_users_public_community_name():
    catalog, users = await _repositories()
    await users.ensure_user(
        user_id=42,
        first_name="Alice",
        last_name=None,
        username="alice",
        language_code="en",
    )
    owner = SimpleNamespace(id=999, first_name="Owner")
    state = FakeState()

    start = FakeCallback(owner, "aucn:42")
    await user_community_name_start(start, users, state)
    assert state.value == AdminState.user_community_name
    assert state.data == {"target_user_id": 42}
    assert "owner moderation" in start.message.edits[-1][0]

    message = FakeMessage(owner, "Alice's Cinema Club")
    await user_community_name_input(message, state, users, catalog)
    profile = users.get_user(42)
    assert profile.watchlist_display_name == "Alice's Cinema Club"
    assert state.value is None
    assert "Alice's Cinema Club" in message.answers[-1][0]
    assert catalog.recent_audit(1)[0].action == "user.community_name"

    detail_text, detail_markup = user_detail(profile)
    assert "Alice's Cinema Club" in detail_text
    assert any(
        button.callback_data == "aucn:42" for row in detail_markup.inline_keyboard for button in row
    )

    reset = FakeCallback(owner, "aucnr:42")
    await user_community_name_reset(reset, users, catalog, state)
    assert users.get_user(42).watchlist_display_name is None
    assert "Community name reset" in reset.answers[-1][0]
    assert "Community name</b>  •  Alice" in reset.message.edits[-1][0]


async def test_native_command_menu_is_dashboard_only():
    assert [(command.command, command.description) for command in USER_COMMANDS] == [
        ("dashboard", "Emergency dashboard repost")
    ]
    assert OWNER_COMMANDS == USER_COMMANDS

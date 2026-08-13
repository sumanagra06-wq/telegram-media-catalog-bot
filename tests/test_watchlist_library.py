from types import SimpleNamespace

import pytest

from app.config import Config
from app.handlers.watchlist import (
    community_name_received,
    shared_watchlist,
    watchlist_library_add_selected,
    watchlist_library_alphabet,
    watchlist_library_alphabet_selected,
    watchlist_library_category_selected,
    watchlist_library_status_selected,
    watchlist_library_toggle,
    watchlist_visibility,
)
from app.metadata import parse_metadata
from app.models import CatalogState, MediaType, UsersState, WatchStatus
from app.repositories import CatalogRepository, UserRepository
from app.services import CatalogQueryService, SearchSessionStore
from app.storage import MemorySnapshotBackend, StateStore
from app.ui import public_watchlist_directory


class FakeState:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


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


def _user(user_id, first_name):
    return SimpleNamespace(
        id=user_id,
        first_name=first_name,
        last_name=None,
        username=first_name.casefold(),
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


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


async def test_watchlist_library_supports_pages_alphabet_ticks_and_bulk_status():
    catalog, users = await _repositories()
    user = _user(42, "Alice")
    await _register(users, user)
    category = await catalog.add_category("Movies", -10010, "Movies")
    titles = [
        "Aardvark",
        "Alien",
        "Amelie",
        "Apollo",
        "Aquaman",
        "Arrival",
        "Avatar",
        "Batman",
        "Blade",
        "Casablanca",
        "Dune",
        "3 Idiots",
    ]
    contents = {}
    for index, title in enumerate(titles, start=1):
        _, content, _ = await catalog.upsert_file(
            category_id=category.id,
            source_chat_id=-10010,
            source_message_id=index,
            telegram_file_id=f"file-{index}",
            telegram_file_unique_id=f"unique-{index}",
            media_type=MediaType.VIDEO,
            metadata=parse_metadata(f"{title} 2020 1080p English mkv"),
        )
        contents[content.title] = content

    await users.upsert_watchlist_entry(
        user_id=user.id,
        content_id=contents["Arrival"].id,
        title="Arrival",
        year=2020,
        category_id=category.id,
        category_name=category.name,
        status=WatchStatus.TO_WATCH,
    )
    query = CatalogQueryService(catalog)
    sessions = SearchSessionStore()
    choose_category = FakeCallback(user, f"wlbc:{category.id}")
    await watchlist_library_category_selected(
        choose_category,
        users,
        catalog,
        query,
        sessions,
        _config(),
    )
    result_text, result_kwargs = choose_category.message.edits[-1]
    result_buttons = _buttons(result_kwargs["reply_markup"])
    first_toggle = next(
        button for button in result_buttons if button.callback_data.startswith("wlbt:")
    )
    token = first_toggle.callback_data.split(":", 2)[1]
    assert "alphabetical catalog" in result_text
    assert any(button.callback_data == f"wlbp:{token}:1" for button in result_buttons)
    visible_title_labels = [
        button.text
        for button in result_buttons
        if button.callback_data.startswith("wlbt:") and button.text not in {"☐", "✅"}
    ]
    plain_labels = [label.removeprefix("📚 ") for label in visible_title_labels]
    assert plain_labels == sorted(plain_labels, key=str.casefold)

    alphabet = FakeCallback(user, f"wlba:{token}:0")
    await watchlist_library_alphabet(
        alphabet,
        users,
        catalog,
        sessions,
        _config(),
    )
    alphabet_callbacks = {
        button.callback_data for button in _buttons(alphabet.message.edits[-1][1]["reply_markup"])
    }
    assert {f"wlaf:{token}:A", f"wlaf:{token}:B", f"wlaf:{token}:0"} <= alphabet_callbacks

    choose_b = FakeCallback(user, f"wlaf:{token}:B")
    await watchlist_library_alphabet_selected(
        choose_b,
        users,
        catalog,
        sessions,
        _config(),
    )
    assert "Alphabet: <b>B</b>" in choose_b.message.edits[-1][0]
    b_buttons = _buttons(choose_b.message.edits[-1][1]["reply_markup"])
    b_labels = [button.text for button in b_buttons if button.text not in {"☐", "✅"}]
    assert any("Batman" in label for label in b_labels)
    assert any("Blade" in label for label in b_labels)
    assert not any("Arrival" in label for label in b_labels)

    select_blade = FakeCallback(user, f"wlbt:{token}:{contents['Blade'].id}:0")
    await watchlist_library_toggle(
        select_blade,
        users,
        catalog,
        sessions,
        _config(),
    )
    choose_a = FakeCallback(user, f"wlaf:{token}:A")
    await watchlist_library_alphabet_selected(
        choose_a,
        users,
        catalog,
        sessions,
        _config(),
    )
    a_labels = [
        button.text
        for button in _buttons(choose_a.message.edits[-1][1]["reply_markup"])
        if button.callback_data.startswith("wlbt:") and button.text not in {"☐", "✅"}
    ]
    assert any(label.startswith("📚 Arrival") for label in a_labels)
    select_arrival = FakeCallback(user, f"wlbt:{token}:{contents['Arrival'].id}:0")
    await watchlist_library_toggle(
        select_arrival,
        users,
        catalog,
        sessions,
        _config(),
    )
    assert "Selected: <b>2/25</b>" in select_arrival.message.edits[-1][0]

    add_selected = FakeCallback(user, f"wlbd:{token}:0")
    await watchlist_library_add_selected(
        add_selected,
        users,
        sessions,
        _config(),
    )
    status_callbacks = {
        button.callback_data
        for button in _buttons(add_selected.message.edits[-1][1]["reply_markup"])
    }
    assert {
        f"wlbs:{token}:t:0",
        f"wlbs:{token}:h:0",
        f"wlbs:{token}:c:0",
    } <= status_callbacks

    choose_status = FakeCallback(user, f"wlbs:{token}:h:0")
    await watchlist_library_status_selected(
        choose_status,
        users,
        catalog,
        sessions,
        _config(),
    )
    profile = users.get_user(user.id)
    assert {entry.title for entry in profile.watchlist.values()} == {"Arrival", "Blade"}
    assert {entry.status for entry in profile.watchlist.values()} == {WatchStatus.ON_HOLD}
    assert "MY WATCHLIST" in choose_status.message.edits[-1][0]


async def test_community_name_is_editable_but_watchlists_cannot_be_private():
    _, users = await _repositories()
    owner = _user(42, "Alice")
    viewer = _user(43, "Bob")
    await _register(users, owner)
    await _register(users, viewer)
    state = FakeState()
    message = FakeMessage(owner, "A & B Cinema Club")

    await community_name_received(message, users, state)

    profile = users.get_user(owner.id)
    assert state.cleared is True
    assert profile.watchlist_display_name == "A & B Cinema Club"
    directory_text, directory_markup = public_watchlist_directory([profile], 0)
    assert "Public lists: <b>1</b>" in directory_text
    assert "A & B Cinema Club" in _buttons(directory_markup)[0].text

    shared = FakeCallback(viewer, f"wlv:{owner.id}:0")
    await shared_watchlist(shared, users, _config())
    assert "A &amp; B Cinema Club’s watchlist" in shared.message.edits[-1][0]

    old_private_button = FakeCallback(owner, "wlvis:0")
    await watchlist_visibility(old_private_button, users, _config())
    assert old_private_button.answers[-1][1]["show_alert"] is True
    assert "always public" in old_private_button.answers[-1][0]
    assert users.get_user(owner.id).watchlist_public is True

    with pytest.raises(ValueError, match="40 characters"):
        await users.set_watchlist_display_name(owner.id, "x" * 41)

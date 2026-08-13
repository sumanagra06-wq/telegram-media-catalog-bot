from app.metadata import parse_metadata
from app.models import (
    CatalogState,
    Category,
    MediaType,
    UserProfile,
    UserStatus,
    WatchlistEntry,
    WatchStatus,
)
from app.repositories import CatalogRepository
from app.services import CatalogQueryService, SearchSessionStore
from app.storage import MemorySnapshotBackend, StateStore
from app.ui import (
    content_screen,
    public_watchlist_directory,
    search_results,
    season_screen,
    watchlist_add_method,
    watchlist_category_picker,
    watchlist_custom_batch_preview,
    watchlist_entries,
    watchlist_entry_detail,
    watchlist_home,
    watchlist_status_picker,
)


def _callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


async def test_core_inline_callback_data_is_valid_and_statuses_are_exact():
    store = StateStore(MemorySnapshotBackend("catalog"), CatalogState, CatalogState)
    await store.initialize()
    catalog = CatalogRepository(store)
    category = await catalog.add_category("Series", -1001, "Series")
    content = None
    for episode in range(1, 26):
        _, content, _ = await catalog.upsert_file(
            category_id=category.id,
            source_chat_id=-1001,
            source_message_id=episode,
            telegram_file_id=f"file-{episode}",
            telegram_file_unique_id=f"unique-{episode}",
            media_type=MediaType.VIDEO,
            metadata=parse_metadata(
                f"Maamla Legal Hai S02E{episode:02d} 1080p Hindi WEB DL x264 mkv"
            ),
        )

    query = CatalogQueryService(catalog)
    session = SearchSessionStore().create(42, "Maamla", [content.id])
    _, search_markup = search_results(session, [content], 0)
    _, content_markup = content_screen(
        content=content,
        category=category,
        query=query,
        back_token=session.token,
    )
    _, season_markup = season_screen(content, 2, query, session.token, 0)
    _, status_markup = watchlist_status_picker(content.title, "wacs:test")

    callbacks = (
        _callbacks(search_markup)
        + _callbacks(content_markup)
        + _callbacks(season_markup)
        + _callbacks(status_markup)
    )
    assert callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)

    labels = [button.text for row in status_markup.inline_keyboard for button in row]
    assert any("To Watch" in label for label in labels)
    assert any("On Hold" in label for label in labels)
    assert any("Completed" in label for label in labels)
    assert not any("Watching" in label or "Dropped" in label for label in labels)


def test_watchlist_panel_callbacks_are_bounded_and_shared_details_are_read_only():
    category = Category(
        id="cat_movies",
        name="Movies",
        slug="movies",
        active_channel_id=-1001,
    )
    entry = WatchlistEntry(
        id="w_entry123",
        content_id="c_content123",
        title="Interstellar",
        category_id=category.id,
        category_name=category.name,
        status=WatchStatus.TO_WATCH,
    )
    owner = UserProfile(
        telegram_user_id=123456789,
        first_name="Alice",
        username="alice",
        status=UserStatus.ACTIVE,
        watchlist={entry.id: entry},
    )
    markups = [
        watchlist_home(owner)[1],
        watchlist_add_method()[1],
        watchlist_category_picker([category], "wamc", "Manual title")[1],
        watchlist_status_picker(entry.title, "wacs:c_content123")[1],
        watchlist_entries(owner, [entry], 0, own=True)[1],
        watchlist_entries(owner, [entry], 0, own=False)[1],
        watchlist_entry_detail(entry, owner, own=True, content_available=True)[1],
        watchlist_entry_detail(entry, owner, own=False, content_available=True)[1],
        public_watchlist_directory([owner], 0)[1],
    ]
    callbacks = [value for markup in markups for value in _callbacks(markup)]
    assert callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)

    shared_markup = watchlist_entry_detail(entry, owner, own=False, content_available=True)[1]
    shared_callbacks = _callbacks(shared_markup)
    assert not any(value.startswith(("wlu:", "wld:")) for value in shared_callbacks)


def test_custom_batch_preview_stays_within_telegram_text_button_and_callback_limits():
    titles = [f"Title {index} <&> " + ("x" * 140) for index in range(25)]
    text, markup = watchlist_custom_batch_preview(titles, set(range(25)))
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert len(text) <= 4096
    assert "&lt;&amp;&gt;" in text
    assert all(len(button.text) <= 64 for button in buttons)
    assert all(
        button.callback_data is None or len(button.callback_data.encode("utf-8")) <= 64
        for button in buttons
    )

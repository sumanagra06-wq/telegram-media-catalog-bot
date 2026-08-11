from app.metadata import parse_metadata
from app.models import CatalogState, MediaType, WatchStatus
from app.repositories import CatalogRepository
from app.services import CatalogQueryService, SearchSessionStore
from app.storage import MemorySnapshotBackend, StateStore
from app.ui import content_screen, search_results, season_screen


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
        watch_status=WatchStatus.ON_HOLD,
        back_token=session.token,
    )
    _, season_markup = season_screen(content, 2, query, session.token, 0)

    callbacks = _callbacks(search_markup) + _callbacks(content_markup) + _callbacks(season_markup)
    assert callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)

    labels = [button.text for row in content_markup.inline_keyboard for button in row]
    assert any("To Watch" in label for label in labels)
    assert any("On Hold" in label for label in labels)
    assert any("Completed" in label for label in labels)
    assert not any("Watching" in label or "Dropped" in label for label in labels)

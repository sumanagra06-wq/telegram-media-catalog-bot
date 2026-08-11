import pytest

from app.metadata import parse_metadata
from app.models import (
    AccessMode,
    CatalogState,
    CategoryMode,
    MediaType,
    UsersState,
    UserStatus,
    WatchStatus,
)
from app.repositories import CatalogRepository, UserRepository
from app.services import CatalogQueryService
from app.storage import MemorySnapshotBackend, StateStore, StorageError


@pytest.fixture
async def catalog_pair():
    backend = MemorySnapshotBackend("catalog")
    store = StateStore(backend, CatalogState, CatalogState)
    await store.initialize()
    return CatalogRepository(store), backend


@pytest.fixture
async def user_pair():
    backend = MemorySnapshotBackend("users")
    store = StateStore(backend, UsersState, UsersState)
    await store.initialize()
    return UserRepository(store), backend


async def _add_file(catalog, category_id, message_id, text):
    metadata = parse_metadata(text)
    return await catalog.upsert_file(
        category_id=category_id,
        source_chat_id=-100100,
        source_message_id=message_id,
        telegram_file_id=f"tg-{message_id}",
        telegram_file_unique_id=f"unique-{message_id}",
        media_type=MediaType.VIDEO,
        metadata=metadata,
    )


async def test_dynamic_category_and_series_grouping(catalog_pair):
    catalog, _ = catalog_pair
    category = await catalog.add_category(
        "Series", -100100, "Series Storage", CategoryMode.EPISODIC, 1
    )
    first, content, is_new = await _add_file(
        catalog, category.id, 1, "Maamla Legal Hai S02E01 1080p Hindi WEB DL x264 mkv"
    )
    second, same_content, _ = await _add_file(
        catalog, category.id, 2, "Maamla Legal Hai S02E02 720p Hindi WEB DL x264 mkv"
    )
    assert is_new is True
    assert first.content_id == second.content_id == content.id == same_content.id
    assert catalog.category_for_channel(-100100).id == category.id

    query = CatalogQueryService(catalog)
    assert query.seasons(content.id) == [2]
    assert query.episodes(content.id, 2) == [1, 2]
    assert len(query.episode_variants(content.id, 2, 1)) == 1


async def test_pack_parts_and_search_ranking(catalog_pair):
    catalog, _ = catalog_pair
    category = await catalog.add_category("Series", -100100, None)
    for part in (1, 2, 3):
        await _add_file(
            catalog,
            category.id,
            part,
            f"Game.Of.Thrones.S01.720p.Hindi-English.HEVC.x265.zip.zip.{part:03d}",
        )
    await _add_file(catalog, category.id, 10, "Dark S01E01 1080p Hindi mkv")
    await _add_file(catalog, category.id, 11, "Dark Matter S01E01 1080p English mkv")

    query = CatalogQueryService(catalog)
    hits = query.search("dark")
    assert [hit.content.title for hit in hits[:2]] == ["Dark", "Dark Matter"]

    got = query.search("Game of Thrones")[0].content
    assert [item.pack_part for item in query.season_pack_parts(got.id, 1)] == [1, 2, 3]


async def test_duplicate_channel_update_is_an_update_not_a_duplicate_record(catalog_pair):
    catalog, _ = catalog_pair
    category = await catalog.add_category("Movies", -100100, None)
    first, content, created = await _add_file(
        catalog, category.id, 1, "Interstellar 2014 1080p Hindi English WEB DL mkv"
    )
    second, same_content, created_again = await _add_file(
        catalog, category.id, 1, "Interstellar 2014 720p Hindi English WEB DL mkv"
    )
    assert created is True
    assert created_again is False
    assert first.id == second.id
    assert content.id == same_content.id
    assert len(catalog.snapshot().files) == 1
    assert catalog.get_file(first.id).quality == "720p"


async def test_editing_only_file_preserves_content_id_and_updates_lookup(catalog_pair):
    catalog, _ = catalog_pair
    category = await catalog.add_category("Movies", -100100, None)
    first, content, _ = await _add_file(
        catalog, category.id, 1, "Interstelar 2014 1080p Hindi WEB DL mkv"
    )
    second, corrected, created = await _add_file(
        catalog, category.id, 1, "Interstellar 2014 1080p Hindi WEB DL mkv"
    )
    assert created is False
    assert first.id == second.id
    assert content.id == corrected.id
    assert catalog.get_content(content.id).title == "Interstellar"
    assert CatalogQueryService(catalog).search("Interstellar")[0].content.id == content.id


async def test_failed_commit_does_not_replace_in_memory_state(catalog_pair):
    catalog, backend = catalog_pair
    backend.fail_next_commit = True
    with pytest.raises(StorageError):
        await catalog.add_category("Movies", -100100, None)
    assert catalog.list_categories() == []


async def test_user_access_and_exact_watchlist_statuses(user_pair, catalog_pair):
    users, _ = user_pair
    catalog, _ = catalog_pair
    category = await catalog.add_category("Movies", -100100, None)
    _, content, _ = await _add_file(
        catalog, category.id, 1, "Interstellar 2014 1080p Hindi English WEB DL mkv"
    )

    user, created = await users.ensure_user(
        user_id=42,
        first_name="Test",
        last_name=None,
        username="tester",
        language_code="en",
    )
    assert created is True
    assert user.status == UserStatus.ACTIVE

    entry = await users.set_watch_status(
        user_id=42,
        content_id=content.id,
        title=content.title,
        year=content.year,
        category_id=category.id,
        category_name=category.name,
        status=WatchStatus.ON_HOLD,
    )
    assert entry.status == WatchStatus.ON_HOLD
    assert {status.value for status in WatchStatus} == {"to_watch", "on_hold", "completed"}

    await users.set_access_mode(AccessMode.APPROVAL)
    pending, _ = await users.ensure_user(
        user_id=43,
        first_name="Pending",
        last_name=None,
        username=None,
        language_code="en",
    )
    assert pending.status == UserStatus.PENDING

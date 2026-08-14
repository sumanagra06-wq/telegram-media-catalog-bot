import pytest

from app.metadata import parse_metadata
from app.models import (
    AccessMode,
    CatalogState,
    CategoryMode,
    ContentKind,
    ContentRecord,
    MediaType,
    UsersState,
    UserStatus,
    WatchStatus,
)
from app.repositories import CatalogRepository, UserRepository
from app.services import CatalogQueryService
from app.storage import MemorySnapshotBackend, StateStore, StorageError
from app.ui import pack_screen, season_screen
from app.utils import normalize_title


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


async def test_catalog_schema_v2_snapshot_migrates_to_v3():
    legacy = CatalogState(schema_version=2, revision=7).model_dump(mode="json")
    backend = MemorySnapshotBackend("catalog", initial=legacy)
    store = StateStore(backend, CatalogState, CatalogState)
    await store.initialize()
    catalog = CatalogRepository(store)

    assert await catalog.migrate_schema() is True
    assert catalog.snapshot().schema_version == 3
    assert catalog.snapshot().revision == 8
    assert catalog.recent_audit(1)[0].details == "Migrated catalog schema to version 3"
    assert await catalog.migrate_schema() is False


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


async def test_repair_merges_existing_episode_titles_without_reupload(catalog_pair):
    catalog, backend = catalog_pair
    category = await catalog.add_category(
        "Series", -100100, "Series Storage", CategoryMode.EPISODIC, 1
    )
    for episode in range(1, 7):
        await _add_file(
            catalog,
            category.id,
            episode + 2,
            f"Operation Safed Sagar The Highest Air Force Mission S01E{episode:02d} 1 mkv",
        )

    # Recreate the exact bad persisted shape observed in production: six physical files,
    # six content IDs, and filename-style titles that still include the episode token.
    state = catalog.store.state
    records = sorted(state.files.values(), key=lambda item: item.source_message_id)
    state.contents = {}
    state.content_lookup = {}
    immutable_media_fields = {
        record.id: (
            record.source_chat_id,
            record.source_message_id,
            record.telegram_file_id,
            record.telegram_file_unique_id,
        )
        for record in records
    }
    for episode, record in enumerate(records, start=1):
        raw_title = f"Operation Safed Sagar The Highest Air Force Mission S01E{episode:02d} 1 mkv"
        content_id = f"c_old_{episode}"
        normalized = normalize_title(raw_title)
        group_key = f"{category.id}|{normalized}|?"
        record.content_id = content_id
        record.title = raw_title
        state.contents[content_id] = ContentRecord(
            id=content_id,
            group_key=group_key,
            category_id=category.id,
            title=raw_title,
            normalized_title=normalized,
            kind=ContentKind.SERIES,
            file_ids=[record.id],
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        state.content_lookup[group_key] = content_id

    revision_before = state.revision
    commits_before = len(backend.commits)
    result = await catalog.repair_episodic_grouping()
    repaired = catalog.snapshot()

    assert result.changed is True
    assert result.updated_files == 6
    assert result.merged_contents == 5
    assert len(result.content_id_remap) == 5
    assert set(result.repaired_file_ids) == set(repaired.files)
    assert repaired.revision == revision_before + 1
    assert len(repaired.contents) == 1
    assert len(repaired.files) == 6
    content = next(iter(repaired.contents.values()))
    assert content.title == "Operation Safed Sagar The Highest Air Force Mission"
    assert {record.content_id for record in repaired.files.values()} == {content.id}
    assert {record.title for record in repaired.files.values()} == {content.title}
    assert {
        record.id: (
            record.source_chat_id,
            record.source_message_id,
            record.telegram_file_id,
            record.telegram_file_unique_id,
        )
        for record in repaired.files.values()
    } == immutable_media_fields

    second = await catalog.repair_episodic_grouping()
    assert second.changed is False
    assert catalog.snapshot().revision == revision_before + 1
    assert len(backend.commits) == commits_before + 1


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


async def test_combined_episode_ranges_render_as_labeled_season_packs(catalog_pair):
    catalog, _ = catalog_pair
    category = await catalog.add_category(
        "Series",
        -100100,
        None,
        CategoryMode.EPISODIC,
    )
    filenames = (
        "Doraemon.S18.Ep.1to15.Combined.Multi.Audio+Hindi.mkv",
        "Doraemon.S18.Ep31To40.Hindi+Multi.Audio.mkv",
    )
    for message_id, filename in enumerate(filenames, start=1):
        await _add_file(catalog, category.id, message_id, filename)

    content = CatalogQueryService(catalog).search("Doraemon")[0].content
    query = CatalogQueryService(catalog)
    parts = query.season_pack_parts(content.id, 18)
    assert [(item.episode_start, item.episode_end) for item in parts] == [(1, 15), (31, 40)]
    assert all(item.episode is None for item in parts)

    season_text, season_markup = season_screen(content, 18, query, "0", 0)
    assert "Combined episode-range packs" in season_text
    assert "Download combined episode packs" in str(season_markup)

    pack_text, pack_markup = pack_screen(content, 18, parts, "0", 0)
    assert "combined episode packs" in pack_text
    assert "Episodes 1–15" in str(pack_markup)
    assert "Episodes 31–40" in str(pack_markup)


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

    entry, entry_created = await users.upsert_watchlist_entry(
        user_id=42,
        content_id=content.id,
        title=content.title,
        year=content.year,
        category_id=category.id,
        category_name=category.name,
        status=WatchStatus.ON_HOLD,
    )
    assert entry_created is True
    assert entry.id.startswith("w_")
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

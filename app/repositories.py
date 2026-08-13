from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .metadata import ParsedMetadata, canonicalize_title
from .models import (
    AccessMode,
    AuditEvent,
    CatalogState,
    Category,
    CategoryMode,
    ContentKind,
    ContentRecord,
    FileRecord,
    IndexFailure,
    MediaType,
    RecordKind,
    RemovedSourceRecord,
    UserProfile,
    UsersState,
    UserStatus,
    WatchlistEntry,
    WatchStatus,
)
from .storage import StateStore
from .utils import make_id, normalize_title, slugify, utcnow_iso


@dataclass(frozen=True)
class CatalogRepairResult:
    updated_files: int = 0
    updated_contents: int = 0
    merged_contents: int = 0
    content_id_remap: dict[str, str] = field(default_factory=dict)
    repaired_file_ids: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.updated_files or self.updated_contents or self.merged_contents)


@dataclass(frozen=True)
class ContentRemovalResult:
    content: ContentRecord
    files: tuple[FileRecord, ...]
    sources: tuple[RemovedSourceRecord, ...]


@dataclass(frozen=True)
class BulkWatchlistResult:
    entries: tuple[WatchlistEntry, ...]
    created: int
    updated: int


def _source_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def _append_audit(
    state: CatalogState,
    action: str,
    details: str,
    actor_id: int | None = None,
) -> None:
    state.audit_events.append(
        AuditEvent(
            id=make_id("audit"),
            action=action,
            actor_id=actor_id,
            details=details,
        )
    )
    state.audit_events = state.audit_events[-200:]


class CatalogRepository:
    def __init__(self, store: StateStore[CatalogState]) -> None:
        self.store = store

    def snapshot(self) -> CatalogState:
        return self.store.snapshot()

    async def migrate_schema(self) -> bool:
        if self.store.state.schema_version >= 2:
            return False

        def mutate(state: CatalogState) -> bool:
            state.schema_version = 2
            _append_audit(state, "catalog.schema", "Migrated catalog schema to version 2")
            return True

        return await self.store.mutate(mutate)

    def is_source_removed(self, chat_id: int, message_id: int) -> bool:
        return _source_key(chat_id, message_id) in self.store.state.removed_sources

    def pending_removed_sources(self) -> list[RemovedSourceRecord]:
        return [
            item.model_copy(deep=True)
            for item in self.store.state.removed_sources.values()
            if not item.telegram_deleted
        ]

    def get_category(self, category_id: str) -> Category | None:
        item = self.store.state.categories.get(category_id)
        return item.model_copy(deep=True) if item else None

    def category_for_channel(self, channel_id: int) -> Category | None:
        for category in self.store.state.categories.values():
            if (
                channel_id == category.active_channel_id
                or channel_id in category.legacy_channel_ids
            ):
                return category.model_copy(deep=True)
        return None

    def list_categories(self, include_disabled: bool = False) -> list[Category]:
        values = self.store.state.categories.values()
        result = [item.model_copy(deep=True) for item in values if include_disabled or item.enabled]
        return sorted(result, key=lambda item: item.name.casefold())

    async def add_category(
        self,
        name: str,
        channel_id: int,
        channel_title: str | None,
        mode: CategoryMode = CategoryMode.MIXED,
        actor_id: int | None = None,
    ) -> Category:
        name = " ".join(name.split()).strip()
        if not name:
            raise ValueError("Category name cannot be empty")

        def mutate(state: CatalogState) -> Category:
            for existing in state.categories.values():
                if existing.name.casefold() == name.casefold():
                    raise ValueError("A category with this name already exists")
                if (
                    channel_id == existing.active_channel_id
                    or channel_id in existing.legacy_channel_ids
                ):
                    raise ValueError("This channel is already assigned to a category")
            base_slug = slugify(name)
            used = {item.slug for item in state.categories.values()}
            slug = base_slug
            suffix = 2
            while slug in used:
                slug = f"{base_slug}-{suffix}"
                suffix += 1
            category = Category(
                id=make_id("cat"),
                name=name,
                slug=slug,
                active_channel_id=channel_id,
                channel_title=channel_title,
                mode=mode,
            )
            state.categories[category.id] = category
            _append_audit(state, "category.add", f"Added {name} ({channel_id})", actor_id)
            return category.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def rename_category(
        self, category_id: str, new_name: str, actor_id: int | None = None
    ) -> Category:
        new_name = " ".join(new_name.split()).strip()
        if not new_name:
            raise ValueError("Category name cannot be empty")

        def mutate(state: CatalogState) -> Category:
            category = state.categories.get(category_id)
            if category is None:
                raise ValueError("Category not found")
            if any(
                item.id != category_id and item.name.casefold() == new_name.casefold()
                for item in state.categories.values()
            ):
                raise ValueError("A category with this name already exists")
            old = category.name
            category.name = new_name
            category.updated_at = utcnow_iso()
            _append_audit(state, "category.rename", f"Renamed {old} to {new_name}", actor_id)
            return category.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def set_category_mode(
        self, category_id: str, mode: CategoryMode, actor_id: int | None = None
    ) -> Category:
        def mutate(state: CatalogState) -> Category:
            category = state.categories.get(category_id)
            if category is None:
                raise ValueError("Category not found")
            category.mode = mode
            category.updated_at = utcnow_iso()
            _append_audit(
                state, "category.mode", f"Set {category.name} mode to {mode.value}", actor_id
            )
            return category.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def set_category_enabled(
        self, category_id: str, enabled: bool, actor_id: int | None = None
    ) -> Category:
        def mutate(state: CatalogState) -> Category:
            category = state.categories.get(category_id)
            if category is None:
                raise ValueError("Category not found")
            category.enabled = enabled
            category.updated_at = utcnow_iso()
            _append_audit(
                state,
                "category.enable" if enabled else "category.disable",
                f"{'Enabled' if enabled else 'Disabled'} {category.name}",
                actor_id,
            )
            return category.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def change_category_channel(
        self,
        category_id: str,
        channel_id: int,
        channel_title: str | None,
        actor_id: int | None = None,
    ) -> Category:
        def mutate(state: CatalogState) -> Category:
            category = state.categories.get(category_id)
            if category is None:
                raise ValueError("Category not found")
            for item in state.categories.values():
                if item.id == category_id:
                    continue
                if channel_id == item.active_channel_id or channel_id in item.legacy_channel_ids:
                    raise ValueError("This channel is already assigned to another category")
            old = category.active_channel_id
            if old != channel_id and old not in category.legacy_channel_ids:
                category.legacy_channel_ids.append(old)
            if channel_id in category.legacy_channel_ids:
                category.legacy_channel_ids.remove(channel_id)
            category.active_channel_id = channel_id
            category.channel_title = channel_title
            category.updated_at = utcnow_iso()
            _append_audit(
                state,
                "category.channel",
                f"Changed {category.name} channel from {old} to {channel_id}",
                actor_id,
            )
            return category.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def record_failure(
        self,
        source_chat_id: int,
        source_message_id: int,
        category_id: str,
        reason: str,
    ) -> None:
        key = _source_key(source_chat_id, source_message_id)

        def mutate(state: CatalogState) -> None:
            previous = state.failures.get(key)
            state.failures[key] = IndexFailure(
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                category_id=category_id,
                reason=reason,
                created_at=previous.created_at if previous else utcnow_iso(),
                updated_at=utcnow_iso(),
            )
            _append_audit(state, "index.failure", f"{key}: {reason}")

        await self.store.mutate(mutate)

    @staticmethod
    def _repair_episodic_state(state: CatalogState) -> CatalogRepairResult:
        """Canonicalize and merge episodic records without touching Telegram media.

        This repairs snapshots produced when a filename-style value after ``Title:`` or
        ``Name:`` was stored verbatim. Physical file records and source message references
        remain unchanged; only catalog metadata and content associations are rewritten.
        """

        groups: dict[str, list[tuple[FileRecord, str]]] = defaultdict(list)
        for record in state.files.values():
            if record.season is None and record.episode is None:
                continue
            canonical = canonicalize_title(record.title)
            if not canonical:
                continue
            content = state.contents.get(record.content_id)
            year = record.year if record.year is not None else (content.year if content else None)
            key = f"{record.category_id}|{normalize_title(canonical)}|{year or '?'}"
            groups[key].append((record, canonical))

        updated_file_ids: set[str] = set()
        updated_content_ids: set[str] = set()
        involved_content_ids: set[str] = set()
        remap_targets: dict[str, set[str]] = defaultdict(set)

        for group_key, entries in sorted(groups.items()):
            entries.sort(
                key=lambda item: (item[0].created_at, item[0].source_message_id, item[0].id)
            )
            canonical_title = entries[0][1]
            normalized = normalize_title(canonical_title)
            year = next((record.year for record, _ in entries if record.year is not None), None)
            candidate_ids = {record.content_id for record, _ in entries}
            involved_content_ids.update(candidate_ids)
            candidates = [
                state.contents[content_id]
                for content_id in candidate_ids
                if content_id in state.contents
            ]
            if not candidates:
                continue
            if year is None:
                year = next(
                    (content.year for content in candidates if content.year is not None),
                    None,
                )
            exact = [
                content
                for content in candidates
                if content.group_key == group_key
                or (
                    content.normalized_title == normalized
                    and content.year == year
                    and content.category_id == entries[0][0].category_id
                )
            ]
            primary = min(
                exact or candidates,
                key=lambda content: (content.created_at, content.id),
            )
            for record, _ in entries:
                old_content_id = record.content_id
                if record.title != canonical_title:
                    record.title = canonical_title
                    record.updated_at = utcnow_iso()
                    updated_file_ids.add(record.id)
                if old_content_id != primary.id:
                    record.content_id = primary.id
                    record.updated_at = utcnow_iso()
                    updated_file_ids.add(record.id)
                    remap_targets[old_content_id].add(primary.id)

            desired = (
                primary.title != canonical_title
                or primary.normalized_title != normalized
                or primary.group_key != group_key
                or primary.category_id != entries[0][0].category_id
                or primary.year != year
                or primary.kind != ContentKind.SERIES
            )
            if desired:
                primary.title = canonical_title
                primary.normalized_title = normalized
                primary.group_key = group_key
                primary.category_id = entries[0][0].category_id
                primary.year = year
                primary.kind = ContentKind.SERIES
                primary.updated_at = utcnow_iso()
                updated_content_ids.add(primary.id)

        # Rebuild associations from authoritative file records after every reassignment.
        old_file_ids = {content.id: list(content.file_ids) for content in state.contents.values()}
        for content in state.contents.values():
            content.file_ids = []
        for record in sorted(state.files.values(), key=lambda item: (item.created_at, item.id)):
            content = state.contents.get(record.content_id)
            if content is not None:
                content.file_ids.append(record.id)
        for content in state.contents.values():
            if content.file_ids != old_file_ids.get(content.id, []):
                content.updated_at = utcnow_iso()
                updated_content_ids.add(content.id)

        obsolete_ids = {
            content_id
            for content_id in involved_content_ids
            if content_id in state.contents and not state.contents[content_id].file_ids
        }
        content_id_remap: dict[str, str] = {}
        for content_id in obsolete_ids:
            targets = remap_targets.get(content_id, set())
            if len(targets) == 1:
                content_id_remap[content_id] = next(iter(targets))
            state.contents.pop(content_id, None)
            updated_content_ids.discard(content_id)

        # Drop all stale episode-specific keys and rebuild the authoritative lookup.
        rebuilt_lookup: dict[str, str] = {}
        for content in sorted(state.contents.values(), key=lambda item: (item.created_at, item.id)):
            rebuilt_lookup.setdefault(content.group_key, content.id)
        lookup_changed = rebuilt_lookup != state.content_lookup
        state.content_lookup = rebuilt_lookup

        result = CatalogRepairResult(
            updated_files=len(updated_file_ids),
            updated_contents=len(updated_content_ids)
            + int(lookup_changed and not updated_content_ids),
            merged_contents=len(obsolete_ids),
            content_id_remap=content_id_remap,
            repaired_file_ids=tuple(sorted(updated_file_ids)),
        )
        if result.changed:
            _append_audit(
                state,
                "catalog.repair.episodic",
                f"Canonicalized {result.updated_files} files and merged "
                f"{result.merged_contents} duplicate titles",
            )
        return result

    async def repair_episodic_grouping(self) -> CatalogRepairResult:
        preview = self.store.state.model_copy(deep=True)
        preview_result = self._repair_episodic_state(preview)
        if not preview_result.changed:
            return preview_result

        def mutate(state: CatalogState) -> CatalogRepairResult:
            return self._repair_episodic_state(state)

        return await self.store.mutate(mutate)

    @staticmethod
    def _choose_content(
        state: CatalogState,
        category_id: str,
        metadata: ParsedMetadata,
    ) -> ContentRecord | None:
        normalized = normalize_title(metadata.title)
        exact_key = f"{category_id}|{normalized}|{metadata.year or '?'}"
        exact_id = state.content_lookup.get(exact_key)
        exact = state.contents.get(exact_id) if exact_id else None
        if exact is not None:
            return exact
        candidates = [
            item
            for item in state.contents.values()
            if item.category_id == category_id and item.normalized_title == normalized
        ]
        if metadata.year is not None:
            unknown = next((item for item in candidates if item.year is None), None)
            if unknown:
                return unknown
            return None
        if len(candidates) == 1:
            return candidates[0]
        unknown = next((item for item in candidates if item.year is None), None)
        return unknown

    async def upsert_file(
        self,
        *,
        category_id: str,
        source_chat_id: int,
        source_message_id: int,
        telegram_file_id: str,
        telegram_file_unique_id: str,
        media_type: MediaType,
        metadata: ParsedMetadata,
    ) -> tuple[FileRecord, ContentRecord, bool]:
        # Enforce one stable series identity at the persistence boundary even if a future
        # caption-parser branch accidentally leaves SxxExx or technical suffixes in the title.
        if metadata.season is not None or metadata.episode is not None:
            canonical_title = canonicalize_title(metadata.title)
            if canonical_title and canonical_title != metadata.title:
                metadata = metadata.model_copy(update={"title": canonical_title})
        source = _source_key(source_chat_id, source_message_id)

        def mutate(state: CatalogState) -> tuple[FileRecord, ContentRecord, bool]:
            if source in state.removed_sources:
                raise ValueError("This source message was permanently removed by the owner")
            category = state.categories.get(category_id)
            if category is None:
                raise ValueError("Category not found")
            old_file_id = state.source_lookup.get(source)
            old_file = state.files.get(old_file_id) if old_file_id else None
            is_new = old_file is None

            content = self._choose_content(state, category_id, metadata)
            reused_old_content = False
            if content is None and old_file is not None:
                old_content = state.contents.get(old_file.content_id)
                if old_content is not None and old_content.file_ids == [old_file.id]:
                    # Correcting the only file's title/year should preserve content deep links and
                    # watchlist relationships rather than create a ghost content record.
                    content = old_content
                    reused_old_content = True
            content_kind = (
                ContentKind.SERIES
                if metadata.season is not None or metadata.episode is not None
                else ContentKind.MOVIE
            )
            if content is None:
                normalized = normalize_title(metadata.title)
                group_key = f"{category_id}|{normalized}|{metadata.year or '?'}"
                content = ContentRecord(
                    id=make_id("c"),
                    group_key=group_key,
                    category_id=category_id,
                    title=metadata.title,
                    normalized_title=normalized,
                    year=metadata.year,
                    kind=content_kind,
                )
                state.contents[content.id] = content
                state.content_lookup[group_key] = content.id
            else:
                old_key = content.group_key
                content.title = metadata.title
                content.normalized_title = normalize_title(metadata.title)
                if metadata.year is not None and (reused_old_content or content.year is None):
                    content.year = metadata.year
                content.group_key = (
                    f"{category_id}|{content.normalized_title}|{content.year or '?'}"
                )
                if old_key != content.group_key:
                    state.content_lookup.pop(old_key, None)
                state.content_lookup[content.group_key] = content.id
                if content_kind == ContentKind.SERIES:
                    content.kind = ContentKind.SERIES
                content.updated_at = utcnow_iso()

            if metadata.episode is not None:
                record_kind = RecordKind.EPISODE
            elif metadata.season is not None:
                record_kind = RecordKind.SEASON_PACK_PART
            else:
                record_kind = RecordKind.MOVIE

            file_id = old_file.id if old_file else make_id("f")
            created_at = old_file.created_at if old_file else utcnow_iso()
            record = FileRecord(
                id=file_id,
                content_id=content.id,
                category_id=category_id,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                telegram_file_id=telegram_file_id,
                telegram_file_unique_id=telegram_file_unique_id,
                media_type=media_type,
                record_kind=record_kind,
                title=metadata.title,
                year=metadata.year or content.year,
                languages=metadata.languages,
                quality=metadata.quality,
                season=metadata.season,
                episode=metadata.episode,
                pack_part=metadata.pack_part
                or (1 if record_kind == RecordKind.SEASON_PACK_PART else None),
                available=True,
                created_at=created_at,
                updated_at=utcnow_iso(),
            )

            if old_file and old_file.content_id != content.id:
                old_content = state.contents.get(old_file.content_id)
                if old_content and file_id in old_content.file_ids:
                    old_content.file_ids.remove(file_id)
                    old_content.updated_at = utcnow_iso()
            state.files[file_id] = record
            state.source_lookup[source] = file_id
            if file_id not in content.file_ids:
                content.file_ids.append(file_id)
            content.updated_at = utcnow_iso()
            state.failures.pop(source, None)
            _append_audit(
                state,
                "index.add" if is_new else "index.update",
                f"{metadata.title} from {source}",
            )
            return (
                record.model_copy(deep=True),
                content.model_copy(deep=True),
                is_new,
            )

        return await self.store.mutate(mutate)

    async def remove_content(
        self,
        content_id: str,
        actor_id: int,
    ) -> ContentRemovalResult:
        """Remove one catalog title and tombstone every source before Telegram deletion.

        The catalog commit is the safety boundary: delivery and automatic re-indexing are
        blocked even if deleting a source-channel message later fails.
        """

        def mutate(state: CatalogState) -> ContentRemovalResult:
            content = state.contents.get(content_id)
            if content is None:
                raise ValueError("Title not found")
            files = sorted(
                (record for record in state.files.values() if record.content_id == content.id),
                key=lambda record: (record.source_chat_id, record.source_message_id, record.id),
            )
            sources: list[RemovedSourceRecord] = []
            for record in files:
                source = _source_key(record.source_chat_id, record.source_message_id)
                tombstone = RemovedSourceRecord(
                    source_chat_id=record.source_chat_id,
                    source_message_id=record.source_message_id,
                    content_title=content.title,
                )
                state.removed_sources[source] = tombstone
                sources.append(tombstone)
                state.files.pop(record.id, None)
                state.source_lookup.pop(source, None)
                state.failures.pop(source, None)

            state.contents.pop(content.id, None)
            for key, value in list(state.content_lookup.items()):
                if value == content.id:
                    state.content_lookup.pop(key, None)
            _append_audit(
                state,
                "content.remove",
                f"Removed {content.title} with {len(files)} files",
                actor_id,
            )
            return ContentRemovalResult(
                content=content.model_copy(deep=True),
                files=tuple(record.model_copy(deep=True) for record in files),
                sources=tuple(item.model_copy(deep=True) for item in sources),
            )

        return await self.store.mutate(mutate)

    async def mark_removed_sources_deleted(self, sources: Iterable[tuple[int, int]]) -> int:
        keys = {_source_key(chat_id, message_id) for chat_id, message_id in sources}
        pending = [
            key
            for key in keys
            if key in self.store.state.removed_sources
            and not self.store.state.removed_sources[key].telegram_deleted
        ]
        if not pending:
            return 0

        def mutate(state: CatalogState) -> int:
            changed = 0
            for key in pending:
                item = state.removed_sources.get(key)
                if item is None or item.telegram_deleted:
                    continue
                item.telegram_deleted = True
                item.updated_at = utcnow_iso()
                changed += 1
            if changed:
                _append_audit(
                    state,
                    "content.sources_deleted",
                    f"Confirmed deletion of {changed} source messages",
                )
            return changed

        return await self.store.mutate(mutate)

    async def mark_file_available(self, file_id: str, available: bool) -> FileRecord:
        def mutate(state: CatalogState) -> FileRecord:
            record = state.files.get(file_id)
            if record is None:
                raise ValueError("File not found")
            record.available = available
            record.updated_at = utcnow_iso()
            _append_audit(
                state,
                "file.available" if available else "file.unavailable",
                f"Set {file_id} available={available}",
            )
            return record.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def add_audit(self, action: str, details: str, actor_id: int | None) -> None:
        def mutate(state: CatalogState) -> None:
            _append_audit(state, action, details, actor_id)

        await self.store.mutate(mutate)

    def get_content(self, content_id: str) -> ContentRecord | None:
        item = self.store.state.contents.get(content_id)
        return item.model_copy(deep=True) if item else None

    def get_file(self, file_id: str) -> FileRecord | None:
        item = self.store.state.files.get(file_id)
        return item.model_copy(deep=True) if item else None

    def files_for_content(self, content_id: str, available_only: bool = True) -> list[FileRecord]:
        content = self.store.state.contents.get(content_id)
        if content is None:
            return []
        result = []
        for file_id in content.file_ids:
            record = self.store.state.files.get(file_id)
            if record and (record.available or not available_only):
                result.append(record.model_copy(deep=True))
        return result

    def recent_audit(self, limit: int = 20) -> list[AuditEvent]:
        return [item.model_copy(deep=True) for item in self.store.state.audit_events[-limit:]][::-1]


class UserRepository:
    def __init__(self, store: StateStore[UsersState]) -> None:
        self.store = store

    def snapshot(self) -> UsersState:
        return self.store.snapshot()

    async def migrate_schema(self) -> bool:
        needs_migration = self.store.state.schema_version < 4 or any(
            not entry.id or key != entry.id
            for user in self.store.state.users.values()
            for key, entry in user.watchlist.items()
        )
        if not needs_migration:
            return False

        def mutate(state: UsersState) -> bool:
            for user in state.users.values():
                migrated: dict[str, WatchlistEntry] = {}
                changed = False
                for key, entry in user.watchlist.items():
                    entry_id = entry.id or make_id("w")
                    while entry_id in migrated:
                        entry_id = make_id("w")
                    if key != entry_id or entry.id != entry_id:
                        changed = True
                    entry.id = entry_id
                    migrated[entry_id] = entry
                user.watchlist = migrated
                if not user.watchlist_public:
                    user.watchlist_public = True
                    changed = True
                if changed:
                    user.updated_at = utcnow_iso()
            state.schema_version = 4
            return True

        return await self.store.mutate(mutate)

    def get_user(self, user_id: int) -> UserProfile | None:
        item = self.store.state.users.get(str(user_id))
        return item.model_copy(deep=True) if item else None

    def list_users(self, statuses: Iterable[UserStatus] | None = None) -> list[UserProfile]:
        allowed = set(statuses) if statuses else None
        users = [
            item.model_copy(deep=True)
            for item in self.store.state.users.values()
            if allowed is None or item.status in allowed
        ]
        return sorted(users, key=lambda item: item.created_at, reverse=True)

    async def ensure_user(
        self,
        *,
        user_id: int,
        first_name: str,
        last_name: str | None,
        username: str | None,
        language_code: str | None,
        is_owner: bool = False,
    ) -> tuple[UserProfile, bool]:
        existing = self.store.state.users.get(str(user_id))
        now = datetime.now(UTC)
        should_write = existing is None
        if existing:
            metadata_changed = (
                existing.first_name != first_name
                or existing.last_name != last_name
                or existing.username != username
                or existing.language_code != language_code
            )
            try:
                last_seen = datetime.fromisoformat(existing.last_seen_at)
            except ValueError:
                last_seen = now - timedelta(days=1)
            should_write = metadata_changed or now - last_seen >= timedelta(hours=6)
            if is_owner and existing.status != UserStatus.ACTIVE:
                should_write = True
            if (
                self.store.state.access_mode == AccessMode.PUBLIC
                and existing.status == UserStatus.PENDING
            ):
                should_write = True
        if not should_write and existing:
            return existing.model_copy(deep=True), False

        def mutate(state: UsersState) -> tuple[UserProfile, bool]:
            key = str(user_id)
            user = state.users.get(key)
            created = user is None
            if user is None:
                status = (
                    UserStatus.ACTIVE
                    if is_owner or state.access_mode == AccessMode.PUBLIC
                    else UserStatus.PENDING
                )
                user = UserProfile(
                    telegram_user_id=user_id,
                    first_name=first_name,
                    last_name=last_name,
                    username=username,
                    language_code=language_code,
                    status=status,
                )
                state.users[key] = user
            else:
                user.first_name = first_name
                user.last_name = last_name
                user.username = username
                user.language_code = language_code
                if is_owner or (
                    state.access_mode == AccessMode.PUBLIC and user.status == UserStatus.PENDING
                ):
                    user.status = UserStatus.ACTIVE
                user.updated_at = utcnow_iso()
                user.last_seen_at = utcnow_iso()
            return user.model_copy(deep=True), created

        return await self.store.mutate(mutate)

    async def set_access_mode(self, mode: AccessMode) -> AccessMode:
        def mutate(state: UsersState) -> AccessMode:
            state.access_mode = mode
            return mode

        return await self.store.mutate(mutate)

    async def set_user_status(self, user_id: int, status: UserStatus) -> UserProfile:
        def mutate(state: UsersState) -> UserProfile:
            user = state.users.get(str(user_id))
            if user is None:
                raise ValueError("User not found")
            user.status = status
            user.updated_at = utcnow_iso()
            return user.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def set_panel_dashboard_message(self, user_id: int, message_id: int) -> UserProfile:
        current = self.store.state.users.get(str(user_id))
        if current is None:
            raise ValueError("User not found")
        if current.panel_dashboard_message_id == message_id:
            return current.model_copy(deep=True)

        def mutate(state: UsersState) -> UserProfile:
            user = state.users.get(str(user_id))
            if user is None:
                raise ValueError("User not found")
            user.panel_dashboard_message_id = message_id
            user.updated_at = utcnow_iso()
            return user.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def set_panel_workspace_message(self, user_id: int, message_id: int) -> UserProfile:
        current = self.store.state.users.get(str(user_id))
        if current is None:
            raise ValueError("User not found")
        if current.panel_workspace_message_id == message_id:
            return current.model_copy(deep=True)

        def mutate(state: UsersState) -> UserProfile:
            user = state.users.get(str(user_id))
            if user is None:
                raise ValueError("User not found")
            user.panel_workspace_message_id = message_id
            user.updated_at = utcnow_iso()
            return user.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def replace_panel_workspace_message(
        self,
        user_id: int,
        message_id: int,
        *,
        expected_previous_id: int | None,
    ) -> UserProfile:
        def mutate(state: UsersState) -> UserProfile:
            user = state.users.get(str(user_id))
            if user is None:
                raise ValueError("User not found")
            if user.panel_workspace_message_id != expected_previous_id:
                raise ValueError("Workspace changed while it was being replaced")
            user.panel_workspace_message_id = message_id
            user.updated_at = utcnow_iso()
            return user.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def clear_panel_workspace_message(
        self,
        user_id: int,
        *,
        expected_message_id: int | None = None,
    ) -> bool:
        current = self.store.state.users.get(str(user_id))
        if current is None or current.panel_workspace_message_id is None:
            return False
        if (
            expected_message_id is not None
            and current.panel_workspace_message_id != expected_message_id
        ):
            return False

        def mutate(state: UsersState) -> bool:
            user = state.users.get(str(user_id))
            if user is None or user.panel_workspace_message_id is None:
                return False
            if (
                expected_message_id is not None
                and user.panel_workspace_message_id != expected_message_id
            ):
                return False
            user.panel_workspace_message_id = None
            user.updated_at = utcnow_iso()
            return True

        return await self.store.mutate(mutate)

    async def clear_all_panel_workspace_messages(self) -> int:
        if not any(
            user.panel_workspace_message_id is not None for user in self.store.state.users.values()
        ):
            return 0

        def mutate(state: UsersState) -> int:
            cleared = 0
            now = utcnow_iso()
            for user in state.users.values():
                if user.panel_workspace_message_id is None:
                    continue
                user.panel_workspace_message_id = None
                user.updated_at = now
                cleared += 1
            return cleared

        return await self.store.mutate(mutate)

    async def upsert_watchlist_entry(
        self,
        *,
        user_id: int,
        title: str,
        category_id: str,
        category_name: str,
        status: WatchStatus,
        content_id: str | None = None,
        year: int | None = None,
    ) -> tuple[WatchlistEntry, bool]:
        title = " ".join(title.split()).strip()
        if not title:
            raise ValueError("Title cannot be empty")
        if len(title) > 160:
            raise ValueError("Title must be 160 characters or fewer")

        def mutate(state: UsersState) -> tuple[WatchlistEntry, bool]:
            user = state.users.get(str(user_id))
            if user is None:
                raise ValueError("User not found")
            normalized = normalize_title(title)
            existing = next(
                (
                    entry
                    for entry in user.watchlist.values()
                    if entry.category_id == category_id
                    and (
                        (content_id is not None and entry.content_id == content_id)
                        or normalize_title(entry.title) == normalized
                    )
                ),
                None,
            )
            created = existing is None
            entry_id = existing.id if existing else make_id("w")
            entry = WatchlistEntry(
                id=entry_id,
                content_id=content_id
                if content_id is not None
                else (existing.content_id if existing else None),
                title=title,
                year=year if year is not None else (existing.year if existing else None),
                category_id=category_id,
                category_name=category_name,
                status=status,
                added_at=existing.added_at if existing else utcnow_iso(),
                updated_at=utcnow_iso(),
            )
            user.watchlist[entry_id] = entry
            user.updated_at = utcnow_iso()
            return entry.model_copy(deep=True), created

        return await self.store.mutate(mutate)

    async def bulk_upsert_catalog_watchlist(
        self,
        *,
        user_id: int,
        items: Iterable[tuple[ContentRecord, str]],
        status: WatchStatus,
    ) -> BulkWatchlistResult:
        selected = tuple(items)
        if not selected:
            raise ValueError("Select at least one title")
        if len(selected) > 25:
            raise ValueError("Select no more than 25 titles at once")
        content_ids = [content.id for content, _ in selected]
        if len(set(content_ids)) != len(content_ids):
            raise ValueError("Duplicate catalog titles are not allowed")

        def mutate(state: UsersState) -> BulkWatchlistResult:
            user = state.users.get(str(user_id))
            if user is None:
                raise ValueError("User not found")
            results: list[WatchlistEntry] = []
            created_count = 0
            for content, category_name in selected:
                existing = next(
                    (
                        entry
                        for entry in user.watchlist.values()
                        if entry.category_id == content.category_id
                        and (
                            entry.content_id == content.id
                            or normalize_title(entry.title) == normalize_title(content.title)
                        )
                    ),
                    None,
                )
                if existing is None:
                    created_count += 1
                entry_id = existing.id if existing else make_id("w")
                entry = WatchlistEntry(
                    id=entry_id,
                    content_id=content.id,
                    title=content.title,
                    year=content.year,
                    category_id=content.category_id,
                    category_name=category_name,
                    status=status,
                    added_at=existing.added_at if existing else utcnow_iso(),
                    updated_at=utcnow_iso(),
                )
                user.watchlist[entry_id] = entry
                results.append(entry.model_copy(deep=True))
            user.updated_at = utcnow_iso()
            return BulkWatchlistResult(
                entries=tuple(results),
                created=created_count,
                updated=len(results) - created_count,
            )

        return await self.store.mutate(mutate)

    async def bulk_upsert_manual_watchlist(
        self,
        *,
        user_id: int,
        titles: Iterable[str],
        category_id: str,
        category_name: str,
        status: WatchStatus,
    ) -> BulkWatchlistResult:
        cleaned: list[str] = []
        normalized_seen: set[str] = set()
        for raw_title in titles:
            title = " ".join(raw_title.split()).strip()
            if not title:
                continue
            if len(title) > 160:
                raise ValueError("Every title must be 160 characters or fewer")
            normalized = normalize_title(title)
            if not normalized or normalized in normalized_seen:
                continue
            normalized_seen.add(normalized)
            cleaned.append(title)
        if not cleaned:
            raise ValueError("Add at least one custom title")
        if len(cleaned) > 25:
            raise ValueError("Add no more than 25 custom titles at once")

        def mutate(state: UsersState) -> BulkWatchlistResult:
            user = state.users.get(str(user_id))
            if user is None:
                raise ValueError("User not found")
            entries: list[WatchlistEntry] = []
            created_count = 0
            for title in cleaned:
                normalized = normalize_title(title)
                existing = next(
                    (
                        entry
                        for entry in user.watchlist.values()
                        if entry.category_id == category_id
                        and normalize_title(entry.title) == normalized
                    ),
                    None,
                )
                if existing is None:
                    created_count += 1
                entry_id = existing.id if existing else make_id("w")
                entry = WatchlistEntry(
                    id=entry_id,
                    content_id=existing.content_id if existing else None,
                    title=title,
                    year=existing.year if existing else None,
                    category_id=category_id,
                    category_name=category_name,
                    status=status,
                    added_at=existing.added_at if existing else utcnow_iso(),
                    updated_at=utcnow_iso(),
                )
                user.watchlist[entry_id] = entry
                entries.append(entry.model_copy(deep=True))
            user.updated_at = utcnow_iso()
            return BulkWatchlistResult(
                entries=tuple(entries),
                created=created_count,
                updated=len(entries) - created_count,
            )

        return await self.store.mutate(mutate)

    def get_watchlist_entry(self, user_id: int, entry_id: str) -> WatchlistEntry | None:
        user = self.store.state.users.get(str(user_id))
        entry = user.watchlist.get(entry_id) if user else None
        return entry.model_copy(deep=True) if entry else None

    async def update_watchlist_status(
        self,
        user_id: int,
        entry_id: str,
        status: WatchStatus,
    ) -> WatchlistEntry:
        def mutate(state: UsersState) -> WatchlistEntry:
            user = state.users.get(str(user_id))
            if user is None:
                raise ValueError("User not found")
            entry = user.watchlist.get(entry_id)
            if entry is None:
                raise ValueError("Watchlist entry not found")
            entry.status = status
            entry.updated_at = utcnow_iso()
            user.updated_at = utcnow_iso()
            return entry.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def remove_watchlist_entry(self, user_id: int, entry_id: str) -> bool:
        def mutate(state: UsersState) -> bool:
            user = state.users.get(str(user_id))
            if user is None:
                raise ValueError("User not found")
            removed = user.watchlist.pop(entry_id, None) is not None
            if removed:
                user.updated_at = utcnow_iso()
            return removed

        return await self.store.mutate(mutate)

    async def set_watchlist_visibility(self, user_id: int, is_public: bool) -> UserProfile:
        if not is_public:
            raise ValueError("Community watchlists are always public")
        current = self.store.state.users.get(str(user_id))
        if current is None:
            raise ValueError("User not found")
        if current.watchlist_public:
            return current.model_copy(deep=True)

        def mutate(state: UsersState) -> UserProfile:
            user = state.users.get(str(user_id))
            if user is None:
                raise ValueError("User not found")
            user.watchlist_public = True
            user.updated_at = utcnow_iso()
            return user.model_copy(deep=True)

        return await self.store.mutate(mutate)

    async def set_watchlist_display_name(
        self,
        user_id: int,
        display_name: str | None,
    ) -> UserProfile:
        if display_name is not None:
            display_name = " ".join(display_name.split()).strip()
            if not display_name:
                raise ValueError("Community name cannot be empty")
            if len(display_name) > 40:
                raise ValueError("Community name must be 40 characters or fewer")
        current = self.store.state.users.get(str(user_id))
        if current is None:
            raise ValueError("User not found")
        if current.watchlist_display_name == display_name and current.watchlist_public:
            return current.model_copy(deep=True)

        def mutate(state: UsersState) -> UserProfile:
            user = state.users.get(str(user_id))
            if user is None:
                raise ValueError("User not found")
            user.watchlist_display_name = display_name
            user.watchlist_public = True
            user.updated_at = utcnow_iso()
            return user.model_copy(deep=True)

        return await self.store.mutate(mutate)

    def public_watchlist_users(self, exclude_user_id: int | None = None) -> list[UserProfile]:
        values = [
            user.model_copy(deep=True)
            for user in self.store.state.users.values()
            if user.status == UserStatus.ACTIVE and user.telegram_user_id != exclude_user_id
        ]
        return sorted(
            values,
            key=lambda user: (
                (user.watchlist_display_name or user.first_name).casefold(),
                user.telegram_user_id,
            ),
        )

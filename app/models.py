from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .utils import utcnow_iso


class CategoryMode(str, Enum):
    SINGLE = "single"
    EPISODIC = "episodic"
    MIXED = "mixed"


class ContentKind(str, Enum):
    MOVIE = "movie"
    SERIES = "series"


class RecordKind(str, Enum):
    MOVIE = "movie"
    EPISODE = "episode"
    SEASON_PACK_PART = "season_pack_part"


class MediaType(str, Enum):
    VIDEO = "video"
    DOCUMENT = "document"


class WatchStatus(str, Enum):
    TO_WATCH = "to_watch"
    ON_HOLD = "on_hold"
    COMPLETED = "completed"


class UserStatus(str, Enum):
    ACTIVE = "active"
    PENDING = "pending"
    SUSPENDED = "suspended"
    BANNED = "banned"


class AccessMode(str, Enum):
    PUBLIC = "public"
    APPROVAL = "approval"
    ALLOWLIST = "allowlist"


class Category(BaseModel):
    id: str
    name: str
    slug: str
    active_channel_id: int
    channel_title: str | None = None
    legacy_channel_ids: list[int] = Field(default_factory=list)
    mode: CategoryMode = CategoryMode.MIXED
    enabled: bool = True
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)


class ContentRecord(BaseModel):
    id: str
    group_key: str
    category_id: str
    title: str
    normalized_title: str
    year: int | None = None
    kind: ContentKind
    file_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)


class FileRecord(BaseModel):
    id: str
    content_id: str
    category_id: str
    source_chat_id: int
    source_message_id: int
    telegram_file_id: str
    telegram_file_unique_id: str
    media_type: MediaType
    record_kind: RecordKind
    title: str
    year: int | None = None
    languages: list[str] = Field(default_factory=list)
    quality: str | None = None
    season: int | None = None
    episode: int | None = None
    pack_part: int | None = None
    available: bool = True
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)


class IndexFailure(BaseModel):
    source_chat_id: int
    source_message_id: int
    category_id: str
    reason: str
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)


class AuditEvent(BaseModel):
    id: str
    action: str
    actor_id: int | None = None
    details: str
    created_at: str = Field(default_factory=utcnow_iso)


class VersionedState(BaseModel):
    revision: int = 0
    updated_at: str = Field(default_factory=utcnow_iso)


class RemovedSourceRecord(BaseModel):
    source_chat_id: int
    source_message_id: int
    content_title: str
    telegram_deleted: bool = False
    removed_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)


class CatalogState(VersionedState):
    schema_version: int = 2
    kind: str = "catalog"
    categories: dict[str, Category] = Field(default_factory=dict)
    contents: dict[str, ContentRecord] = Field(default_factory=dict)
    files: dict[str, FileRecord] = Field(default_factory=dict)
    source_lookup: dict[str, str] = Field(default_factory=dict)
    content_lookup: dict[str, str] = Field(default_factory=dict)
    removed_sources: dict[str, RemovedSourceRecord] = Field(default_factory=dict)
    failures: dict[str, IndexFailure] = Field(default_factory=dict)
    audit_events: list[AuditEvent] = Field(default_factory=list)


class WatchlistEntry(BaseModel):
    id: str = ""
    content_id: str | None = None
    title: str
    year: int | None = None
    category_id: str
    category_name: str
    status: WatchStatus
    added_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)


class DeliveryTopicRef(BaseModel):
    message_thread_id: int
    name: str


class UserProfile(BaseModel):
    telegram_user_id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    status: UserStatus
    watchlist_public: bool = True
    watchlist_display_name: str | None = None
    watchlist: dict[str, WatchlistEntry] = Field(default_factory=dict)
    panel_dashboard_message_id: int | None = None
    panel_workspace_message_id: int | None = None
    panel_workspace_is_receipt: bool = False
    delivery_topic_id: int | None = None
    delivery_topics: dict[str, DeliveryTopicRef] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)
    last_seen_at: str = Field(default_factory=utcnow_iso)


class UsersState(VersionedState):
    schema_version: int = 6
    kind: str = "users"
    access_mode: AccessMode = AccessMode.PUBLIC
    users: dict[str, UserProfile] = Field(default_factory=dict)

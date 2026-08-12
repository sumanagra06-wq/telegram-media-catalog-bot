from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import logging
from collections.abc import Callable
from typing import Any, Generic, Protocol, TypeVar

from aiogram import Bot
from aiogram.types import BufferedInputFile
from pydantic import BaseModel

from .models import VersionedState
from .utils import utcnow_iso

LOGGER = logging.getLogger(__name__)
MANIFEST_MARKER = "TDB_MANIFEST_V1"
MAX_COMPRESSED_SNAPSHOT_BYTES = 18 * 1024 * 1024

StateT = TypeVar("StateT", bound=VersionedState)
ResultT = TypeVar("ResultT")


class StorageError(RuntimeError):
    """Raised when a Telegram-backed snapshot cannot be committed or restored."""


class SnapshotRef(BaseModel):
    file_id: str
    message_id: int
    revision: int
    checksum_sha256: str
    compressed_size: int
    created_at: str


class SnapshotManifest(BaseModel):
    marker: str = MANIFEST_MARKER
    kind: str
    schema_version: int = 1
    current: SnapshotRef
    previous: SnapshotRef | None = None
    updated_at: str


class SnapshotBackend(Protocol):
    kind: str

    async def load(self) -> dict[str, Any] | None: ...

    async def commit(self, payload: dict[str, Any], revision: int) -> None: ...


class TelegramSnapshotBackend:
    """Versioned gzip snapshots addressed by one pinned manifest message.

    A commit uploads the new immutable document first and only then edits the manifest.
    A crash before the manifest edit leaves the previous revision authoritative.
    """

    def __init__(self, bot: Bot, channel_id: int, kind: str) -> None:
        self.bot = bot
        self.channel_id = channel_id
        self.kind = kind
        self.manifest: SnapshotManifest | None = None
        self.manifest_message_id: int | None = None

    @staticmethod
    def _manifest_text(manifest: SnapshotManifest) -> str:
        body = manifest.model_dump_json(exclude_none=True)
        return f"{MANIFEST_MARKER}\n{body}"

    @staticmethod
    def _parse_manifest(text: str | None) -> SnapshotManifest:
        if not text or not text.startswith(f"{MANIFEST_MARKER}\n"):
            raise StorageError(
                "The most recent pinned database message is not a valid database manifest. "
                "Unpin unrelated messages and restore the bot manifest."
            )
        try:
            return SnapshotManifest.model_validate_json(text.split("\n", 1)[1])
        except Exception as exc:  # pydantic gives detailed errors, wrapped for operators
            raise StorageError("The pinned database manifest is invalid") from exc

    async def _download_ref(self, ref: SnapshotRef) -> dict[str, Any]:
        destination = io.BytesIO()
        try:
            await self.bot.download(ref.file_id, destination=destination)
        except Exception as exc:
            raise StorageError(f"Could not download {self.kind} snapshot r{ref.revision}") from exc
        compressed = destination.getvalue()
        checksum = hashlib.sha256(compressed).hexdigest()
        if checksum != ref.checksum_sha256:
            raise StorageError(
                f"Checksum mismatch for {self.kind} snapshot r{ref.revision}: "
                f"expected {ref.checksum_sha256}, received {checksum}"
            )
        try:
            raw = gzip.decompress(compressed)
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise StorageError(f"Could not decode {self.kind} snapshot r{ref.revision}") from exc
        if payload.get("kind") != self.kind:
            raise StorageError(f"Expected a {self.kind} snapshot, received {payload.get('kind')!r}")
        if int(payload.get("revision", -1)) != ref.revision:
            raise StorageError("Snapshot revision does not match its manifest")
        return payload

    async def load(self) -> dict[str, Any] | None:
        chat = await self.bot.get_chat(self.channel_id)
        pinned = chat.pinned_message
        if pinned is None:
            return None
        manifest = self._parse_manifest(pinned.text)
        if manifest.kind != self.kind:
            raise StorageError(
                f"Database channel {self.channel_id} contains {manifest.kind!r}, "
                f"but {self.kind!r} was expected"
            )
        self.manifest = manifest
        self.manifest_message_id = pinned.message_id
        try:
            return await self._download_ref(manifest.current)
        except StorageError:
            if manifest.previous is None:
                raise
            LOGGER.exception("Current %s snapshot failed; attempting previous revision", self.kind)
            payload = await self._download_ref(manifest.previous)
            # Promote the verified fallback so subsequent restarts do not retry a bad file.
            repaired = SnapshotManifest(
                kind=self.kind,
                schema_version=manifest.schema_version,
                current=manifest.previous,
                previous=None,
                updated_at=utcnow_iso(),
            )
            await self.bot.edit_message_text(
                chat_id=self.channel_id,
                message_id=pinned.message_id,
                text=self._manifest_text(repaired),
                parse_mode=None,
            )
            self.manifest = repaired
            return payload

    async def commit(self, payload: dict[str, Any], revision: int) -> None:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        compressed = gzip.compress(raw, compresslevel=9, mtime=0)
        if len(compressed) > MAX_COMPRESSED_SNAPSHOT_BYTES:
            raise StorageError(
                f"The compressed {self.kind} snapshot is {len(compressed)} bytes. "
                "It must be sharded before reaching Telegram's download limit."
            )

        checksum = hashlib.sha256(compressed).hexdigest()
        snapshot_message = await self.bot.send_document(
            chat_id=self.channel_id,
            document=BufferedInputFile(
                compressed,
                filename=f"{self.kind}-r{revision}.json.gz",
            ),
            caption=f"{self.kind} database snapshot • revision {revision}",
            disable_notification=True,
        )
        if snapshot_message.document is None:
            raise StorageError("Telegram did not return a document for the uploaded snapshot")

        new_ref = SnapshotRef(
            file_id=snapshot_message.document.file_id,
            message_id=snapshot_message.message_id,
            revision=revision,
            checksum_sha256=checksum,
            compressed_size=len(compressed),
            created_at=utcnow_iso(),
        )
        old_previous = self.manifest.previous if self.manifest else None
        new_manifest = SnapshotManifest(
            kind=self.kind,
            schema_version=int(payload.get("schema_version", 1)),
            current=new_ref,
            previous=self.manifest.current if self.manifest else None,
            updated_at=utcnow_iso(),
        )

        try:
            if self.manifest_message_id is None:
                pointer = await self.bot.send_message(
                    chat_id=self.channel_id,
                    text=self._manifest_text(new_manifest),
                    parse_mode=None,
                    disable_notification=True,
                )
                self.manifest_message_id = pointer.message_id
                await self.bot.pin_chat_message(
                    chat_id=self.channel_id,
                    message_id=pointer.message_id,
                    disable_notification=True,
                )
            else:
                await self.bot.edit_message_text(
                    chat_id=self.channel_id,
                    message_id=self.manifest_message_id,
                    text=self._manifest_text(new_manifest),
                    parse_mode=None,
                )
        except Exception:
            # The unreferenced snapshot is safe to remove; the old manifest is still valid.
            try:
                await self.bot.delete_message(self.channel_id, snapshot_message.message_id)
            except Exception:
                LOGGER.warning("Could not remove orphaned snapshot message", exc_info=True)
            raise

        # The manifest edit/pin response is the commit point. Verification is advisory: raising
        # after Telegram has committed would leave in-memory state behind the durable revision.
        try:
            chat = await self.bot.get_chat(self.channel_id)
            if (
                chat.pinned_message is None
                or chat.pinned_message.message_id != self.manifest_message_id
            ):
                LOGGER.error(
                    "The %s manifest is not the most recent pinned message. Unpin unrelated "
                    "database-channel messages before the next restart.",
                    self.kind,
                )
        except Exception:
            LOGGER.warning("Could not verify the pinned %s manifest", self.kind, exc_info=True)

        self.manifest = new_manifest
        if old_previous is not None:
            try:
                await self.bot.delete_message(self.channel_id, old_previous.message_id)
            except Exception:
                LOGGER.warning("Could not delete an old snapshot backup", exc_info=True)


class MemorySnapshotBackend:
    """Deterministic backend used by unit tests."""

    def __init__(self, kind: str, initial: dict[str, Any] | None = None) -> None:
        self.kind = kind
        self.payload = initial
        self.commits: list[dict[str, Any]] = []
        self.fail_next_commit = False

    async def load(self) -> dict[str, Any] | None:
        if self.payload is None:
            return None
        return json.loads(json.dumps(self.payload))

    async def commit(self, payload: dict[str, Any], revision: int) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise StorageError("Injected commit failure")
        copied = json.loads(json.dumps(payload))
        self.payload = copied
        self.commits.append(copied)


class StateStore(Generic[StateT]):
    def __init__(
        self,
        backend: SnapshotBackend,
        model_type: type[StateT],
        default_factory: Callable[[], StateT],
    ) -> None:
        self.backend = backend
        self.model_type = model_type
        self.default_factory = default_factory
        self._state: StateT | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> StateT:
        if self._state is None:
            raise StorageError(f"{self.backend.kind} state has not been initialized")
        return self._state

    async def initialize(self) -> StateT:
        async with self._lock:
            payload = await self.backend.load()
            if payload is None:
                state = self.default_factory()
                await self.backend.commit(state.model_dump(mode="json"), state.revision)
            else:
                state = self.model_type.model_validate(payload)
            self._state = state
            return state.model_copy(deep=True)

    def snapshot(self) -> StateT:
        return self.state.model_copy(deep=True)

    async def mutate(
        self,
        mutator: Callable[[StateT], ResultT],
    ) -> ResultT:
        async with self._lock:
            current = self.state
            draft = current.model_copy(deep=True)
            result = mutator(draft)
            draft.revision = current.revision + 1
            draft.updated_at = utcnow_iso()
            await self.backend.commit(draft.model_dump(mode="json"), draft.revision)
            self._state = draft
            return result

    def export_gzip(self) -> bytes:
        raw = json.dumps(
            self.state.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return gzip.compress(raw, compresslevel=9, mtime=0)

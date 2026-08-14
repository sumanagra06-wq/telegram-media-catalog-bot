from __future__ import annotations

import asyncio
import logging
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from .models import ContentRecord, FileRecord
from .repositories import CatalogRepository, FileUpsertRequest
from .utils import compact_label, safe_html

LOGGER = logging.getLogger(__name__)

UpsertResult = tuple[FileRecord, ContentRecord, bool]


@dataclass
class _PendingUpsert:
    request: FileUpsertRequest
    future: asyncio.Future[UpsertResult]


class CatalogIngestBatcher:
    """Coalesce a rapid channel-post burst into a small number of durable snapshots."""

    def __init__(
        self,
        catalog: CatalogRepository,
        *,
        delay_seconds: float = 0.75,
        max_batch_size: int = 100,
    ) -> None:
        self.catalog = catalog
        self.delay_seconds = max(delay_seconds, 0.0)
        self.max_batch_size = max(max_batch_size, 1)
        self._pending: list[_PendingUpsert] = []
        self._guard = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        self._scheduled: asyncio.Task[None] | None = None
        self._closed = False

    def pending_count(self) -> int:
        return len(self._pending)

    async def submit(self, request: FileUpsertRequest) -> UpsertResult:
        if self._closed:
            raise RuntimeError("Catalog ingestion is shutting down")
        future: asyncio.Future[UpsertResult] = asyncio.get_running_loop().create_future()
        # A canceled webhook waiter must not turn a later durable-write failure into an
        # unobserved Future warning; awaiting callers still receive the stored exception.
        future.add_done_callback(lambda item: None if item.cancelled() else item.exception())
        async with self._guard:
            if self._closed:
                raise RuntimeError("Catalog ingestion is shutting down")
            self._pending.append(_PendingUpsert(request=request, future=future))
            if len(self._pending) >= self.max_batch_size:
                if self._scheduled is not None:
                    self._scheduled.cancel()
                self._scheduled = asyncio.create_task(self._flush())
            elif self._scheduled is None:
                self._scheduled = asyncio.create_task(self._flush_after_delay())
        # The durable write must continue even if Telegram cancels one webhook request.
        return await asyncio.shield(future)

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(self.delay_seconds)
            await self._flush()
        except asyncio.CancelledError:
            return

    async def _flush(self) -> None:
        async with self._flush_lock:
            current = asyncio.current_task()
            async with self._guard:
                if self._scheduled is current:
                    self._scheduled = None
                batch = self._pending
                self._pending = []
            if not batch:
                return
            try:
                results = await self.catalog.upsert_files([item.request for item in batch])
            except Exception as exc:
                LOGGER.exception("Catalog ingestion batch of %s files failed", len(batch))
                for item in batch:
                    if not item.future.done():
                        item.future.set_exception(exc)
            else:
                for item, result in zip(batch, results, strict=True):
                    if not item.future.done():
                        item.future.set_result(result)
                LOGGER.info(
                    "Committed catalog ingestion batch: files=%s revision=%s",
                    len(batch),
                    self.catalog.revision(),
                )
            finally:
                async with self._guard:
                    if self._pending and self._scheduled is None and not self._closed:
                        self._scheduled = asyncio.create_task(self._flush_after_delay())

    async def shutdown(self) -> None:
        async with self._guard:
            self._closed = True
            scheduled = self._scheduled
        if scheduled is not None:
            with suppress(asyncio.CancelledError):
                await scheduled
        await self._flush()


@dataclass(frozen=True)
class IndexAuditEntry:
    detail_text: str
    created: bool
    category_name: str
    title: str
    source_message_id: int
    season: int | None = None
    episode: int | None = None
    episode_start: int | None = None
    episode_end: int | None = None


class IndexAuditBatcher:
    """Rate-limit human-readable audit cards while durable per-file events stay in snapshots."""

    def __init__(
        self,
        bot: Bot,
        channel_id: int,
        *,
        delay_seconds: float = 1.5,
        max_batch_size: int = 100,
    ) -> None:
        self.bot = bot
        self.channel_id = channel_id
        self.delay_seconds = max(delay_seconds, 0.0)
        self.max_batch_size = max(max_batch_size, 1)
        self._pending: list[IndexAuditEntry] = []
        self._guard = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        self._scheduled: asyncio.Task[None] | None = None
        self._closed = False

    async def submit(self, entry: IndexAuditEntry) -> None:
        async with self._guard:
            if self._closed:
                return
            self._pending.append(entry)
            if len(self._pending) >= self.max_batch_size:
                if self._scheduled is not None:
                    self._scheduled.cancel()
                self._scheduled = asyncio.create_task(self._flush())
            elif self._scheduled is None:
                self._scheduled = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(self.delay_seconds)
            await self._flush()
        except asyncio.CancelledError:
            return

    @staticmethod
    def _entry_label(entry: IndexAuditEntry) -> str:
        location = ""
        if entry.episode is not None:
            season = f"S{entry.season:02d}" if entry.season is not None else ""
            location = f" • {season}E{entry.episode:02d}"
        elif entry.episode_start is not None and entry.episode_end is not None:
            season = f"S{entry.season:02d} " if entry.season is not None else ""
            location = f" • {season}E{entry.episode_start}–{entry.episode_end}"
        elif entry.season is not None:
            location = f" • Season {entry.season} pack"
        return (
            f"{'✅' if entry.created else '✏️'} <code>{entry.source_message_id}</code> • "
            f"{safe_html(compact_label(entry.title, 80))}{location}"
        )

    @classmethod
    def _render(cls, entries: list[IndexAuditEntry]) -> str:
        if len(entries) == 1:
            return entries[0].detail_text
        created = sum(item.created for item in entries)
        updated = len(entries) - created
        categories = Counter(item.category_name for item in entries)
        category_text = ", ".join(
            f"{safe_html(name)} ({count})" for name, count in sorted(categories.items())
        )
        samples = entries[:20]
        lines = [
            "✅ <b>CATALOG BURST INDEXED</b>",
            "<blockquote>One durable snapshot transaction</blockquote>",
            "━━━━━━━━━━━━━━━━━━",
            f"📦 <b>Files processed</b>  •  {len(entries)}",
            f"🆕 <b>New</b>  •  {created}",
            f"✏️ <b>Updated</b>  •  {updated}",
            f"🗂 <b>Categories</b>  •  {category_text}",
            "━━━━━━━━━━━━━━━━━━",
            *[cls._entry_label(item) for item in samples],
        ]
        if len(entries) > len(samples):
            lines.append(f"…and {len(entries) - len(samples)} more files.")
        lines.append("Every file retains its individual durable catalog audit event.")
        return "\n".join(lines)

    async def _send(self, text: str) -> None:
        for attempt in range(3):
            try:
                await self.bot.send_message(
                    self.channel_id,
                    text,
                    disable_notification=True,
                )
                return
            except TelegramRetryAfter as exc:
                if attempt == 2:
                    break
                delay = max(float(exc.retry_after), 0.1) + 0.1
            except (TelegramNetworkError, TelegramServerError):
                if attempt == 2:
                    break
                delay = 0.25 * (2**attempt)
            except TelegramAPIError:
                break
            await asyncio.sleep(delay)
        LOGGER.warning("Could not write a catalog ingestion audit summary")

    async def _flush(self) -> None:
        async with self._flush_lock:
            current = asyncio.current_task()
            async with self._guard:
                if self._scheduled is current:
                    self._scheduled = None
                entries = self._pending
                self._pending = []
            if not entries:
                return
            await self._send(self._render(entries))
            async with self._guard:
                if self._pending and self._scheduled is None and not self._closed:
                    self._scheduled = asyncio.create_task(self._flush_after_delay())

    async def shutdown(self) -> None:
        async with self._guard:
            self._closed = True
            scheduled = self._scheduled
        if scheduled is not None:
            with suppress(asyncio.CancelledError):
                await scheduled
        await self._flush()

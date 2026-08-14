from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .models import ContentKind, ContentRecord, FileRecord, RecordKind
from .repositories import CatalogRepository
from .utils import make_id, normalize_title, safe_html


@dataclass(frozen=True, slots=True)
class SearchHit:
    content: ContentRecord
    score: float


@dataclass(slots=True)
class SearchSession:
    token: str
    user_id: int
    query: str
    content_ids: list[str]
    created_at: float
    result_heading: str = "SEARCH RESULTS"
    selected_content_ids: set[str] = field(default_factory=set)
    alphabet_filter: str | None = None
    text_filter: str | None = None
    only_unsaved: bool = False
    selected_only: bool = False
    context: str = "catalog"


class SearchSessionStore:
    def __init__(self, ttl_seconds: int = 1800) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, SearchSession] = {}

    def create(
        self,
        user_id: int,
        query: str,
        content_ids: list[str],
        *,
        result_heading: str = "SEARCH RESULTS",
        context: str = "catalog",
    ) -> SearchSession:
        self.prune()
        token = make_id("q", 4).split("_", 1)[1]
        session = SearchSession(
            token,
            user_id,
            query,
            content_ids,
            time.monotonic(),
            result_heading=result_heading,
            context=context,
        )
        self._sessions[token] = session
        return session

    def get(self, token: str, user_id: int) -> SearchSession | None:
        session = self._sessions.get(token)
        if session is None or session.user_id != user_id:
            return None
        if time.monotonic() - session.created_at > self.ttl_seconds:
            self._sessions.pop(token, None)
            return None
        return session

    def prune(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        expired = [key for key, item in self._sessions.items() if item.created_at < cutoff]
        for key in expired:
            self._sessions.pop(key, None)


class CatalogQueryService:
    SEARCH_PAGE_SIZE = 4
    CONTENT_PAGE_SIZE = 6
    EPISODE_PAGE_SIZE = 20

    def __init__(self, catalog: CatalogRepository) -> None:
        self.catalog = catalog

    def _is_visible(self, content: ContentRecord) -> bool:
        return self.catalog.content_is_visible(content.id)

    def search(self, raw_query: str) -> list[SearchHit]:
        raw_query = " ".join(raw_query.split()).strip()
        year_match = re.search(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", raw_query)
        requested_year = int(year_match.group()) if year_match else None
        title_query = re.sub(r"(?<!\d)(?:19\d{2}|20\d{2})(?!\d)", " ", raw_query)
        query = normalize_title(title_query)
        if not query:
            return []

        hits: list[SearchHit] = []
        for content in self.catalog.list_contents():
            if not self._is_visible(content):
                continue
            candidate_values = [content.normalized_title]
            best = 0.0
            for candidate in candidate_values:
                if candidate == query:
                    score = 1000.0
                elif candidate.startswith(query):
                    score = 900.0 - min(len(candidate) - len(query), 100) / 10
                elif re.search(rf"\b{re.escape(query)}\b", candidate):
                    score = 825.0
                elif query in candidate:
                    score = 750.0 - min(candidate.index(query), 100) / 10
                else:
                    ratio = fuzz.WRatio(query, candidate)
                    score = 6.5 * ratio
                    if ratio < 48:
                        score = 0.0
                best = max(best, score)
            if best <= 0:
                continue
            if requested_year is not None:
                if content.year == requested_year:
                    best += 120
                elif content.year is not None:
                    best -= 80
            hits.append(SearchHit(content.model_copy(deep=True), best))

        hits.sort(
            key=lambda item: (
                -item.score,
                item.content.title.casefold(),
                item.content.year or 0,
            )
        )
        return hits

    def browse_category(self, category_id: str) -> list[ContentRecord]:
        values = [
            item
            for item in self.catalog.list_contents()
            if item.category_id == category_id and self._is_visible(item)
        ]
        return sorted(values, key=lambda item: (item.title.casefold(), item.year or 0))

    def recently_added(self, limit: int = 30) -> list[ContentRecord]:
        values = [item for item in self.catalog.list_contents() if self._is_visible(item)]
        return sorted(values, key=lambda item: item.updated_at, reverse=True)[:limit]

    def files(self, content_id: str) -> list[FileRecord]:
        return self.catalog.files_for_content(content_id)

    def seasons(self, content_id: str) -> list[int]:
        return sorted({item.season for item in self.files(content_id) if item.season is not None})

    def episodes(self, content_id: str, season: int) -> list[int]:
        return sorted(
            {
                item.episode
                for item in self.files(content_id)
                if item.record_kind == RecordKind.EPISODE
                and item.season == season
                and item.episode is not None
            }
        )

    def episode_variants(self, content_id: str, season: int, episode: int) -> list[FileRecord]:
        values = [
            item
            for item in self.files(content_id)
            if item.record_kind == RecordKind.EPISODE
            and item.season == season
            and item.episode == episode
        ]
        return self._sort_variants(values)

    def season_pack_parts(self, content_id: str, season: int) -> list[FileRecord]:
        values = [
            item
            for item in self.files(content_id)
            if item.record_kind == RecordKind.SEASON_PACK_PART and item.season == season
        ]
        return sorted(values, key=lambda item: item.pack_part or 0)

    def movie_variants(self, content_id: str) -> list[FileRecord]:
        values = [item for item in self.files(content_id) if item.record_kind == RecordKind.MOVIE]
        return self._sort_variants(values)

    @staticmethod
    def _sort_variants(values: list[FileRecord]) -> list[FileRecord]:
        quality_order = {"2160p": 0, "1080p": 1, "720p": 2, "480p": 3, "360p": 4}
        return sorted(
            values,
            key=lambda item: (
                quality_order.get(item.quality or "", 99),
                ", ".join(item.languages),
                item.id,
            ),
        )

    def aggregates(self, content_id: str) -> tuple[list[str], list[str]]:
        files = self.files(content_id)
        languages = sorted({language for item in files for language in item.languages})
        qualities = sorted(
            {item.quality for item in files if item.quality},
            key=lambda quality: int(re.sub(r"\D", "", quality) or 0),
            reverse=True,
        )
        return languages, qualities


def variant_label(file: FileRecord) -> str:
    language = " + ".join(file.languages) if file.languages else "Unknown language"
    quality = file.quality or "Unknown quality"
    return f"{language} • {quality}"


def delivery_caption(file: FileRecord, kind: ContentKind, category_name: str) -> str:
    icon = "📺" if kind == ContentKind.SERIES else "🎬"
    lines = [
        f"{icon} <b>{safe_html(file.title)}</b>",
        f"<blockquote>{safe_html(category_name)} collection • temporary delivery</blockquote>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if file.record_kind == RecordKind.EPISODE:
        lines.append(f"▶️ <b>Episode</b>  •  Season {file.season}, Episode {file.episode}")
    elif file.record_kind == RecordKind.SEASON_PACK_PART:
        if file.episode_start is not None and file.episode_end is not None:
            lines.append(
                f"📦 <b>Episode pack</b>  •  Season {file.season}, "
                f"Episodes {file.episode_start}–{file.episode_end}"
            )
        else:
            lines.append(
                f"📦 <b>Season pack</b>  •  Season {file.season}, Part {file.pack_part or 1}"
            )
    lines.extend(
        [
            f"📅 <b>Year</b>  •  {file.year or 'Unknown'}",
            "🗣 <b>Language</b>  •  "
            + safe_html(", ".join(file.languages) if file.languages else "Unknown"),
            "💎 <b>Quality</b>  •  " + safe_html(file.quality or "Unknown"),
            "━━━━━━━━━━━━━━━━━━",
            "🔓 Save or forward now • this bot-chat copy expires in 5 minutes.",
        ]
    )
    return "\n".join(lines)

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field


class ParsedMetadata(BaseModel):
    title: str
    year: int | None = None
    languages: list[str] = Field(default_factory=list)
    quality: str | None = None
    season: int | None = None
    episode: int | None = None
    pack_part: int | None = None


class MetadataParseError(ValueError):
    """Raised when allowlisted media metadata cannot be extracted safely."""


@dataclass(frozen=True)
class _Language:
    canonical: str
    aliases: tuple[str, ...]


_LANGUAGES = (
    _Language("Hindi", ("hindi", "hin")),
    _Language("English", ("english", "eng")),
    _Language("Tamil", ("tamil", "tam")),
    _Language("Telugu", ("telugu", "tel")),
    _Language("Malayalam", ("malayalam", "mal")),
    _Language("Kannada", ("kannada", "kan")),
    _Language("Bengali", ("bengali", "bangla", "ben")),
    _Language("Marathi", ("marathi", "mar")),
    _Language("Punjabi", ("punjabi", "panjabi", "pun")),
    _Language("Gujarati", ("gujarati", "guj")),
    _Language("Urdu", ("urdu", "urd")),
    _Language("Japanese", ("japanese", "jpn")),
    _Language("Korean", ("korean", "kor")),
    _Language("Spanish", ("spanish", "spa")),
    _Language("French", ("french", "fre", "fra")),
    _Language("German", ("german", "ger", "deu")),
)

_WARNING_RE = re.compile(
    r"(?i)(automatically\s*delete|delete\s*after|forward\s+in\s+another|"
    r"please\s+forward|join\s+@|https?://|t\.me/)"
)
_MENTION_RE = re.compile(r"^(?:\s*@\w+\s+)+", re.UNICODE)
_SEASON_EPISODE_RE = re.compile(
    r"(?i)\bS(?:eason)?\s*0*(?P<season>\d{1,3})\s*[-._ ]*"
    r"E(?:p(?:isode)?)?\s*0*(?P<episode>\d{1,4})\b"
)
_SEASON_RE = re.compile(r"(?i)\bS(?:eason)?\s*0*(?P<season>\d{1,3})\b")
_EPISODE_RE = re.compile(r"(?i)\bE(?:p(?:isode)?)?\s*0*(?P<episode>\d{1,4})\b")
_YEAR_RE = re.compile(r"(?<!\d)(?P<year>19\d{2}|20\d{2})(?!\d)")
_QUALITY_RE = re.compile(r"(?i)(?<!\w)(2160p|1080p|720p|480p|360p|4k|uhd|fhd)(?!\w)")
_PACK_PART_RE = re.compile(
    r"(?i)(?:\.zip)+\.(?P<part>\d{3})(?:\D|$)|\bpart[ ._-]*0*(?P<part2>\d+)\b"
)

_LABELS = {
    "title": ("title", "name", "movie", "series", "show"),
    "year": ("year", "release year", "released"),
    "language": ("language", "languages", "audio", "dual audio"),
    "quality": ("quality", "resolution", "video quality"),
    "season": ("season", "season no", "season number"),
    "episode": ("episode", "episode no", "episode number", "ep"),
}

_TECHNICAL_BOUNDARY_RE = re.compile(
    r"(?i)\b(?:web[ ._-]*dl|webrip|blu[ ._-]*ray|brrip|dvdrip|hdrip|"
    r"x26[45]|hevc|avc|10[ ._-]*bit|esubs?|aac|ddp?|atmos)\b"
)


def _labeled_value(text: str, names: tuple[str, ...]) -> str | None:
    joined = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    pattern = re.compile(rf"(?im)^\s*[^\w@]*\s*(?:{joined})\s*[:=\-]\s*(?P<value>.+?)\s*$")
    match = pattern.search(text)
    return match.group("value").strip() if match else None


def _metadata_lines(caption: str | None, filename: str | None) -> list[str]:
    lines: list[str] = []
    if caption:
        for raw in caption.splitlines():
            line = raw.strip()
            if line and not _WARNING_RE.search(line):
                lines.append(line)
    if filename and not any(filename.strip() == line for line in lines):
        lines.append(filename.strip())
    return lines


def _primary_line(lines: list[str]) -> str:
    if not lines:
        raise MetadataParseError("No caption or filename was provided")
    # Prefer a line containing recognizable media metadata over labels or advertisements.
    for line in lines:
        if _SEASON_RE.search(line) or _QUALITY_RE.search(line) or _YEAR_RE.search(line):
            return line
    return lines[0]


def _normalize_working_line(line: str) -> str:
    line = _MENTION_RE.sub("", line.strip())
    line = re.sub(r"^[^\w@]+", "", line, flags=re.UNICODE)
    line = line.replace("_", " ").replace(".", " ")
    return " ".join(line.split())


def _extract_languages(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for language in _LANGUAGES:
        best: int | None = None
        for alias in language.aliases:
            match = re.search(rf"(?i)(?<!\w){re.escape(alias)}(?!\w)", text)
            if match and (best is None or match.start() < best):
                best = match.start()
        if best is not None:
            found.append((best, language.canonical))
    return [name for _, name in sorted(found)]


def _extract_quality(text: str) -> str | None:
    match = _QUALITY_RE.search(text)
    if not match:
        return None
    value = match.group(1).casefold()
    return {"4k": "2160p", "uhd": "2160p", "fhd": "1080p"}.get(value, value)


def _extract_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def _clean_title(value: str) -> str:
    value = _MENTION_RE.sub("", value.strip())
    value = re.sub(r"^[^\w@]+", "", value, flags=re.UNICODE)
    value = value.replace("_", " ").replace(".", " ")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[\s|:\-–—]+$", "", value)
    return value.strip()


def _title_from_primary(primary: str) -> str:
    working = _normalize_working_line(primary)
    boundaries: list[int] = []
    for pattern in (_SEASON_EPISODE_RE, _SEASON_RE, _YEAR_RE, _QUALITY_RE, _TECHNICAL_BOUNDARY_RE):
        match = pattern.search(working)
        if match:
            boundaries.append(match.start())
    if not boundaries:
        # Remove common trailing extensions only when no metadata boundary exists.
        working = re.sub(r"(?i)(?:\s+(?:mkv|mp4|avi|mov|zip|rar|7z))+$", "", working)
        title = _clean_title(working)
    else:
        title = _clean_title(working[: min(boundaries)])

    # Dot/underscore-separated filenames commonly capitalize every word. Make connector
    # words natural without changing acronyms such as LOST.
    if "." in primary or "_" in primary:
        connectors = {"a", "an", "and", "at", "for", "in", "of", "on", "or", "the", "to"}
        words = title.split()
        title = " ".join(
            word.casefold() if index and word.casefold() in connectors else word
            for index, word in enumerate(words)
        )
    return title


def parse_metadata(caption: str | None, filename: str | None = None) -> ParsedMetadata:
    lines = _metadata_lines(caption, filename)
    if not lines:
        raise MetadataParseError("No caption or filename was provided")
    combined = "\n".join(lines)
    primary = _primary_line(lines)
    scan_text = _normalize_working_line(" ".join(lines))

    explicit_title = _labeled_value(combined, _LABELS["title"])
    if explicit_title:
        title = _clean_title(explicit_title)
    else:
        first_working = _normalize_working_line(lines[0])
        first_has_metadata = any(
            pattern.search(first_working)
            for pattern in (_SEASON_RE, _YEAR_RE, _QUALITY_RE, _TECHNICAL_BOUNDARY_RE)
        )
        first_is_non_title_label = bool(
            re.match(
                r"(?i)^\s*(?:year|release year|released|language|languages|audio|"
                r"quality|resolution|season|episode|ep)\s*[:=\-]",
                first_working,
            )
        )
        title = (
            _clean_title(lines[0])
            if len(lines) > 1 and not first_has_metadata and not first_is_non_title_label
            else _title_from_primary(primary)
        )
    if not title:
        raise MetadataParseError("Could not determine the title")

    season_episode = _SEASON_EPISODE_RE.search(scan_text)
    season_label = _extract_int(_labeled_value(combined, _LABELS["season"]))
    episode_label = _extract_int(_labeled_value(combined, _LABELS["episode"]))
    season_match = _SEASON_RE.search(scan_text)
    episode_match = _EPISODE_RE.search(scan_text)

    season = season_label
    episode = episode_label
    if season_episode:
        season = season or int(season_episode.group("season"))
        episode = episode or int(season_episode.group("episode"))
    else:
        if season is None and season_match:
            season = int(season_match.group("season"))
        if episode is None and episode_match:
            episode = int(episode_match.group("episode"))

    year_value = _labeled_value(combined, _LABELS["year"])
    year = _extract_int(year_value)
    if year is None:
        year_match = _YEAR_RE.search(scan_text)
        year = int(year_match.group("year")) if year_match else None

    language_value = _labeled_value(combined, _LABELS["language"])
    languages = _extract_languages(language_value or scan_text)
    quality_value = _labeled_value(combined, _LABELS["quality"])
    quality = _extract_quality(quality_value or scan_text)

    pack_match = _PACK_PART_RE.search(combined)
    pack_part = None
    if pack_match:
        pack_part = int(pack_match.group("part") or pack_match.group("part2"))

    return ParsedMetadata(
        title=title,
        year=year,
        languages=languages,
        quality=quality,
        season=season,
        episode=episode,
        pack_part=pack_part,
    )

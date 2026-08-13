from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TypeVar

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .models import (
    AccessMode,
    Category,
    ContentKind,
    ContentRecord,
    FileRecord,
    UserProfile,
    UserStatus,
    WatchlistEntry,
    WatchStatus,
)
from .presentation import ActionButton as InlineKeyboardButton
from .services import CatalogQueryService, SearchSession, variant_label
from .utils import compact_label, normalize_title, safe_html

WATCH_CODES = {
    WatchStatus.TO_WATCH: "t",
    WatchStatus.ON_HOLD: "h",
    WatchStatus.COMPLETED: "c",
}
CODE_WATCH = {value: key for key, value in WATCH_CODES.items()}
ItemT = TypeVar("ItemT")
DIVIDER = "━━━━━━━━━━━━━━━━━━"


def _page_line(page: int, pages: int, total: int | None = None) -> str:
    total_text = f"  •  {total} items" if total is not None else ""
    return f"<code>Page {page + 1}/{pages}</code>{total_text}"


def page_slice(values: Sequence[ItemT], page: int, page_size: int) -> tuple[list[ItemT], int, int]:
    pages = max(1, math.ceil(len(values) / page_size))
    page = max(0, min(page, pages - 1))
    start = page * page_size
    return list(values[start : start + page_size]), page, pages


def main_dashboard(
    is_owner: bool, first_name: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    greeting = f", {safe_html(first_name)}" if first_name else ""
    text = (
        "✨ <b>MEDIA LIBRARY</b>\n"
        "<blockquote>Your private cinema, organized and ready.</blockquote>\n"
        f"👋 <b>Welcome{greeting}</b>\n\n"
        f"{DIVIDER}\n"
        "🔎 <b>Instant title search</b>\n"
        "Send any movie or series name directly in this chat.\n\n"
        "💡 <b>Try:</b> <code>Dark</code>  •  <code>Dune</code>  •  "
        "<code>Interstellar 2014</code>\n"
        f"{DIVIDER}\n"
        "🔐 Files are delivered with Telegram content protection."
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔎 Search", callback_data="menu:search", style="primary"),
        InlineKeyboardButton(text="🗂 Browse library", callback_data="menu:browse"),
    )
    builder.row(
        InlineKeyboardButton(text="✨ Recently added", callback_data="menu:recent"),
        InlineKeyboardButton(text="📚 Watchlist", callback_data="menu:watchlist", style="primary"),
    )
    builder.row(InlineKeyboardButton(text="❓ Help & tips", callback_data="menu:help"))
    if is_owner:
        builder.row(
            InlineKeyboardButton(
                text="🛡 Open admin control center",
                callback_data="admin:home",
                style="primary",
            )
        )
    return text, builder.as_markup()


def panel_dashboard(
    is_owner: bool,
    first_name: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    greeting = f", {safe_html(first_name)}" if first_name else ""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔎 Search", callback_data="p:search", style="primary"),
        InlineKeyboardButton(text="🗂 Browse", callback_data="p:browse", style="primary"),
    )
    builder.row(
        InlineKeyboardButton(text="✨ Recently added", callback_data="p:recent"),
        InlineKeyboardButton(text="📚 Watchlist", callback_data="p:watchlist", style="primary"),
    )
    builder.row(InlineKeyboardButton(text="❓ Help & tips", callback_data="p:help"))
    if is_owner:
        builder.row(
            InlineKeyboardButton(
                text="🛡 Admin Control Center",
                callback_data="p:admin",
                style="primary",
            )
        )
    return (
        (
            "✨ <b>MEDIA LIBRARY DASHBOARD</b>\n"
            "<blockquote>Pinned home • one clean workspace</blockquote>\n"
            f"👋 <b>Welcome{greeting}</b>\n\n"
            f"{DIVIDER}\n"
            "🔎 Search instantly or browse the complete catalog.\n"
            "☑️ Select several titles and save them together.\n"
            "⏱ Your temporary workspace closes after 5 quiet minutes.\n"
            f"{DIVIDER}\n"
            "💡 Send a title directly in chat whenever you prefer."
        ),
        builder.as_markup(),
    )


def panel_workspace_home(is_owner: bool) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔎 Search", callback_data="p:search", style="primary"),
        InlineKeyboardButton(text="🗂 Browse", callback_data="p:browse", style="primary"),
    )
    builder.row(
        InlineKeyboardButton(text="✨ Recently added", callback_data="p:recent"),
        InlineKeyboardButton(text="📚 Watchlist", callback_data="p:watchlist", style="primary"),
    )
    if is_owner:
        builder.row(
            InlineKeyboardButton(
                text="🛡 Admin Control Center",
                callback_data="p:admin",
                style="primary",
            )
        )
    builder.row(
        InlineKeyboardButton(text="❓ Help", callback_data="p:help"),
        InlineKeyboardButton(text="✖️ Close workspace", callback_data="p:close"),
    )
    return (
        (
            "🧭 <b>TEMPORARY WORKSPACE</b>\n"
            "<blockquote>Your pinned dashboard stays unchanged above.</blockquote>\n"
            f"{DIVIDER}\n"
            "Choose an action. Every screen will reuse this card.\n\n"
            "⏱ It closes automatically after 5 minutes without interaction."
        ),
        builder.as_markup(),
    )


def post_delivery_dashboard(is_owner: bool) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔎 Search", callback_data="p:search", style="primary"),
        InlineKeyboardButton(text="🗂 Browse", callback_data="p:browse", style="primary"),
    )
    builder.row(
        InlineKeyboardButton(text="✨ Recently added", callback_data="p:recent"),
        InlineKeyboardButton(text="📚 Watchlist", callback_data="p:watchlist", style="primary"),
    )
    if is_owner:
        builder.row(
            InlineKeyboardButton(
                text="🛡 Admin Control Center",
                callback_data="p:admin",
                style="primary",
            )
        )
    builder.row(
        InlineKeyboardButton(text="❓ Help", callback_data="p:help"),
        InlineKeyboardButton(text="✖️ Close", callback_data="p:close"),
    )
    return (
        (
            "✨ <b>MEDIA LIBRARY DASHBOARD</b>\n"
            "<blockquote>Your file is above • what would you like next?</blockquote>\n"
            f"{DIVIDER}\n"
            "The controls were moved below the delivered file so you do not need to scroll back.\n\n"
            "⏱ This temporary dashboard closes after 5 quiet minutes."
        ),
        builder.as_markup(),
    )


def panel_browse_categories(categories: list[Category]) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    icons = {"single": "🎬", "episodic": "📺", "mixed": "🗂"}
    for category in categories:
        builder.row(
            InlineKeyboardButton(
                text=f"{icons[category.mode.value]} {compact_label(category.name)}",
                callback_data=f"pb:{category.id}",
                style="primary",
            )
        )
    builder.row(
        InlineKeyboardButton(text="◀️ Workspace", callback_data="p:home"),
        InlineKeyboardButton(text="✖️ Close", callback_data="p:close"),
    )
    text = (
        "🗂 <b>BROWSE & MULTI-SELECT</b>\n"
        "<blockquote>Explore a collection, then tick one or more titles.</blockquote>\n"
        f"{DIVIDER}\n"
        f"📚 Available collections: <b>{len(categories)}</b>\n\n"
        "Choose a collection:"
    )
    if not categories:
        text += "\n\n🫙 <i>No collections are available yet.</i>"
    return text, builder.as_markup()


def selectable_results(
    session: SearchSession,
    contents: list[ContentRecord],
    page: int,
    page_size: int = 4,
) -> tuple[str, InlineKeyboardMarkup]:
    visible, page, pages = page_slice(contents, page, page_size)
    selected = session.selected_content_ids
    builder = InlineKeyboardBuilder()
    for rank, content in enumerate(visible, start=page * page_size + 1):
        checked = content.id in selected
        builder.row(
            InlineKeyboardButton(
                text="✅" if checked else "☐",
                callback_data=f"px:{session.token}:{content.id}:{page}",
                style="success" if checked else None,
            ),
            InlineKeyboardButton(
                text=compact_label(
                    f"{rank}. {'📺' if content.kind == ContentKind.SERIES else '🎬'} "
                    f"{content.title} ({content.year or 'Unknown'})",
                    52,
                ),
                callback_data=f"ct:{content.id}:{session.token}:{page}",
                style="primary",
            ),
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="◀️ Previous", callback_data=f"sr:{session.token}:{page - 1}")
        )
    if page + 1 < pages:
        navigation.append(
            InlineKeyboardButton(text="Next ▶️", callback_data=f"sr:{session.token}:{page + 1}")
        )
    if navigation:
        builder.row(*navigation)
    builder.row(
        InlineKeyboardButton(
            text=f"➕ Add Selected · {len(selected)}",
            callback_data=f"pa:{session.token}:{page}",
            style="success" if selected else None,
        )
    )
    builder.row(
        InlineKeyboardButton(text="◀️ Workspace", callback_data="p:home"),
        InlineKeyboardButton(text="✖️ Close", callback_data="p:close"),
    )
    heading = session.result_heading
    if session.query == "Recently added":
        heading = "RECENTLY ADDED"
    return (
        (
            f"☑️ <b>{safe_html(heading)}</b>\n"
            f"<blockquote>Results for “{safe_html(session.query)}”</blockquote>\n"
            f"{DIVIDER}\n"
            f"🎯 Found: <b>{len(contents)}</b>  •  Selected: <b>{len(selected)}/25</b>\n"
            f"{_page_line(page, pages)}\n\n"
            "Tap ☐ to select titles, or tap a title to view and download it. "
            "Selections stay checked across pages."
        ),
        builder.as_markup(),
    )


def bulk_watchlist_status_picker(
    session: SearchSession,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    count = len(session.selected_content_ids)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📌 To watch",
            callback_data=f"pw:{session.token}:t:{page}",
            style="primary",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⏸ On hold",
            callback_data=f"pw:{session.token}:h:{page}",
            style="primary",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✅ Completed",
            callback_data=f"pw:{session.token}:c:{page}",
            style="success",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Back to selection",
            callback_data=f"sr:{session.token}:{page}",
        ),
        InlineKeyboardButton(text="✖️ Close", callback_data="p:close"),
    )
    return (
        (
            "📚 <b>ADD SELECTED TITLES</b>\n"
            f"<blockquote>{count} title{'s' if count != 1 else ''} selected</blockquote>\n"
            f"{DIVIDER}\n"
            "Choose one Watchlist status for the whole selection:\n\n"
            "📌 <b>To watch</b>  •  saved for later\n"
            "⏸ <b>On hold</b>  •  paused for now\n"
            "✅ <b>Completed</b>  •  already finished"
        ),
        builder.as_markup(),
    )


def browse_categories(categories: list[Category]) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    icons = {"single": "🎬", "episodic": "📺", "mixed": "🗂"}
    for category in categories:
        builder.row(
            InlineKeyboardButton(
                text=f"{icons[category.mode.value]} {compact_label(category.name)}",
                callback_data=f"browse:{category.id}",
                style="primary",
            )
        )
    builder.row(InlineKeyboardButton(text="🏠 Main menu", callback_data="menu:home"))
    text = (
        "🗂 <b>BROWSE THE LIBRARY</b>\n"
        "<blockquote>Explore the library one collection at a time.</blockquote>\n"
        f"{DIVIDER}\n"
        f"📚 Available collections: <b>{len(categories)}</b>\n\n"
        "Choose where you want to explore:"
    )
    if not categories:
        text += "\n\n🫙 <i>No collections are available yet.</i>"
    return text, builder.as_markup()


def search_results(
    session: SearchSession,
    contents: list[ContentRecord],
    page: int,
    page_size: int = 4,
    *,
    heading: str = "SEARCH RESULTS",
    prompt: str = "Tap the best match below:",
) -> tuple[str, InlineKeyboardMarkup]:
    visible, page, pages = page_slice(contents, page, page_size)
    if heading == "SEARCH RESULTS" and session.query == "Recently added":
        heading = "RECENTLY ADDED"
        prompt = "Fresh arrivals—choose a title to view its details:"
    builder = InlineKeyboardBuilder()
    for rank, content in enumerate(visible, start=page * page_size + 1):
        icon = "📺" if content.kind == ContentKind.SERIES else "🎬"
        year = f" ({content.year or 'Unknown'})"
        builder.row(
            InlineKeyboardButton(
                text=compact_label(f"{rank}. {icon} {content.title}{year}", 58),
                callback_data=f"ct:{content.id}:{session.token}:{page}",
                style="primary",
            )
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="◀️ Previous", callback_data=f"sr:{session.token}:{page - 1}")
        )
    if page + 1 < pages:
        navigation.append(
            InlineKeyboardButton(text="Next ▶️", callback_data=f"sr:{session.token}:{page + 1}")
        )
    if navigation:
        builder.row(*navigation)
    builder.row(InlineKeyboardButton(text="🏠 Main menu", callback_data="menu:home"))
    text = (
        f"🔎 <b>{safe_html(heading)}</b>\n"
        f"<blockquote>Results for “{safe_html(session.query)}”</blockquote>\n"
        f"{DIVIDER}\n"
        f"🎯 Showing <b>{len(contents)}</b> title{'s' if len(contents) != 1 else ''}\n"
        f"{_page_line(page, pages)}\n\n"
        f"{safe_html(prompt)}"
    )
    return text, builder.as_markup()


def no_results(query: str) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔎 Try another search", callback_data="menu:search"),
        InlineKeyboardButton(text="🗂 Browse instead", callback_data="menu:browse"),
    )
    builder.row(InlineKeyboardButton(text="🏠 Main menu", callback_data="menu:home"))
    return (
        (
            "🔍 <b>NO MATCH FOUND</b>\n"
            f"<blockquote>“{safe_html(query)}”</blockquote>\n"
            f"{DIVIDER}\n"
            "🪄 Try fewer words, remove the year, or check the spelling."
        ),
        builder.as_markup(),
    )


def content_screen(
    *,
    content: ContentRecord,
    category: Category,
    query: CatalogQueryService,
    back_token: str = "0",
    back_page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    languages, qualities = query.aggregates(content.id)
    icon = "📺" if content.kind == ContentKind.SERIES else "🎬"
    kind_label = "Series" if content.kind == ContentKind.SERIES else "Movie"
    language_text = safe_html(", ".join(languages) if languages else "Unknown")
    quality_text = safe_html(", ".join(qualities) if qualities else "Unknown")
    lines = [
        f"{icon} <b>{safe_html(content.title)}</b>",
        f"<blockquote>{kind_label} details and available files</blockquote>",
        DIVIDER,
        f"🗂 <b>Category</b>  •  {safe_html(category.name)}",
        f"📅 <b>Year</b>  •  {content.year or 'Unknown'}",
        f"🗣 <b>Language</b>  •  {language_text}",
        f"💎 <b>Quality</b>  •  {quality_text}",
    ]

    builder = InlineKeyboardBuilder()
    if content.kind == ContentKind.SERIES:
        seasons = query.seasons(content.id)
        if seasons:
            lines.extend(
                [
                    DIVIDER,
                    f"📚 <b>{len(seasons)} season{'s' if len(seasons) != 1 else ''} available</b>",
                ]
            )
        else:
            lines.extend([DIVIDER, "🫙 <i>No seasons are available right now.</i>"])
        buttons = [
            InlineKeyboardButton(
                text=f"📺 Season {season}",
                callback_data=f"se:{content.id}:{season}:{back_token}:{back_page}",
                style="primary",
            )
            for season in seasons
        ]
        for index in range(0, len(buttons), 2):
            builder.row(*buttons[index : index + 2])
    else:
        variants = query.movie_variants(content.id)
        if not variants:
            lines.extend([DIVIDER, "🫙 <i>No files are available right now.</i>"])
        elif len(variants) == 1:
            lines.extend([DIVIDER, "✅ <b>Ready for protected delivery</b>"])
            builder.row(
                InlineKeyboardButton(
                    text="▶️ Get protected file",
                    callback_data=f"fl:{variants[0].id}",
                    style="success",
                )
            )
        else:
            lines.extend([DIVIDER, f"🎞 <b>{len(variants)} versions available</b>"])
            for item in variants:
                builder.row(
                    InlineKeyboardButton(
                        text=compact_label(f"▶️ {variant_label(item)}", 58),
                        callback_data=f"fl:{item.id}",
                        style="success",
                    )
                )

    # This is a search-navigation sentinel, not a credential.
    if back_token != "0":  # nosec B105
        builder.row(
            InlineKeyboardButton(text="◀️ Results", callback_data=f"sr:{back_token}:{back_page}"),
            InlineKeyboardButton(text="🏠 Home", callback_data="menu:home"),
        )
    else:
        builder.row(InlineKeyboardButton(text="🏠 Main menu", callback_data="menu:home"))
    return "\n".join(lines), builder.as_markup()


def season_screen(
    content: ContentRecord,
    season: int,
    query: CatalogQueryService,
    token: str,
    result_page: int,
    episode_page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    episodes = query.episodes(content.id, season)
    visible, episode_page, pages = page_slice(episodes, episode_page, query.EPISODE_PAGE_SIZE)
    builder = InlineKeyboardBuilder()
    episode_buttons = [
        InlineKeyboardButton(
            text=f"▶️ E{episode:02d}",
            callback_data=f"ep:{content.id}:{season}:{episode}:{token}:{result_page}",
            style="primary",
        )
        for episode in visible
    ]
    for index in range(0, len(episode_buttons), 4):
        builder.row(*episode_buttons[index : index + 4])

    pack_parts = query.season_pack_parts(content.id, season)
    if pack_parts:
        builder.row(
            InlineKeyboardButton(
                text="📦 Download complete season pack",
                callback_data=f"pk:{content.id}:{season}:{token}:{result_page}",
                style="success",
            )
        )
    navigation: list[InlineKeyboardButton] = []
    if episode_page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="◀️ Previous",
                callback_data=f"epg:{content.id}:{season}:{token}:{result_page}:{episode_page - 1}",
            )
        )
    if episode_page + 1 < pages:
        navigation.append(
            InlineKeyboardButton(
                text="Next ▶️",
                callback_data=f"epg:{content.id}:{season}:{token}:{result_page}:{episode_page + 1}",
            )
        )
    if navigation:
        builder.row(*navigation)
    builder.row(
        InlineKeyboardButton(
            text="◀️ Seasons", callback_data=f"ct:{content.id}:{token}:{result_page}"
        ),
        InlineKeyboardButton(text="🏠 Home", callback_data="menu:home"),
    )
    text = (
        f"📺 <b>{safe_html(content.title)}</b>\n"
        f"<blockquote>Season {season} • choose what to watch</blockquote>\n"
        f"{DIVIDER}\n"
        f"🎞 Episodes available: <b>{len(episodes)}</b>\n"
        f"{_page_line(episode_page, pages)}\n\n"
        "Tap an episode to receive it:"
    )
    if not episodes and pack_parts:
        text = (
            f"📺 <b>{safe_html(content.title)}</b>\n"
            f"<blockquote>Season {season}</blockquote>\n"
            f"{DIVIDER}\n"
            "📦 Individual episodes are unavailable, but the complete season pack is ready."
        )
    elif not episodes:
        text = (
            f"📺 <b>{safe_html(content.title)}</b>\n"
            f"<blockquote>Season {season}</blockquote>\n"
            f"{DIVIDER}\n"
            "🫙 <i>No episodes or season packs are available right now.</i>"
        )
    return text, builder.as_markup()


def variants_screen(
    content: ContentRecord,
    season: int,
    episode: int,
    variants: list[FileRecord],
    token: str,
    result_page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    for item in variants:
        builder.row(
            InlineKeyboardButton(
                text=compact_label(f"▶️ {variant_label(item)}", 58),
                callback_data=f"fl:{item.id}",
                style="success",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Episodes", callback_data=f"se:{content.id}:{season}:{token}:{result_page}"
        ),
        InlineKeyboardButton(text="🏠 Home", callback_data="menu:home"),
    )
    return (
        (
            f"📺 <b>{safe_html(content.title)}</b>\n"
            f"<blockquote>Season {season} • Episode {episode}</blockquote>\n"
            f"{DIVIDER}\n"
            f"🎞 Available versions: <b>{len(variants)}</b>\n\n"
            "Choose your preferred language and quality:"
        ),
        builder.as_markup(),
    )


def pack_screen(
    content: ContentRecord,
    season: int,
    parts: list[FileRecord],
    token: str,
    result_page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    for item in parts:
        label = f"📦 Part {item.pack_part or 1} • {variant_label(item)}"
        builder.row(
            InlineKeyboardButton(
                text=compact_label(label, 58),
                callback_data=f"fl:{item.id}",
                style="success",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Episodes", callback_data=f"se:{content.id}:{season}:{token}:{result_page}"
        ),
        InlineKeyboardButton(text="🏠 Home", callback_data="menu:home"),
    )
    return (
        (
            f"📦 <b>{safe_html(content.title)}</b>\n"
            f"<blockquote>Season {season} • complete pack</blockquote>\n"
            f"{DIVIDER}\n"
            f"🧩 Archive parts: <b>{len(parts)}</b>\n\n"
            "Download every part before extracting the archive:"
        ),
        builder.as_markup(),
    )


def watchlist_display_name(user: UserProfile) -> str:
    return user.watchlist_display_name or user.first_name


def watchlist_home(user: UserProfile) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="➕ Add a title", callback_data="wla:start", style="success")
    )
    builder.row(
        InlineKeyboardButton(
            text=f"📚 My titles · {len(user.watchlist)}",
            callback_data="wlm:0",
            style="primary",
        ),
        InlineKeyboardButton(text="🌐 Community", callback_data="wlp:0", style="primary"),
    )
    builder.row(
        InlineKeyboardButton(
            text="✏️ Change community name",
            callback_data="wln:edit",
            style="primary",
        )
    )
    builder.row(InlineKeyboardButton(text="🏠 Main menu", callback_data="menu:home"))
    return (
        (
            "📚 <b>MY WATCHLIST</b>\n"
            "<blockquote>Plan it. Pause it. Complete it.</blockquote>\n"
            f"{DIVIDER}\n"
            f"🎞 Saved titles  •  <b>{len(user.watchlist)}</b>\n"
            f"👤 Community name  •  <b>{safe_html(watchlist_display_name(user))}</b>\n"
            "🌐 Visibility  •  <b>Always public</b>\n"
            f"{DIVIDER}\n"
            "Paste a custom batch or select several library titles together."
        ),
        builder.as_markup(),
    )


def watchlist_add_method() -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="☑️ Select from the library", callback_data="wla:catalog", style="primary"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✍️ Add custom titles", callback_data="wla:manual", style="primary"
        )
    )
    builder.row(InlineKeyboardButton(text="◀️ Watchlist", callback_data="menu:watchlist"))
    return (
        (
            "➕ <b>ADD TO WATCHLIST</b>\n"
            "<blockquote>Select library titles or paste a custom batch.</blockquote>\n"
            f"{DIVIDER}\n"
            "How would you like to add titles?"
        ),
        builder.as_markup(),
    )


def watchlist_category_picker(
    categories: list[Category], callback_prefix: str, heading: str
) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    icons = {"single": "🎬", "episodic": "📺", "mixed": "🗂"}
    for category in categories:
        builder.row(
            InlineKeyboardButton(
                text=f"{icons[category.mode.value]} {compact_label(category.name)}",
                callback_data=f"{callback_prefix}:{category.id}",
                style="primary",
            )
        )
    builder.row(InlineKeyboardButton(text="◀️ Add title", callback_data="wla:start"))
    custom_batch = callback_prefix == "wamc"
    prompt = (
        "Which collection would you like to browse?"
        if callback_prefix == "wlbc"
        else "Where do these titles belong?"
    )
    text = (
        f"🗂 <b>{safe_html(heading.upper())}</b>\n"
        f"<blockquote>Step 1 of {4 if custom_batch else 3} • choose a collection</blockquote>\n"
        f"{DIVIDER}\n"
        f"{prompt}"
    )
    if not categories:
        text += "\n\n🫙 <i>No enabled categories are available.</i>"
    return text, builder.as_markup()


def watchlist_custom_input(category_name: str) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="watchlist:add"))
    return (
        (
            "✍️ <b>CUSTOM TITLE BATCH</b>\n"
            f"<blockquote>{safe_html(category_name)} • Step 2 of 4 • up to 25 titles</blockquote>\n"
            f"{DIVIDER}\n"
            "Send one title per line. Blank lines and repeated titles are ignored; each title may be "
            "up to 160 characters. You can review and untick titles before saving."
        ),
        builder.as_markup(),
    )


def watchlist_custom_batch_preview(
    titles: list[str],
    selected: set[int],
) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    for index, title in enumerate(titles):
        marker = "✅" if index in selected else "☐"
        label = title if len(title) <= 42 else f"{title[:39]}…"
        builder.row(
            InlineKeyboardButton(
                text=f"{marker} {label}",
                callback_data=f"wctp:{index}",
                style="success" if index in selected else None,
            )
        )
    builder.row(
        InlineKeyboardButton(text="✅ Select all", callback_data="wcta:all"),
        InlineKeyboardButton(text="🧹 Clear", callback_data="wcta:none"),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"Continue with {len(selected)} ›",
            callback_data="wct:continue",
            style="primary",
        )
    )
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="watchlist:add"))
    preview = "\n".join(
        f"{'✅' if index in selected else '▫️'} {safe_html(title[:88])}"
        f"{'…' if len(title) > 88 else ''}"
        for index, title in enumerate(titles)
    )
    return (
        (
            "☑️ <b>REVIEW CUSTOM TITLES</b>\n"
            f"<blockquote>Step 3 of 4 • {len(selected)} of {len(titles)} selected</blockquote>\n"
            f"{DIVIDER}\n{preview}\n\n"
            "Tap a title to include or exclude it, then continue to choose one status for the batch."
        ),
        builder.as_markup(),
    )


def watchlist_title_initial(title: str) -> str:
    stripped = title.strip()
    if not stripped:
        return "#"
    initial = stripped[0].upper()
    return initial if "A" <= initial <= "Z" else "#"


def watchlist_library_filter(
    session: SearchSession,
    contents: list[ContentRecord],
    saved_content_ids: set[str] | None = None,
) -> list[ContentRecord]:
    saved_content_ids = saved_content_ids or set()
    filtered = contents
    if session.alphabet_filter is not None:
        filtered = [
            content
            for content in filtered
            if watchlist_title_initial(content.title) == session.alphabet_filter
        ]
    if session.text_filter:
        query = normalize_title(session.text_filter)
        filtered = [content for content in filtered if query in content.normalized_title]
    if session.only_unsaved:
        filtered = [content for content in filtered if content.id not in saved_content_ids]
    if session.selected_only:
        filtered = [content for content in filtered if content.id in session.selected_content_ids]
    return filtered


def watchlist_library_results(
    session: SearchSession,
    contents: list[ContentRecord],
    page: int,
    *,
    saved_content_ids: set[str] | None = None,
    page_size: int = 6,
) -> tuple[str, InlineKeyboardMarkup]:
    saved_content_ids = saved_content_ids or set()
    filtered = watchlist_library_filter(session, contents, saved_content_ids)
    visible, page, pages = page_slice(filtered, page, page_size)
    selected = session.selected_content_ids
    saved_content_ids = saved_content_ids or set()
    builder = InlineKeyboardBuilder()
    for content in visible:
        checked = content.id in selected
        marker = "📚 " if content.id in saved_content_ids else ""
        callback_data = f"wlbt:{session.token}:{content.id}:{page}"
        builder.row(
            InlineKeyboardButton(
                text="✅" if checked else "☐",
                callback_data=callback_data,
                style="success" if checked else None,
            ),
            InlineKeyboardButton(
                text=compact_label(
                    f"{marker}{content.title} ({content.year or 'Unknown'})",
                    52,
                ),
                callback_data=callback_data,
                style="primary",
            ),
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="◀️ Previous",
                callback_data=f"wlbp:{session.token}:{page - 1}",
            )
        )
    if page + 1 < pages:
        navigation.append(
            InlineKeyboardButton(
                text="Next ▶️",
                callback_data=f"wlbp:{session.token}:{page + 1}",
            )
        )
    if navigation:
        builder.row(*navigation)
    alphabet_label = session.alphabet_filter or "All"
    builder.row(
        InlineKeyboardButton(
            text=(
                f"🔎 {compact_label(session.text_filter, 22)}"
                if session.text_filter
                else "🔎 Search"
            ),
            callback_data=f"wlfq:{session.token}:{page}",
            style="success" if session.text_filter else "primary",
        ),
        InlineKeyboardButton(
            text="✅ Unsaved only" if session.only_unsaved else "📭 Unsaved only",
            callback_data=f"wluo:{session.token}:{page}",
            style="success" if session.only_unsaved else None,
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="☑️ Select page",
            callback_data=f"wlsp:{session.token}:{page}",
            style="primary",
        ),
        InlineKeyboardButton(
            text="🧹 Clear selected",
            callback_data=f"wlcl:{session.token}:{page}",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"🔤 Alphabet · {alphabet_label}",
            callback_data=f"wlba:{session.token}:{page}",
            style="primary",
        ),
        InlineKeyboardButton(
            text="✅ Selected only" if session.selected_only else "👁 Review selected",
            callback_data=f"wlrv:{session.token}:{page}",
            style="success" if session.selected_only else None,
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=f"➕ Add Selected · {len(selected)}",
            callback_data=f"wlbd:{session.token}:{page}",
            style="success" if selected else None,
        )
    )
    builder.row(
        InlineKeyboardButton(text="◀️ Collections", callback_data="wla:catalog"),
        InlineKeyboardButton(text="✖️ Cancel", callback_data="menu:watchlist"),
    )
    text = (
        "☑️ <b>CHOOSE LIBRARY TITLES</b>\n"
        f"<blockquote>{safe_html(session.query)} • alphabetical catalog</blockquote>\n"
        f"{DIVIDER}\n"
        f"🎞 Matching titles: <b>{len(filtered)}</b>  •  Selected: <b>{len(selected)}/25</b>\n"
        f"🔤 Alphabet: <b>{safe_html(alphabet_label)}</b>  •  {_page_line(page, pages)}\n"
        f"🔎 Search: <b>{safe_html(session.text_filter or 'Off')}</b>  •  "
        f"Unsaved: <b>{'Only' if session.only_unsaved else 'All'}</b>\n\n"
        "Tap ☐ or the title to select it. 📚 marks a title already in your Watchlist."
    )
    if not filtered:
        text += "\n\n🫙 <i>No titles match the active filters.</i>"
    return text, builder.as_markup()


def watchlist_alphabet_picker(
    session: SearchSession,
    contents: list[ContentRecord],
) -> tuple[str, InlineKeyboardMarkup]:
    available = {watchlist_title_initial(content.title) for content in contents}
    builder = InlineKeyboardBuilder()
    buttons = [
        InlineKeyboardButton(
            text="✅ All" if session.alphabet_filter is None else "All",
            callback_data=f"wlaf:{session.token}:*",
            style="success" if session.alphabet_filter is None else "primary",
        )
    ]
    for letter in [*"ABCDEFGHIJKLMNOPQRSTUVWXYZ", "#"]:
        if letter not in available:
            continue
        buttons.append(
            InlineKeyboardButton(
                text=f"✅ {letter}" if session.alphabet_filter == letter else letter,
                callback_data=f"wlaf:{session.token}:{'0' if letter == '#' else letter}",
                style="success" if session.alphabet_filter == letter else "primary",
            )
        )
    for index in range(0, len(buttons), 6):
        builder.row(*buttons[index : index + 6])
    builder.row(
        InlineKeyboardButton(
            text="◀️ Back to titles",
            callback_data=f"wlbp:{session.token}:0",
        )
    )
    return (
        (
            "🔤 <b>JUMP BY ALPHABET</b>\n"
            f"<blockquote>{safe_html(session.query)} • direct title filter</blockquote>\n"
            f"{DIVIDER}\n"
            "Choose a letter to see only titles beginning with it. "
            "Your current selections stay checked."
        ),
        builder.as_markup(),
    )


def watchlist_library_status_picker(
    session: SearchSession,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    count = len(session.selected_content_ids)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🕓 To Watch",
            callback_data=f"wlbs:{session.token}:t:{page}",
            style="primary",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⏸ On Hold",
            callback_data=f"wlbs:{session.token}:h:{page}",
            style="primary",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✅ Completed",
            callback_data=f"wlbs:{session.token}:c:{page}",
            style="success",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Back to titles",
            callback_data=f"wlbp:{session.token}:{page}",
        ),
        InlineKeyboardButton(text="✖️ Cancel", callback_data="menu:watchlist"),
    )
    return (
        (
            "📚 <b>ADD SELECTED TITLES</b>\n"
            f"<blockquote>{count} title{'s' if count != 1 else ''} selected</blockquote>\n"
            f"{DIVIDER}\n"
            "Choose one status for every selected library title."
        ),
        builder.as_markup(),
    )


def watchlist_status_picker(
    title: str,
    callback_prefix: str,
    *,
    plural: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    labels = {
        WatchStatus.TO_WATCH: "🕓 To Watch",
        WatchStatus.ON_HOLD: "⏸ On Hold",
        WatchStatus.COMPLETED: "✅ Completed",
    }
    for status in WatchStatus:
        builder.row(
            InlineKeyboardButton(
                text=labels[status],
                callback_data=f"{callback_prefix}:{WATCH_CODES[status]}",
                style="success" if status == WatchStatus.COMPLETED else "primary",
            )
        )
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data="menu:watchlist"))
    return (
        (
            f"➕ <b>{safe_html(title)}</b>\n"
            "<blockquote>Final step • choose a status</blockquote>\n"
            f"{DIVIDER}\n"
            f"Where should {'these titles' if plural else 'this title'} go?"
        ),
        builder.as_markup(),
    )


def _watch_status_label(status: WatchStatus) -> str:
    return {
        WatchStatus.TO_WATCH: "🕓 To Watch",
        WatchStatus.ON_HOLD: "⏸ On Hold",
        WatchStatus.COMPLETED: "✅ Completed",
    }[status]


def watchlist_entries(
    owner: UserProfile,
    entries: list[WatchlistEntry],
    page: int,
    *,
    own: bool,
    page_size: int = 6,
) -> tuple[str, InlineKeyboardMarkup]:
    visible, page, pages = page_slice(entries, page, page_size)
    builder = InlineKeyboardBuilder()
    for entry in visible:
        callback_data = (
            f"wle:{entry.id}:{page}" if own else f"wved:{owner.telegram_user_id}:{entry.id}:{page}"
        )
        builder.row(
            InlineKeyboardButton(
                text=compact_label(
                    f"{_watch_status_label(entry.status)} • 🗂 {entry.category_name}",
                    58,
                ),
                callback_data=callback_data,
                style="success" if entry.status == WatchStatus.COMPLETED else None,
            )
        )
        builder.row(
            InlineKeyboardButton(
                text=compact_label(f"🎞 {entry.title}", 58),
                callback_data=callback_data,
                style="primary",
            )
        )
    navigation: list[InlineKeyboardButton] = []
    prefix = "wlm" if own else f"wlv:{owner.telegram_user_id}"
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="◀️ Previous", callback_data=f"{prefix}:{page - 1}")
        )
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"{prefix}:{page + 1}"))
    if navigation:
        builder.row(*navigation)
    if own and not entries:
        builder.row(
            InlineKeyboardButton(
                text="➕ Add your first title", callback_data="wla:start", style="success"
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Watchlist" if own else "◀️ Community",
            callback_data="menu:watchlist" if own else "wlp:0",
        ),
        InlineKeyboardButton(text="🏠 Home", callback_data="menu:home"),
    )
    name = "MY TITLES" if own else f"{watchlist_display_name(owner)}’s watchlist"
    status_counts = {status: 0 for status in WatchStatus}
    for entry in entries:
        status_counts[entry.status] += 1
    text = (
        f"📚 <b>{safe_html(name)}</b>\n"
        f"<blockquote>{len(entries)} saved title{'s' if len(entries) != 1 else ''}</blockquote>\n"
        f"{DIVIDER}\n"
        f"🕓 To Watch: <b>{status_counts[WatchStatus.TO_WATCH]}</b>  •  "
        f"⏸ On Hold: <b>{status_counts[WatchStatus.ON_HOLD]}</b>\n"
        f"✅ Completed: <b>{status_counts[WatchStatus.COMPLETED]}</b>  •  "
        "🗂 Category shown per title\n"
        f"{_page_line(page, pages)}"
    )
    if not entries:
        text += "\n\n🫙 <i>No titles here yet. Your next favorite can start here.</i>"
    return text, builder.as_markup()


def watchlist_entry_detail(
    entry: WatchlistEntry,
    owner: UserProfile,
    *,
    own: bool,
    content_available: bool,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    if own:
        for status in WatchStatus:
            selected = " ✓" if entry.status == status else ""
            builder.row(
                InlineKeyboardButton(
                    text=_watch_status_label(status) + selected,
                    callback_data=f"wlu:{entry.id}:{WATCH_CODES[status]}",
                    style="success" if entry.status == status else "primary",
                )
            )
        builder.row(
            InlineKeyboardButton(
                text="🗑 Remove from watchlist",
                callback_data=f"wld:{entry.id}",
                style="danger",
            )
        )
    if content_available and entry.content_id:
        builder.row(
            InlineKeyboardButton(
                text="🎞 Open in the library",
                callback_data=f"ct:{entry.content_id}:0:0",
                style="primary",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=(
                "◀️ My titles"
                if own
                else compact_label(f"◀️ {watchlist_display_name(owner)}’s list", 58)
            ),
            callback_data=(f"wlm:{page}" if own else f"wlv:{owner.telegram_user_id}:{page}"),
        )
    )
    availability = "Available in the library" if content_available else "Custom saved title"
    source_icon = "🎞" if content_available else "✍️"
    text = (
        f"📚 <b>{safe_html(entry.title)}</b>\n"
        "<blockquote>Watchlist title details</blockquote>\n"
        f"{DIVIDER}\n"
        f"🗂 <b>Category</b>  •  {safe_html(entry.category_name)}\n"
        f"🏷 <b>Status</b>  •  {_watch_status_label(entry.status)}\n"
        f"{source_icon} <b>Source</b>  •  {availability}"
    )
    return text, builder.as_markup()


def public_watchlist_directory(
    users: list[UserProfile], page: int, page_size: int = 8
) -> tuple[str, InlineKeyboardMarkup]:
    visible, page, pages = page_slice(users, page, page_size)
    builder = InlineKeyboardBuilder()
    for user in visible:
        username = f" @{user.username}" if user.username else ""
        builder.row(
            InlineKeyboardButton(
                text=compact_label(
                    f"👤 {watchlist_display_name(user)}{username} · {len(user.watchlist)} titles",
                    58,
                ),
                callback_data=f"wlv:{user.telegram_user_id}:0",
                style="primary",
            )
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="◀️ Previous", callback_data=f"wlp:{page - 1}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"wlp:{page + 1}"))
    if navigation:
        builder.row(*navigation)
    builder.row(InlineKeyboardButton(text="◀️ Watchlist", callback_data="menu:watchlist"))
    text = (
        "🌐 <b>COMMUNITY WATCHLISTS</b>\n"
        "<blockquote>Discover what other members saved.</blockquote>\n"
        f"{DIVIDER}\n"
        f"👥 Public lists: <b>{len(users)}</b>\n"
        f"{_page_line(page, pages)}"
    )
    if not users:
        text += "\n\n🫙 <i>No other public watchlists are available yet.</i>"
    return text, builder.as_markup()


def admin_dashboard() -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🗂 Categories", callback_data="admin:categories", style="primary"
        ),
        InlineKeyboardButton(text="🎞 Catalog", callback_data="admin:files", style="primary"),
    )
    builder.row(
        InlineKeyboardButton(
            text="⚠️ Index failures", callback_data="admin:failures", style="primary"
        ),
        InlineKeyboardButton(text="👥 Users", callback_data="admin:users", style="primary"),
    )
    builder.row(
        InlineKeyboardButton(text="📊 Statistics", callback_data="admin:stats"),
        InlineKeyboardButton(text="💾 Database", callback_data="admin:database"),
    )
    builder.row(
        InlineKeyboardButton(text="🔐 Access", callback_data="admin:access", style="primary"),
        InlineKeyboardButton(text="⚙️ Settings", callback_data="admin:settings"),
    )
    builder.row(InlineKeyboardButton(text="📜 Audit trail", callback_data="admin:audit"))
    builder.row(InlineKeyboardButton(text="🏠 User menu", callback_data="menu:home"))
    return (
        (
            "🛡 <b>ADMIN CONTROL CENTER</b>\n"
            "<blockquote>Catalog, access, recovery, and operations.</blockquote>\n"
            f"{DIVIDER}\n"
            "⚡ <b>Quick actions</b>\n"
            "Choose a workspace below. Destructive actions always require confirmation.\n"
            f"{DIVIDER}\n"
            "🔐 Owner-only area"
        ),
        builder.as_markup(),
    )


def admin_categories(categories: list[Category]) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    for item in categories:
        status = "✅" if item.enabled else "⏸"
        builder.row(
            InlineKeyboardButton(
                text=compact_label(f"{status} {item.name}"),
                callback_data=f"ac:{item.id}",
                style="primary" if item.enabled else None,
            )
        )
    builder.row(
        InlineKeyboardButton(text="➕ Add new category", callback_data="aca:start", style="success")
    )
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    enabled = sum(category.enabled for category in categories)
    text = (
        "🗂 <b>CATEGORY MANAGEMENT</b>\n"
        "<blockquote>Control library collections and source channels.</blockquote>\n"
        f"{DIVIDER}\n"
        f"✅ Enabled: <b>{enabled}</b>  •  ⏸ Disabled: <b>{len(categories) - enabled}</b>\n"
        f"📚 Total configured: <b>{len(categories)}</b>"
    )
    if not categories:
        text += "\n\n🫙 <i>No categories configured. Add the first one below.</i>"
    return text, builder.as_markup()


def admin_category_detail(category: Category) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Rename", callback_data=f"acr:{category.id}", style="primary"),
        InlineKeyboardButton(
            text="🔄 Change channel", callback_data=f"acc:{category.id}", style="primary"
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="⚙️ Change mode", callback_data=f"acm:{category.id}", style="primary"
        ),
        InlineKeyboardButton(
            text="⏸ Disable" if category.enabled else "✅ Enable",
            callback_data=f"act:{category.id}",
            style=None if category.enabled else "success",
        ),
    )
    builder.row(InlineKeyboardButton(text="◀️ Categories", callback_data="admin:categories"))
    status_icon = "✅" if category.enabled else "⏸"
    text = (
        f"🗂 <b>{safe_html(category.name)}</b>\n"
        "<blockquote>Category configuration</blockquote>\n"
        f"{DIVIDER}\n"
        f"🆔 <b>ID</b>  •  <code>{category.id}</code>\n"
        f"📡 <b>Channel</b>  •  {safe_html(category.channel_title or 'Unknown')}\n"
        f"🔢 <b>Channel ID</b>  •  <code>{category.active_channel_id}</code>\n"
        f"⚙️ <b>Mode</b>  •  {safe_html(category.mode.value.title())}\n"
        f"{status_icon} <b>Status</b>  •  {'Enabled' if category.enabled else 'Disabled'}\n"
        f"🗄 <b>Legacy channels</b>  •  {len(category.legacy_channel_ids)}"
    )
    return text, builder.as_markup()


def access_mode_panel(current: AccessMode) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    labels = {
        AccessMode.PUBLIC: "🌐 Public",
        AccessMode.APPROVAL: "✅ Admin approval",
        AccessMode.ALLOWLIST: "🔒 Allowlist only",
    }
    for mode, label in labels.items():
        selected = " ✓" if mode == current else ""
        builder.row(
            InlineKeyboardButton(
                text=label + selected,
                callback_data=f"access:{mode.value}",
                style="success" if mode == current else "primary",
            )
        )
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    return (
        (
            "🔐 <b>ACCESS CONTROL</b>\n"
            "<blockquote>Choose who can use the media library.</blockquote>\n"
            f"{DIVIDER}\n"
            f"🟢 Current mode  •  <b>{safe_html(current.value.title())}</b>\n\n"
            "ℹ️ Mode changes never unban suspended or banned users."
        ),
        builder.as_markup(),
    )


def users_panel(users: list[UserProfile]) -> tuple[str, InlineKeyboardMarkup]:
    counts = {status: 0 for status in UserStatus}
    for user in users:
        counts[user.status] += 1
    builder = InlineKeyboardBuilder()
    for status, icon in (
        (UserStatus.ACTIVE, "✅"),
        (UserStatus.PENDING, "🕓"),
        (UserStatus.SUSPENDED, "⏸"),
        (UserStatus.BANNED, "⛔"),
    ):
        builder.row(
            InlineKeyboardButton(
                text=f"{icon} {status.value.title()} · {counts[status]}",
                callback_data=f"aul:{status.value}:0",
                style="primary",
            )
        )
    builder.row(
        InlineKeyboardButton(text="🔎 Find a user", callback_data="auf:start", style="primary")
    )
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    return (
        (
            "👥 <b>USER MANAGEMENT</b>\n"
            "<blockquote>Review access and account status.</blockquote>\n"
            f"{DIVIDER}\n"
            f"👤 Total registered: <b>{len(users)}</b>\n"
            f"✅ {counts[UserStatus.ACTIVE]} active  •  "
            f"🕓 {counts[UserStatus.PENDING]} pending\n"
            f"⏸ {counts[UserStatus.SUSPENDED]} suspended  •  "
            f"⛔ {counts[UserStatus.BANNED]} banned"
        ),
        builder.as_markup(),
    )


def user_detail(user: UserProfile) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Activate",
            callback_data=f"aus:{user.telegram_user_id}:active",
            style="success",
        ),
        InlineKeyboardButton(
            text="⏸ Suspend", callback_data=f"aus:{user.telegram_user_id}:suspended"
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="⛔ Ban user",
            callback_data=f"aus:{user.telegram_user_id}:banned",
            style="danger",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✏️ Edit community name",
            callback_data=f"aucn:{user.telegram_user_id}",
            style="primary",
        )
    )
    builder.row(InlineKeyboardButton(text="◀️ Users", callback_data="admin:users"))
    username = f"@{safe_html(user.username)}" if user.username else "Not set"
    status_icons = {
        UserStatus.ACTIVE: "✅",
        UserStatus.PENDING: "🕓",
        UserStatus.SUSPENDED: "⏸",
        UserStatus.BANNED: "⛔",
    }
    text = (
        f"👤 <b>{safe_html(user.first_name)}</b>\n"
        "<blockquote>User access profile</blockquote>\n"
        f"{DIVIDER}\n"
        f"🆔 <b>User ID</b>  •  <code>{user.telegram_user_id}</code>\n"
        f"🔗 <b>Username</b>  •  {username}\n"
        f"{status_icons[user.status]} <b>Status</b>  •  {safe_html(user.status.value.title())}\n"
        f"📅 <b>Joined</b>  •  {safe_html(user.created_at)}\n"
        f"📚 <b>Watchlist</b>  •  {len(user.watchlist)} titles\n"
        f"🌐 <b>Community name</b>  •  {safe_html(watchlist_display_name(user))}"
    )
    return text, builder.as_markup()

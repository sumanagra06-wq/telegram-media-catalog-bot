from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TypeVar

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
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
from .services import CatalogQueryService, SearchSession, variant_label
from .utils import compact_label, safe_html

WATCH_CODES = {
    WatchStatus.TO_WATCH: "t",
    WatchStatus.ON_HOLD: "h",
    WatchStatus.COMPLETED: "c",
}
CODE_WATCH = {value: key for key, value in WATCH_CODES.items()}
ItemT = TypeVar("ItemT")


def page_slice(values: Sequence[ItemT], page: int, page_size: int) -> tuple[list[ItemT], int, int]:
    pages = max(1, math.ceil(len(values) / page_size))
    page = max(0, min(page, pages - 1))
    start = page * page_size
    return list(values[start : start + page_size]), page, pages


def main_dashboard(is_owner: bool) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🎬 <b>Media Library</b>\n\n"
        "Send the name of any movie or series.\n\n"
        "Examples: <code>Dark</code>, <code>Dune</code>, "
        "<code>Interstellar 2014</code>"
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔎 Search help", callback_data="menu:search"),
        InlineKeyboardButton(text="🗂 Browse", callback_data="menu:browse"),
    )
    builder.row(
        InlineKeyboardButton(text="🆕 Recently added", callback_data="menu:recent"),
        InlineKeyboardButton(text="📚 My watchlist", callback_data="menu:watchlist"),
    )
    builder.row(InlineKeyboardButton(text="❓ Help", callback_data="menu:help"))
    if is_owner:
        builder.row(InlineKeyboardButton(text="🛡 Admin panel", callback_data="admin:home"))
    return text, builder.as_markup()


def browse_categories(categories: list[Category]) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.row(
            InlineKeyboardButton(
                text=f"🗂 {compact_label(category.name)}",
                callback_data=f"browse:{category.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="🏠 Main menu", callback_data="menu:home"))
    text = "🗂 <b>Browse library</b>\n\nChoose a category:"
    if not categories:
        text += "\n\nNo categories are currently available."
    return text, builder.as_markup()


def search_results(
    session: SearchSession,
    contents: list[ContentRecord],
    page: int,
    page_size: int = 4,
) -> tuple[str, InlineKeyboardMarkup]:
    visible, page, pages = page_slice(contents, page, page_size)
    builder = InlineKeyboardBuilder()
    for content in visible:
        icon = "📺" if content.kind == ContentKind.SERIES else "🎬"
        year = f" ({content.year})" if content.year else ""
        builder.row(
            InlineKeyboardButton(
                text=compact_label(f"{icon} {content.title}{year}", 58),
                callback_data=f"ct:{content.id}:{session.token}:{page}",
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
        f"🔎 <b>Results for “{safe_html(session.query)}”</b>\n\n"
        f"Select a title. Page {page + 1} of {pages}."
    )
    return text, builder.as_markup()


def no_results(query: str) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🏠 Main menu", callback_data="menu:home"))
    return (
        (f"No titles matched <b>{safe_html(query)}</b>.\n\nTry fewer words or check the spelling."),
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
    year = f" ({content.year})" if content.year else ""
    lines = [f"{icon} <b>{safe_html(content.title)}{year}</b>", ""]
    lines.append(f"Category: {safe_html(category.name)}")
    lines.append("Language: " + safe_html(", ".join(languages) if languages else "Unknown"))
    lines.append("Quality: " + safe_html(", ".join(qualities) if qualities else "Unknown"))

    builder = InlineKeyboardBuilder()
    if content.kind == ContentKind.SERIES:
        seasons = query.seasons(content.id)
        lines.append(f"Available seasons: {len(seasons)}")
        buttons = [
            InlineKeyboardButton(
                text=f"Season {season}",
                callback_data=f"se:{content.id}:{season}:{back_token}:{back_page}",
            )
            for season in seasons
        ]
        for index in range(0, len(buttons), 2):
            builder.row(*buttons[index : index + 2])
    else:
        variants = query.movie_variants(content.id)
        if len(variants) == 1:
            builder.row(
                InlineKeyboardButton(text="▶️ Get file", callback_data=f"fl:{variants[0].id}")
            )
        else:
            lines.append(f"Available versions: {len(variants)}")
            for item in variants:
                builder.row(
                    InlineKeyboardButton(
                        text=compact_label(variant_label(item), 58),
                        callback_data=f"fl:{item.id}",
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
            text=f"E{episode:02d}",
            callback_data=f"ep:{content.id}:{season}:{episode}:{token}:{result_page}",
        )
        for episode in visible
    ]
    for index in range(0, len(episode_buttons), 4):
        builder.row(*episode_buttons[index : index + 4])

    pack_parts = query.season_pack_parts(content.id, season)
    if pack_parts:
        builder.row(
            InlineKeyboardButton(
                text="📦 Complete Season Pack",
                callback_data=f"pk:{content.id}:{season}:{token}:{result_page}",
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
    text = f"📺 <b>{safe_html(content.title)}</b>\nSeason {season}\n\nChoose an episode:"
    if not episodes and pack_parts:
        text = (
            f"📺 <b>{safe_html(content.title)}</b>\nSeason {season}\n\n"
            "Individual episodes are unavailable; use the season pack."
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
                text=compact_label(variant_label(item), 58), callback_data=f"fl:{item.id}"
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
            f"Season {season} • Episode {episode}\n\nChoose a version:"
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
            InlineKeyboardButton(text=compact_label(label, 58), callback_data=f"fl:{item.id}")
        )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Episodes", callback_data=f"se:{content.id}:{season}:{token}:{result_page}"
        ),
        InlineKeyboardButton(text="🏠 Home", callback_data="menu:home"),
    )
    return (
        (
            f"📦 <b>{safe_html(content.title)} — Season {season} Pack</b>\n\n"
            "Download every part before extracting the archive:"
        ),
        builder.as_markup(),
    )


def watchlist_home(user: UserProfile) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Add title", callback_data="wla:start"))
    builder.row(
        InlineKeyboardButton(text=f"📚 My titles — {len(user.watchlist)}", callback_data="wlm:0"),
        InlineKeyboardButton(text="🌐 Community lists", callback_data="wlp:0"),
    )
    builder.row(
        InlineKeyboardButton(
            text="🔒 Make my list private" if user.watchlist_public else "🌐 Share my list",
            callback_data=f"wlvis:{0 if user.watchlist_public else 1}",
        )
    )
    builder.row(InlineKeyboardButton(text="🏠 Main menu", callback_data="menu:home"))
    visibility = "Public to active bot users" if user.watchlist_public else "Private"
    return (
        (
            "📚 <b>Watchlist</b>\n\n"
            f"Saved titles: {len(user.watchlist)}\n"
            f"Visibility: {visibility}\n\n"
            "Add an indexed catalog title or keep any custom title manually."
        ),
        builder.as_markup(),
    )


def watchlist_add_method() -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🎞 From catalog", callback_data="wla:catalog"),
        InlineKeyboardButton(text="✍️ Manual title", callback_data="wla:manual"),
    )
    builder.row(InlineKeyboardButton(text="◀️ Watchlist", callback_data="menu:watchlist"))
    return "➕ <b>Add title</b>\n\nChoose how to add it:", builder.as_markup()


def watchlist_category_picker(
    categories: list[Category], callback_prefix: str, heading: str
) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.row(
            InlineKeyboardButton(
                text=f"🗂 {compact_label(category.name)}",
                callback_data=f"{callback_prefix}:{category.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="◀️ Add title", callback_data="wla:start"))
    text = f"➕ <b>{safe_html(heading)}</b>\n\nChoose a category:"
    if not categories:
        text += "\n\nNo enabled categories are available."
    return text, builder.as_markup()


def watchlist_status_picker(title: str, callback_prefix: str) -> tuple[str, InlineKeyboardMarkup]:
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
            )
        )
    builder.row(InlineKeyboardButton(text="Cancel", callback_data="menu:watchlist"))
    return (
        f"➕ <b>{safe_html(title)}</b>\n\nChoose its watchlist status:",
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
                text=compact_label(f"{_watch_status_label(entry.status)} • {entry.title}", 58),
                callback_data=callback_data,
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
    builder.row(
        InlineKeyboardButton(
            text="◀️ Watchlist" if own else "◀️ Community",
            callback_data="menu:watchlist" if own else "wlp:0",
        ),
        InlineKeyboardButton(text="🏠 Home", callback_data="menu:home"),
    )
    name = "My titles" if own else f"{owner.first_name}’s watchlist"
    text = f"📚 <b>{safe_html(name)}</b>\n\nPage {page + 1} of {pages}"
    if not entries:
        text += "\n\nNo titles have been added."
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
                )
            )
        builder.row(
            InlineKeyboardButton(text="🗑 Remove from list", callback_data=f"wld:{entry.id}")
        )
    if content_available and entry.content_id:
        builder.row(
            InlineKeyboardButton(
                text="🎞 Open catalog title", callback_data=f"ct:{entry.content_id}:0:0"
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="◀️ My titles" if own else f"◀️ {owner.first_name}’s list",
            callback_data=(f"wlm:{page}" if own else f"wlv:{owner.telegram_user_id}:{page}"),
        )
    )
    availability = "Available in catalog" if content_available else "Text-only watchlist entry"
    text = (
        f"📚 <b>{safe_html(entry.title)}</b>\n\n"
        f"Category: {safe_html(entry.category_name)}\n"
        f"Status: {_watch_status_label(entry.status)}\n"
        f"Source: {availability}"
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
                text=compact_label(f"👤 {user.first_name}{username} — {len(user.watchlist)}", 58),
                callback_data=f"wlv:{user.telegram_user_id}:0",
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
    text = f"🌐 <b>Community watchlists</b>\n\nPage {page + 1} of {pages}"
    if not users:
        text += "\n\nNo other public watchlists are available."
    return text, builder.as_markup()


def admin_dashboard() -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🗂 Categories", callback_data="admin:categories"),
        InlineKeyboardButton(text="🎞 Catalog", callback_data="admin:files"),
    )
    builder.row(
        InlineKeyboardButton(text="❌ Index failures", callback_data="admin:failures"),
        InlineKeyboardButton(text="👥 Users", callback_data="admin:users"),
    )
    builder.row(
        InlineKeyboardButton(text="📊 Statistics", callback_data="admin:stats"),
        InlineKeyboardButton(text="💾 Database", callback_data="admin:database"),
    )
    builder.row(
        InlineKeyboardButton(text="🔐 Access", callback_data="admin:access"),
        InlineKeyboardButton(text="⚙️ Settings", callback_data="admin:settings"),
    )
    builder.row(InlineKeyboardButton(text="📜 Audit", callback_data="admin:audit"))
    builder.row(InlineKeyboardButton(text="🏠 User menu", callback_data="menu:home"))
    return "🛡 <b>Admin panel</b>\n\nChoose an area to manage:", builder.as_markup()


def admin_categories(categories: list[Category]) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    for item in categories:
        status = "✅" if item.enabled else "⏸"
        builder.row(
            InlineKeyboardButton(
                text=compact_label(f"{status} {item.name}"), callback_data=f"ac:{item.id}"
            )
        )
    builder.row(InlineKeyboardButton(text="➕ Add category", callback_data="aca:start"))
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    return f"🗂 <b>Categories</b>\n\nConfigured: {len(categories)}", builder.as_markup()


def admin_category_detail(category: Category) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Rename", callback_data=f"acr:{category.id}"),
        InlineKeyboardButton(text="🔄 Change channel", callback_data=f"acc:{category.id}"),
    )
    builder.row(
        InlineKeyboardButton(text="⚙️ Change mode", callback_data=f"acm:{category.id}"),
        InlineKeyboardButton(
            text="⏸ Disable" if category.enabled else "✅ Enable",
            callback_data=f"act:{category.id}",
        ),
    )
    builder.row(InlineKeyboardButton(text="◀️ Categories", callback_data="admin:categories"))
    text = (
        f"🗂 <b>{safe_html(category.name)}</b>\n\n"
        f"ID: <code>{category.id}</code>\n"
        f"Channel: {safe_html(category.channel_title or 'Unknown')}\n"
        f"Channel ID: <code>{category.active_channel_id}</code>\n"
        f"Mode: {category.mode.value}\n"
        f"Status: {'Enabled' if category.enabled else 'Disabled'}\n"
        f"Legacy channels: {len(category.legacy_channel_ids)}"
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
            InlineKeyboardButton(text=label + selected, callback_data=f"access:{mode.value}")
        )
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    return (
        (
            f"🔐 <b>Access mode</b>\n\nCurrent mode: <b>{current.value}</b>\n\n"
            "Changing the mode never unbans suspended or banned users."
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
                text=f"{icon} {status.value.title()} — {counts[status]}",
                callback_data=f"aul:{status.value}:0",
            )
        )
    builder.row(InlineKeyboardButton(text="🔎 Find user", callback_data="auf:start"))
    builder.row(InlineKeyboardButton(text="◀️ Admin panel", callback_data="admin:home"))
    return f"👥 <b>Users</b>\n\nTotal registered: {len(users)}", builder.as_markup()


def user_detail(user: UserProfile) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Activate", callback_data=f"aus:{user.telegram_user_id}:active"
        ),
        InlineKeyboardButton(
            text="⏸ Suspend", callback_data=f"aus:{user.telegram_user_id}:suspended"
        ),
    )
    builder.row(
        InlineKeyboardButton(text="⛔ Ban", callback_data=f"aus:{user.telegram_user_id}:banned")
    )
    builder.row(InlineKeyboardButton(text="◀️ Users", callback_data="admin:users"))
    username = f"@{safe_html(user.username)}" if user.username else "None"
    text = (
        f"👤 <b>{safe_html(user.first_name)}</b>\n\n"
        f"User ID: <code>{user.telegram_user_id}</code>\n"
        f"Username: {username}\n"
        f"Status: <b>{user.status.value}</b>\n"
        f"Joined: {safe_html(user.created_at)}\n"
        f"Watchlist entries: {len(user.watchlist)}"
    )
    return text, builder.as_markup()

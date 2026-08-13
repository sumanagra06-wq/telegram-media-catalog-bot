from __future__ import annotations

from typing import Any, Literal

from aiogram.types import InlineKeyboardButton

ButtonStyle = Literal["primary", "success", "danger"]


def infer_button_style(text: str, callback_data: str | None = None) -> ButtonStyle | None:
    """Choose a semantic Bot API 9.4 color while retaining client fallback styling."""

    text_value = text.casefold()
    value = f"{text} {callback_data or ''}".casefold()

    # Reversible navigation and dismissive actions intentionally retain Telegram's
    # neutral default appearance, even when their callback points into a risky flow.
    secondary_signals = (
        "cancel",
        "previous",
        "main menu",
        "user menu",
        "home",
        "make my list private",
        "suspend",
        "disable",
    )
    if text_value.startswith(("◀", "⬅", "↩", "back")) or any(
        signal in text_value for signal in secondary_signals
    ):
        return None

    danger_signals = (
        "permanently delete",
        "permanent removal",
        "delete",
        "remove",
        "ban",
        "manually deleted",
    )
    if any(signal in value for signal in danger_signals):
        return "danger"

    success_signals = (
        "confirm",
        "approve",
        "create",
        "add title",
        "get file",
        "download",
        "activate",
        "enable",
        "apply",
        "publish",
        "mark available",
        "completed",
        "export backup",
        "share my list",
        "retry",
    )
    if any(signal in value for signal in success_signals):
        return "success"

    primary_signals = (
        "search",
        "browse",
        "recently added",
        "my watchlist",
        "my titles",
        "community",
        "from catalog",
        "manual title",
        "catalog",
        "categories",
        "users",
        "statistics",
        "database",
        "access",
        "settings",
        "audit",
        "admin panel",
        "season ",
        "episode ",
        "complete season pack",
        "open catalog",
        "to watch",
        "next",
    )
    if any(signal in value for signal in primary_signals):
        return "primary"

    callback_prefixes = (
        "browse:",
        "ct:",
        "se:",
        "ep:",
        "fl:",
        "pk:",
        "wle:",
        "wlv:",
        "wved:",
        "wamc:",
        "wacc:",
        "wacp:",
        "adrt:",
        "au:",
    )
    if callback_data and callback_data.startswith(callback_prefixes):
        return "primary"
    return None


class ActionButton(InlineKeyboardButton):
    """Inline button with centrally inferred semantic color styling."""

    def __init__(self, **data: Any) -> None:
        # An explicit ``style=None`` opts into Telegram's neutral default.
        if "style" not in data:
            data["style"] = infer_button_style(
                str(data.get("text", "")),
                data.get("callback_data"),
            )
        super().__init__(**data)

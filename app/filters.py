from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from .config import Config


class OwnerFilter(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, config: Config) -> bool:
        return config.is_owner(event.from_user.id if event.from_user else None)


class NotOwnerFilter(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, config: Config) -> bool:
        return not config.is_owner(event.from_user.id if event.from_user else None)

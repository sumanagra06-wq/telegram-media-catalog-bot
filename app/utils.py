from __future__ import annotations

import re
import secrets
import unicodedata
from datetime import UTC, datetime
from html import escape


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def make_id(prefix: str, nbytes: int = 6) -> str:
    return f"{prefix}_{secrets.token_urlsafe(nbytes).rstrip('=')}"


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def slugify(value: str) -> str:
    slug = normalize_title(value).replace(" ", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    return slug.strip("-") or "category"


def safe_html(value: object) -> str:
    return escape(str(value), quote=False)


def compact_label(value: str, limit: int = 48) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"

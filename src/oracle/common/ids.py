"""Human-readable, time-sortable identifiers, e.g. `EXP-20261005T130712-9f2c41ab`."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

_PREFIX_RE = re.compile(r"^[A-Z]{1,8}$")
_ID_RE = re.compile(r"^(?P<prefix>[A-Z]{1,8})-(?P<ts>\d{8}T\d{6})-(?P<rand>[0-9a-f]{8})$")


def new_id(prefix: str, *, now: datetime | None = None) -> str:
    if not _PREFIX_RE.fullmatch(prefix):
        raise ValueError(f"id prefix must be 1-8 uppercase letters, got {prefix!r}")
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    stamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(4)}"


def is_valid_id(value: str, prefix: str | None = None) -> bool:
    match = _ID_RE.fullmatch(value)
    return match is not None and (prefix is None or match["prefix"] == prefix)

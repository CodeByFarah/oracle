"""Deterministic hashing used for dataset, config and provenance fingerprints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_CHUNK_BYTES = 1 << 20


def canonical_json(obj: Any) -> bytes:
    """Serialize JSON-native data to a byte-stable form (sorted keys, no whitespace).

    Only JSON-native types are accepted; callers convert models first
    (e.g. `model.model_dump(mode="json")`) so that encoding is never implicit.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()

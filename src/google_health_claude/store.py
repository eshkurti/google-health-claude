"""Writing JSON files deterministically so git diffs only show real changes."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any


def dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def day_path(data_dir: Path, category: str, day: date) -> Path:
    return Path(data_dir) / category / f"{day.isoformat()}.json"


def load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_if_changed(path: Path, payload: Any) -> bool:
    """Write ``payload`` to ``path``. Returns True if the file changed.

    The new content goes to a temporary file first and is then swapped in, so an
    interrupted run never leaves a truncated JSON file behind.
    """
    path = Path(path)
    text = dumps(payload)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return True


def write_bytes_if_changed(path: Path, content: bytes) -> bool:
    """Byte version of ``write_if_changed`` (same atomic swap)."""
    path = Path(path)
    if path.exists() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)
    return True


def _item_key(item: Any) -> str:
    # Google APIs usually give each resource a unique "name" (not yet confirmed
    # for Google Health data points). Without one, the whole item is its identity.
    name = item.get("name") if isinstance(item, dict) else None
    return name if isinstance(name, str) and name else dumps(item)


def merge_items(existing: list, new: list, key: Callable[[Any], str | None] | None = None) -> list:
    """Merge ``new`` into ``existing`` without dropping anything already stored.

    Items with the same identity are replaced in place by their new version;
    unseen items are appended in API order. Merging the same data again returns
    an identical list, so the file stays byte-stable. ``key`` overrides the
    identity (e.g. a sample timestamp); when it returns None the default is used.
    """

    def identity(item: Any) -> str:
        custom = key(item) if key else None
        return custom if custom else _item_key(item)

    new_by_key = {identity(item): item for item in new}
    merged: list = []
    seen: set[str] = set()
    for item in [*existing, *new]:
        key_ = identity(item)
        if key_ in seen:
            continue
        seen.add(key_)
        merged.append(new_by_key.get(key_, item))
    return merged

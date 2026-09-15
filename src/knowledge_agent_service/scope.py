"""Workspace scope normalization.

The extension historically wrote two different identifiers for the same project:
a 20-character sha256 digest of the workspace folder URIs (the chat / MCP path)
and the raw ``file://`` URI (the document / Continuity import path). Retrieval
filtered on the exact string, so a caller only ever saw the half it had written.

This module expands one caller-supplied value into every identifier that may
have been stored for the same workspace, so read and write paths agree without
rewriting historical rows.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

HASH_LENGTH = 20
_HEX_HASH = re.compile(r"^[0-9a-f]{20}$")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def is_hash(value: str) -> bool:
    return bool(_HEX_HASH.match(str(value or "").strip()))


def canonical_workspace_id(value: str) -> str:
    """Return the stable key new records should be written with."""
    text = str(value or "").strip()
    if not text:
        return ""
    if is_hash(text):
        return text
    return _digest(text)


def _local_path(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("file://"):
        return text[len("file://") :]
    return text


def workspace_aliases(*values: str) -> list[str]:
    """Return every stored identifier that can denote one of these workspaces."""
    aliases: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        path = _local_path(text).rstrip("/")
        candidates = (text, text.rstrip("/"), path, PurePosixPath(path).name if path else "")
        for candidate in candidates:
            for item in (candidate, _digest(candidate) if candidate else ""):
                if item and item not in aliases:
                    aliases.append(item)
    return aliases


def hash_aliases(aliases: list[str]) -> list[str]:
    """Hash-shaped aliases also have suffixed variants in the store (``<hash>-1``)."""
    return [alias for alias in aliases if is_hash(alias)]

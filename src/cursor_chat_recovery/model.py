"""Typed data classes for workspaces and chats. Pure data, no behavior."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

Health = Literal["ok", "obsolete", "config-missing", "orphan", "empty-storage"]
Role = Literal["user", "assistant", "system", "tool", "unknown"]


@dataclass(frozen=True)
class WorkspaceIdentifier:
    id: str
    uri: str | None
    scheme: str | None
    is_remote: bool
    remote_host: str | None
    config_path: str | None


@dataclass(frozen=True)
class Workspace:
    identifier: WorkspaceIdentifier
    chat_count: int
    first_chat_at: datetime | None
    last_chat_at: datetime | None
    storage_dir_exists: bool
    is_obsolete: bool
    config_exists: bool
    display_name: str
    health: Health


@dataclass(frozen=True)
class ChatHeader:
    composer_id: str
    name: str | None
    created_at: datetime
    last_updated_at: datetime | None
    workspace_id: str
    subtitle: str | None
    bubble_count_hint: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class Bubble:
    bubble_id: str
    role: Role
    text: str
    created_at: datetime | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ChatDetail:
    header: ChatHeader
    bubbles: list[Bubble]
    composer_data_raw: dict[str, Any]


@dataclass(frozen=True)
class ReassignResult:
    backup_path: str
    composer_ids: list[str]
    from_workspace_ids: list[str]
    to_workspace_id: str
    membership_entries_removed: int = 0


@dataclass(frozen=True)
class MergeResult:
    backup_path: str
    source_workspace_id: str
    target_workspace_id: str
    chats_moved: int


@dataclass(frozen=True)
class SchemaReport:
    ok: bool
    schema_version: str
    missing: list[str]
    unexpected: list[str]
    samples: dict[str, Any]

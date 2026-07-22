"""High-level operations over Storage. The only module that mutates."""
from __future__ import annotations

import copy
import json as _json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from cursor_chat_recovery.model import (
    Bubble,
    ChatDetail,
    ChatHeader,
    MergeResult,
    ReassignResult,
    Workspace,
    WorkspaceIdentifier,
)
from cursor_chat_recovery.paths import decode_ssh_remote_host
from cursor_chat_recovery.storage import Storage


def _parse_workspace_identifier(raw: dict[str, Any]) -> WorkspaceIdentifier:
    wid_id = raw.get("id", "")
    config = raw.get("configPath") or {}
    uri_obj = raw.get("uri") or {}
    config_external = config.get("external")
    uri_external = uri_obj.get("external")
    external = config_external or uri_external
    scheme = (config.get("scheme") or uri_obj.get("scheme"))
    is_remote = scheme == "vscode-remote"
    authority = uri_obj.get("authority") or config.get("authority")
    return WorkspaceIdentifier(
        id=str(wid_id),
        uri=external,
        scheme=scheme,
        is_remote=is_remote,
        remote_host=decode_ssh_remote_host(authority),
        config_path=config_external,
    )


def _coerce_ts(ts: Any) -> datetime | None:
    """Best-effort conversion of a Cursor timestamp to datetime.

    Cursor stores timestamps inconsistently: epoch-milliseconds as int or numeric
    string, ISO-8601 strings, or omits them entirely. Returns None when unparseable.
    """
    if ts is None or ts == "":
        return None
    if isinstance(ts, bool):  # bool is an int subclass; treat as no timestamp
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000)
    if isinstance(ts, str):
        try:
            return datetime.fromtimestamp(int(ts) / 1000)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _display_name(ident: WorkspaceIdentifier) -> str:
    if ident.id == "empty-window":
        return "No folder (empty window)"
    if ident.config_path and "Workspaces/" in ident.config_path:
        ts = ident.config_path.rstrip("/").split("/")[-2] if "/" in ident.config_path else "?"
        return f"Untitled ({ts})"
    if ident.uri:
        return ident.uri.rstrip("/").split("/")[-1] or ident.uri
    return ident.id


def list_workspaces(
    storage_: Storage,
    workspace_storage_dir: Path,
    workspaces_config_dir: Path | None = None,
) -> list[Workspace]:
    headers_data = storage_.read_headers()
    all_headers = headers_data.get("allComposers", [])

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identifiers: dict[str, WorkspaceIdentifier] = {}
    for h in all_headers:
        wid_raw = h.get("workspaceIdentifier") or {}
        ident = _parse_workspace_identifier(wid_raw)
        grouped[ident.id].append(h)
        identifiers.setdefault(ident.id, ident)

    on_disk_ids: set[str] = set()
    obsolete_ids: set[str] = set()
    if workspace_storage_dir.exists():
        for d in workspace_storage_dir.iterdir():
            if d.is_dir():
                on_disk_ids.add(d.name)
                if (d / "obsolete").exists():
                    obsolete_ids.add(d.name)

    results: list[Workspace] = []
    seen_ids: set[str] = set()

    for ws_id, hs in grouped.items():
        ident = identifiers[ws_id]
        created_ats: list[int] = [h["createdAt"] for h in hs if isinstance(h.get("createdAt"), int)]
        first = datetime.fromtimestamp(min(created_ats) / 1000) if created_ats else None
        last = datetime.fromtimestamp(max(created_ats) / 1000) if created_ats else None
        on_disk = ws_id in on_disk_ids
        is_obs = ws_id in obsolete_ids
        config_exists = True
        # Only check config existence for local-file configs. Remote (vscode-remote)
        # config paths can't be stat'd on this machine, so we never flag them missing.
        if ident.config_path and workspaces_config_dir is not None and not ident.is_remote:
            from urllib.parse import unquote, urlparse
            p = unquote(urlparse(ident.config_path).path)
            if p.startswith("/") and len(p) > 2 and p[2] == ":":
                p = p[1:]
            config_exists = Path(p).exists()
        if not on_disk and hs:
            health = "orphan"
        elif is_obs:
            health = "obsolete"
        elif not config_exists:
            health = "config-missing"
        else:
            health = "ok"
        results.append(Workspace(
            identifier=ident,
            chat_count=len(hs),
            first_chat_at=first,
            last_chat_at=last,
            storage_dir_exists=on_disk,
            is_obsolete=is_obs,
            config_exists=config_exists,
            display_name=_display_name(ident),
            health=health,  # type: ignore[arg-type]
        ))
        seen_ids.add(ws_id)

    for ws_id in sorted(on_disk_ids - seen_ids):
        ident = WorkspaceIdentifier(id=ws_id, uri=None, scheme=None,
                                    is_remote=False, remote_host=None, config_path=None)
        results.append(Workspace(
            identifier=ident,
            chat_count=0,
            first_chat_at=None,
            last_chat_at=None,
            storage_dir_exists=True,
            is_obsolete=(ws_id in obsolete_ids),
            config_exists=True,
            display_name=_display_name(ident),
            health="empty-storage",
        ))

    results.sort(key=lambda w: w.last_chat_at or datetime.min, reverse=True)
    return results


def _parse_header(h: dict[str, Any], bubble_count: int | None) -> ChatHeader:
    cid = h["composerId"]
    created = _coerce_ts(h.get("createdAt")) or datetime.min
    last_dt = _coerce_ts(h.get("lastUpdatedAt"))
    ws_id = (h.get("workspaceIdentifier") or {}).get("id", "")
    return ChatHeader(
        composer_id=cid,
        name=h.get("name"),
        created_at=created,
        last_updated_at=last_dt,
        workspace_id=str(ws_id),
        subtitle=h.get("subtitle"),
        bubble_count_hint=bubble_count,
        raw=h,
    )


_ROLE_BY_TYPE: dict[int, str] = {1: "user", 2: "assistant"}


def _parse_bubble(key: str, raw: dict[str, Any]) -> Bubble:
    bubble_id = raw.get("bubbleId") or key.rsplit(":", 1)[-1]
    role = _ROLE_BY_TYPE.get(raw.get("type"), "unknown")  # type: ignore[arg-type]
    return Bubble(
        bubble_id=str(bubble_id),
        role=role,  # type: ignore[arg-type]
        text=str(raw.get("text") or raw.get("richText") or ""),
        created_at=_coerce_ts(raw.get("createdAt")),
        raw=raw,
    )


def load_chat(storage_: Storage, composer_id: str) -> ChatDetail:
    composer_data = storage_.read_kv(f"composerData:{composer_id}")
    if composer_data is None:
        raise KeyError(f"composerData not found for {composer_id}")
    bubble_rows = storage_.read_kv_by_prefix(f"bubbleId:{composer_id}:")
    bubbles = [_parse_bubble(k, v) for k, v in bubble_rows]
    bubbles.sort(key=lambda b: b.created_at or datetime.min)

    headers_data = storage_.read_headers()
    header_raw = next(
        (h for h in headers_data.get("allComposers", []) if h.get("composerId") == composer_id),
        None,
    )
    if header_raw is None:
        raise KeyError(f"header not found for {composer_id}")
    header = _parse_header(header_raw, bubble_count=len(bubbles))
    return ChatDetail(header=header, bubbles=bubbles, composer_data_raw=composer_data)


def list_chats(
    storage_: Storage,
    workspace_id: str,
    limit: int | None = None,
) -> list[ChatHeader]:
    headers_data = storage_.read_headers()
    matched = [
        h for h in headers_data.get("allComposers", [])
        if (h.get("workspaceIdentifier") or {}).get("id") == workspace_id
    ]
    matched.sort(key=lambda h: h.get("lastUpdatedAt") or h.get("createdAt", 0), reverse=True)
    if limit is not None:
        matched = matched[:limit]
    counts = storage_.count_kv_grouped("bubbleId:")
    return [_parse_header(h, counts.get(h["composerId"], 0)) for h in matched]


def export_chat(chat: ChatDetail, fmt: Literal["markdown", "json"] = "markdown") -> str:
    if fmt == "json":
        return _json.dumps({
            "composer_id": chat.header.composer_id,
            "name": chat.header.name,
            "workspace_id": chat.header.workspace_id,
            "created_at": chat.header.created_at.isoformat(),
            "bubbles": [
                {
                    "bubble_id": b.bubble_id,
                    "role": b.role,
                    "text": b.text,
                    "created_at": b.created_at.isoformat() if b.created_at else None,
                }
                for b in chat.bubbles
            ],
        }, indent=2)

    lines = [f"# {chat.header.name or chat.header.composer_id}", ""]
    lines.append(f"_Created: {chat.header.created_at.isoformat()}  ·  "
                 f"workspace: {chat.header.workspace_id}_")
    lines.append("")
    for b in chat.bubbles:
        lines.append(f"## {b.role.upper()}")
        if b.created_at:
            lines.append(f"_{b.created_at.isoformat()}_")
        lines.append("")
        lines.append(b.text)
        lines.append("")
    return "\n".join(lines)


PROJECT_MEMBERSHIP_KEY = "glass.localAgentProjectMembership.v1"


def _drop_stale_project_membership(storage_: Storage, composer_ids: list[str]) -> int:
    """Remove moved chats from Cursor's agent-project membership map.

    The Agents surface groups chats by project via this ItemTable key
    (composerId -> projectId), and membership takes precedence over the header's
    workspaceIdentifier. A stale entry keeps a moved chat grouped under its old
    workspace's project and can rewrite the chat's identifier back at launch
    (Cursor's ensureLocalAssignment/_alignAgentIdentifierToRow reconciliation).
    Dropping the entries is safe: Cursor re-assigns membership from the header's
    workspaceIdentifier on next launch.
    """
    raw = storage_.read_item(PROJECT_MEMBERSHIP_KEY)
    if raw is None:
        return 0
    try:
        membership = _json.loads(raw)
    except _json.JSONDecodeError:
        return 0
    if not isinstance(membership, dict):
        return 0
    moved = set(composer_ids)
    kept = {k: v for k, v in membership.items() if k not in moved}
    removed = len(membership) - len(kept)
    if removed:
        storage_.write_item(
            PROJECT_MEMBERSHIP_KEY,
            _json.dumps(kept, separators=(",", ":"), ensure_ascii=False),
            op_label="membership_cleanup",
        )
    return removed


def reassign_chats(
    storage_: Storage,
    composer_ids: list[str],
    target_ws_id: str,
) -> ReassignResult:
    headers_data = storage_.read_headers()
    headers_list = headers_data.get("allComposers", [])
    target_wid = None
    for h in headers_list:
        wid = h.get("workspaceIdentifier") or {}
        if wid.get("id") == target_ws_id:
            target_wid = copy.deepcopy(wid)
            break
    if target_wid is None:
        target_wid = {"id": target_ws_id}

    from_ids: list[str] = []
    new_list = []
    for h in headers_list:
        if h.get("composerId") in composer_ids:
            from_ids.append((h.get("workspaceIdentifier") or {}).get("id", ""))
            new_h = copy.deepcopy(h)
            new_h["workspaceIdentifier"] = copy.deepcopy(target_wid)
            new_list.append(new_h)
        else:
            new_list.append(h)
    new_data = dict(headers_data)
    new_data["allComposers"] = new_list

    backup_path = storage_.write_headers(new_data, op_label=f"reassign_to_{target_ws_id[:12]}")
    membership_removed = _drop_stale_project_membership(storage_, composer_ids)
    return ReassignResult(
        backup_path=str(backup_path),
        composer_ids=composer_ids,
        from_workspace_ids=sorted(set(from_ids)),
        to_workspace_id=target_ws_id,
        membership_entries_removed=membership_removed,
    )


def merge_workspaces(
    storage_: Storage,
    source_ws_id: str,
    target_ws_id: str,
) -> MergeResult:
    if source_ws_id == target_ws_id:
        raise ValueError("source and target workspace ids must differ")
    headers_data = storage_.read_headers()
    composer_ids = [
        h["composerId"] for h in headers_data.get("allComposers", [])
        if (h.get("workspaceIdentifier") or {}).get("id") == source_ws_id
    ]
    if not composer_ids:
        return MergeResult(
            backup_path="",
            source_workspace_id=source_ws_id,
            target_workspace_id=target_ws_id,
            chats_moved=0,
        )
    res = reassign_chats(storage_, composer_ids, target_ws_id)
    return MergeResult(
        backup_path=res.backup_path,
        source_workspace_id=source_ws_id,
        target_workspace_id=target_ws_id,
        chats_moved=len(composer_ids),
    )

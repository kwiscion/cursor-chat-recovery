"""CLI entry point. Default action: launch TUI. Flags enable headless operations."""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from cursor_chat_recovery import operations, paths, schema, storage

try:
    __version__ = version("cursor-chat-recovery")
except PackageNotFoundError:  # running from source without install
    __version__ = "0.0.0+dev"


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path | None]:
    """Each override applies on its own; only what was not given falls back to the
    installed Cursor's locations."""
    if args.global_db and args.workspace_storage:
        return Path(args.global_db), Path(args.workspace_storage), None
    loc = paths.locate_cursor_dirs()
    global_db = Path(args.global_db) if args.global_db else loc.global_storage_db
    if args.workspace_storage:
        return global_db, Path(args.workspace_storage), None
    return global_db, loc.workspace_storage_dir, loc.workspaces_dir


def _resolve_global_db(args: argparse.Namespace) -> Path:
    if args.global_db:
        return Path(args.global_db)
    return paths.locate_cursor_dirs().global_storage_db


def _do_list(args: argparse.Namespace) -> int:
    global_db, ws_dir, workspaces_dir = _resolve_paths(args)
    with storage.Storage.open_readonly(global_db) as s:
        report = schema.detect_mismatch(s.connection)
        if not report.ok:
            print(schema.agent_prompt(report), file=sys.stderr)
            return 2
        ws_list = operations.list_workspaces(s, ws_dir, workspaces_config_dir=workspaces_dir)
    if args.json:
        print(json.dumps([{
            "id": w.identifier.id,
            "display_name": w.display_name,
            "chat_count": w.chat_count,
            "health": w.health,
            "last_chat_at": w.last_chat_at.isoformat() if w.last_chat_at else None,
            "uri": w.identifier.uri,
        } for w in ws_list], indent=2))
    else:
        for w in ws_list:
            last = w.last_chat_at.strftime("%Y-%m-%d %H:%M") if w.last_chat_at else "-"
            wid = w.identifier.id
            print(f"{w.health:14}  {w.chat_count:>4}  {last}  {w.display_name}  [{wid}]")
    return 0


def _do_export(args: argparse.Namespace) -> int:
    global_db = _resolve_global_db(args)
    with storage.Storage.open_readonly(global_db) as s:
        chat = operations.load_chat(s, args.export)
        out = operations.export_chat(chat, fmt=args.format)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
    else:
        print(out)
    return 0


def _do_reassign(args: argparse.Namespace) -> int:
    if not args.yes:
        print("error: --reassign requires --yes for non-interactive mutation", file=sys.stderr)
        return 1
    global_db = _resolve_global_db(args)
    print(f"Target DB: {global_db}")
    backup_dir = (
        Path(args.backup_dir) if args.backup_dir
        else Path.home() / ".cursor-chat-recovery" / "backups"
    )
    cursor_check: Callable[[], bool] = (
        (lambda: False) if args.no_cursor_check
        else storage._default_cursor_running_check
    )
    composer_ids = args.reassign[0].split(",")
    target_ws_id = args.reassign[1]
    s = storage.Storage.open_rw(global_db, backup_dir=backup_dir, cursor_running_check=cursor_check)
    s.ensure_session_backup()
    res = operations.reassign_chats(s, composer_ids, target_ws_id=target_ws_id)
    s.close()
    print(f"Reassigned {len(res.composer_ids)} chat(s) to {res.to_workspace_id}")
    if res.backup_path:
        print(f"Headers backup: {res.backup_path}")
    return 0


def _do_merge(args: argparse.Namespace) -> int:
    if not args.yes:
        print("error: --merge requires --yes for non-interactive mutation", file=sys.stderr)
        return 1
    global_db = _resolve_global_db(args)
    print(f"Target DB: {global_db}")
    backup_dir = (
        Path(args.backup_dir) if args.backup_dir
        else Path.home() / ".cursor-chat-recovery" / "backups"
    )
    cursor_check: Callable[[], bool] = (
        (lambda: False) if args.no_cursor_check
        else storage._default_cursor_running_check
    )
    src, tgt = args.merge
    s = storage.Storage.open_rw(global_db, backup_dir=backup_dir, cursor_running_check=cursor_check)
    s.ensure_session_backup()
    res = operations.merge_workspaces(s, source_ws_id=src, target_ws_id=tgt)
    s.close()
    print(
        f"Merged {res.chats_moved} chat(s) from "
        f"{res.source_workspace_id} to {res.target_workspace_id}"
    )
    if res.backup_path:
        print(f"Headers backup: {res.backup_path}")
    return 0


def _do_tui(args: argparse.Namespace) -> int:
    from cursor_chat_recovery.tui.app import run_tui
    global_db, ws_dir, workspaces_dir = _resolve_paths(args)
    return run_tui(
        readonly=args.readonly,
        global_db=global_db,
        workspace_storage=ws_dir,
        workspaces_config=workspaces_dir,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cursor-chat-recovery")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--global-db")
    p.add_argument("--workspace-storage")
    p.add_argument("--backup-dir")
    p.add_argument("--readonly", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--no-cursor-check", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--export", metavar="COMPOSER_ID")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("-o", "--output")
    p.add_argument("--reassign", nargs=2, metavar=("COMPOSER_IDS", "TARGET_WS_ID"))
    p.add_argument("--merge", nargs=2, metavar=("SRC_WS_ID", "TARGET_WS_ID"))
    args = p.parse_args(argv)
    if args.list:
        return _do_list(args)
    if args.export:
        return _do_export(args)
    if args.reassign:
        return _do_reassign(args)
    if args.merge:
        return _do_merge(args)
    return _do_tui(args)

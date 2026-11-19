import json as _json
import shutil


def test_cli_list_json(minimal_db, workspace_storage_dir, monkeypatch, capsys):
    from cursor_chat_recovery import cli
    rc = cli.main([
        "--list", "--json",
        "--global-db", str(minimal_db),
        "--workspace-storage", str(workspace_storage_dir),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = _json.loads(out)
    assert any(w["id"] == "ws-alpha" for w in parsed)


def test_cli_export_to_stdout(minimal_db, workspace_storage_dir, capsys):
    from cursor_chat_recovery import cli
    rc = cli.main([
        "--export", "c-alpha-1",
        "--format", "markdown",
        "--global-db", str(minimal_db),
        "--workspace-storage", str(workspace_storage_dir),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# Alpha chat 1" in out


def test_cli_reassign_requires_yes(minimal_db, workspace_storage_dir, tmp_path):
    work = tmp_path / "state.vscdb"
    shutil.copy(minimal_db, work)
    from cursor_chat_recovery import cli
    rc = cli.main([
        "--reassign", "c-alpha-1", "ws-beta",
        "--global-db", str(work),
        "--workspace-storage", str(workspace_storage_dir),
    ])
    assert rc != 0


def test_cli_reassign_with_yes(minimal_db, workspace_storage_dir, tmp_path):
    work = tmp_path / "state.vscdb"
    shutil.copy(minimal_db, work)
    backup_dir = tmp_path / "backups"
    from cursor_chat_recovery import cli
    rc = cli.main([
        "--reassign", "c-alpha-1", "ws-beta",
        "--yes",
        "--global-db", str(work),
        "--workspace-storage", str(workspace_storage_dir),
        "--backup-dir", str(backup_dir),
        "--no-cursor-check",
    ])
    assert rc == 0


def _fake_live_install(tmp_path, monkeypatch, db_source):
    """Stand up a fake 'live' Cursor install and route locate_cursor_dirs() to it."""
    from cursor_chat_recovery import paths
    data_dir = tmp_path / "live" / "Cursor"
    (data_dir / "User" / "globalStorage").mkdir(parents=True)
    (data_dir / "User" / "workspaceStorage").mkdir()
    live_db = data_dir / "User" / "globalStorage" / "state.vscdb"
    shutil.copy(db_source, live_db)
    loc = paths.CursorLocation(
        data_dir=data_dir,
        global_storage_db=live_db,
        workspace_storage_dir=data_dir / "User" / "workspaceStorage",
        workspaces_dir=data_dir / "Workspaces",
    )
    monkeypatch.setattr(paths, "locate_cursor_dirs", lambda: loc)
    return live_db


def _ws_of(db, composer_id):
    from cursor_chat_recovery import storage
    with storage.Storage.open_readonly(db) as s:
        for h in s.read_headers()["allComposers"]:
            if h["composerId"] == composer_id:
                return h["workspaceIdentifier"]["id"]
    return None


def test_cli_reassign_global_db_alone_targets_that_db(minimal_db, tmp_path, monkeypatch, capsys):
    live_db = _fake_live_install(tmp_path, monkeypatch, minimal_db)
    live_before = live_db.read_bytes()
    work = tmp_path / "copy" / "state.vscdb"
    work.parent.mkdir()
    shutil.copy(minimal_db, work)
    from cursor_chat_recovery import cli
    rc = cli.main([
        "--reassign", "c-alpha-1", "ws-beta", "--yes",
        "--global-db", str(work),
        "--backup-dir", str(tmp_path / "backups"),
        "--no-cursor-check",
    ])
    assert rc == 0
    assert _ws_of(work, "c-alpha-1") == "ws-beta"
    assert live_db.read_bytes() == live_before
    assert str(work) in capsys.readouterr().out


def test_cli_merge_global_db_alone_targets_that_db(minimal_db, tmp_path, monkeypatch, capsys):
    live_db = _fake_live_install(tmp_path, monkeypatch, minimal_db)
    live_before = live_db.read_bytes()
    work = tmp_path / "copy" / "state.vscdb"
    work.parent.mkdir()
    shutil.copy(minimal_db, work)
    from cursor_chat_recovery import cli
    rc = cli.main([
        "--merge", "ws-alpha", "ws-beta", "--yes",
        "--global-db", str(work),
        "--backup-dir", str(tmp_path / "backups"),
        "--no-cursor-check",
    ])
    assert rc == 0
    assert _ws_of(work, "c-alpha-1") == "ws-beta"
    assert _ws_of(work, "c-alpha-2") == "ws-beta"
    assert live_db.read_bytes() == live_before
    assert str(work) in capsys.readouterr().out


def test_cli_list_global_db_alone_reads_that_db(
    minimal_db, drift_db, tmp_path, monkeypatch, capsys
):
    _fake_live_install(tmp_path, monkeypatch, drift_db)
    from cursor_chat_recovery import cli
    rc = cli.main(["--list", "--json", "--global-db", str(minimal_db)])
    assert rc == 0
    ids = {w["id"] for w in _json.loads(capsys.readouterr().out)}
    assert "ws-alpha" in ids


def test_cli_export_global_db_alone_needs_no_cursor_install(minimal_db, monkeypatch, capsys):
    from cursor_chat_recovery import cli, paths

    def _no_install() -> paths.CursorLocation:
        raise paths.CursorNotFound("no Cursor here")

    monkeypatch.setattr(paths, "locate_cursor_dirs", _no_install)
    rc = cli.main(["--export", "c-alpha-1", "--global-db", str(minimal_db)])
    assert rc == 0
    assert "# Alpha chat 1" in capsys.readouterr().out


def test_cli_workspace_storage_alone_keeps_default_db(
    minimal_db, workspace_storage_dir, tmp_path, monkeypatch, capsys
):
    _fake_live_install(tmp_path, monkeypatch, minimal_db)
    from cursor_chat_recovery import cli
    rc = cli.main(["--list", "--json", "--workspace-storage", str(workspace_storage_dir)])
    assert rc == 0
    health = {w["id"]: w["health"] for w in _json.loads(capsys.readouterr().out)}
    # ws-alpha only has a storage dir in the fixture tree, not in the fake live install.
    assert health["ws-alpha"] == "ok"

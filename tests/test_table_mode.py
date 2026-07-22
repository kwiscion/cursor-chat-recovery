"""Typed composerHeaders table support (Cursor's composer_header_typed_table gate).

On gated installs the table is authoritative and the legacy
'composer.composerHeaders' blob is dead data: Cursor neither reads nor rewrites
it, so blob-only mutations succeed silently while changing nothing in the UI.
These tests pin the dispatch (read + write), the column/JSON consistency
requirement, and the project-membership cleanup on reassign.
"""
import json
import shutil
import sqlite3
from pathlib import Path

from cursor_chat_recovery import operations, schema, storage


def _rw(db_path: Path, tmp_path: Path) -> storage.Storage:
    work = tmp_path / "state.vscdb"
    shutil.copy(db_path, work)
    s = storage.Storage.open_rw(
        work, backup_dir=tmp_path / "backups", cursor_running_check=lambda: False
    )
    s.ensure_session_backup()
    return s


def test_header_table_mode_detection(table_db, minimal_db):
    with storage.Storage.open_readonly(table_db) as s:
        assert s.header_table_mode() is True
    with storage.Storage.open_readonly(minimal_db) as s:
        assert s.header_table_mode() is False


def test_read_headers_prefers_table_over_stale_blob(table_db):
    with storage.Storage.open_readonly(table_db) as s:
        headers = s.read_headers()["allComposers"]
    ids = {h["composerId"] for h in headers}
    # c-t3 exists only in the table; the stale blob predates it.
    assert ids == {"c-t1", "c-t2", "c-t3"}


def test_reassign_updates_table_column_and_value_json(table_db, tmp_path):
    s = _rw(table_db, tmp_path)
    res = operations.reassign_chats(s, ["c-t1"], target_ws_id="ws-new")
    s.close()
    assert res.to_workspace_id == "ws-new"

    con = sqlite3.connect(f"file:{tmp_path / 'state.vscdb'}?mode=ro", uri=True)
    row = con.execute(
        "SELECT workspaceId, value FROM composerHeaders WHERE composerId='c-t1'"
    ).fetchone()
    # Both stores must move together: Cursor filters on the column
    # (WHERE workspaceId=?) but regenerates it from the value JSON on save.
    assert row[0] == "ws-new"
    assert json.loads(row[1])["workspaceIdentifier"]["id"] == "ws-new"
    # Untouched chat keeps its identity.
    other = con.execute(
        "SELECT workspaceId FROM composerHeaders WHERE composerId='c-t2'"
    ).fetchone()
    assert other[0] == "ws-old"
    con.close()


def test_reassign_in_table_mode_leaves_blob_alone(table_db, tmp_path):
    s = _rw(table_db, tmp_path)
    blob_before = s._read_headers_from_blob()
    operations.reassign_chats(s, ["c-t1"], target_ws_id="ws-new")
    blob_after = s._read_headers_from_blob()
    s.close()
    assert blob_after == blob_before


def test_reassign_drops_stale_project_membership(table_db, tmp_path):
    s = _rw(table_db, tmp_path)
    res = operations.reassign_chats(s, ["c-t1"], target_ws_id="ws-new")
    membership = json.loads(s.read_item("glass.localAgentProjectMembership.v1"))
    s.close()
    # Moved chat's entry removed (it would re-group the chat under the old
    # workspace's project and can revert the identifier at launch); others kept.
    assert res.membership_entries_removed == 1
    assert "c-t1" not in membership
    assert membership["c-other"] == "proj-x"


def test_reassign_roundtrip_identity_in_table_mode(table_db, tmp_path):
    s = _rw(table_db, tmp_path)
    original = s.read_headers()
    res = operations.reassign_chats(s, ["c-t1"], target_ws_id="ws-new")
    backup = json.loads(Path(res.backup_path).read_text(encoding="utf-8"))
    s.write_headers(backup, op_label="undo")
    restored = s.read_headers()
    s.close()
    assert restored == original


def test_merge_workspaces_in_table_mode(table_db, tmp_path):
    s = _rw(table_db, tmp_path)
    res = operations.merge_workspaces(s, source_ws_id="ws-old", target_ws_id="ws-new")
    headers = s.read_headers()["allComposers"]
    s.close()
    assert res.chats_moved == 2
    assert all(h["workspaceIdentifier"]["id"] == "ws-new" for h in headers)


def test_list_workspaces_uses_table(table_db, workspace_storage_dir):
    with storage.Storage.open_readonly(table_db) as s:
        ws_list = operations.list_workspaces(s, workspace_storage_dir)
    by_id = {w.identifier.id: w for w in ws_list}
    # ws-new is invisible in the stale blob; it must appear from the table.
    assert by_id["ws-new"].chat_count == 1
    assert by_id["ws-old"].chat_count == 2


def test_schema_ok_and_reports_table_source(table_db, minimal_db):
    con = sqlite3.connect(f"file:{table_db}?mode=ro", uri=True)
    report = schema.detect_mismatch(con)
    con.close()
    assert report.ok
    assert report.samples["header_source"] == "table"

    con = sqlite3.connect(f"file:{minimal_db}?mode=ro", uri=True)
    report = schema.detect_mismatch(con)
    con.close()
    assert report.ok
    assert report.samples["header_source"] == "blob"


def test_gate_off_with_table_present_uses_blob(table_db, tmp_path):
    # Cursor's switch-back mode: table still exists but the gate is off.
    work = tmp_path / "state.vscdb"
    shutil.copy(table_db, work)
    con = sqlite3.connect(work)
    con.execute(
        "UPDATE ItemTable SET value='false' "
        "WHERE key='composer.composerHeaders.tableGateEnabled'"
    )
    con.commit()
    con.close()
    with storage.Storage.open_readonly(work) as s:
        assert s.header_table_mode() is False
        ids = {h["composerId"] for h in s.read_headers()["allComposers"]}
    assert ids == {"c-t1", "c-t2"}  # blob view
    con = sqlite3.connect(f"file:{work}?mode=ro", uri=True)
    report = schema.detect_mismatch(con)
    con.close()
    assert report.ok
    assert any("gate disabled" in u for u in report.unexpected)

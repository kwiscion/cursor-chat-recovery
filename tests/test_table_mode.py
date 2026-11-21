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
from contextlib import closing
from pathlib import Path

import pytest

from cursor_chat_recovery import operations, schema, storage


def _rw(db_path: Path, tmp_path: Path) -> storage.Storage:
    work = tmp_path / "state.vscdb"
    shutil.copy(db_path, work)
    s = storage.Storage.open_rw(
        work, backup_dir=tmp_path / "backups", cursor_running_check=lambda: False
    )
    s.ensure_session_backup()
    return s


EXTRA_COLUMN_VALUES = {"c-t1": "explore", "c-t2": "shell", "c-t3": ""}


def _rw_with_extra_column(db_path: Path, tmp_path: Path) -> storage.Storage:
    """Like _rw, but the table carries a column the tool doesn't know about, as newer
    Cursor builds do (subagentTypeName lives only in the column, not in the JSON)."""
    work = tmp_path / "state.vscdb"
    shutil.copy(db_path, work)
    con = sqlite3.connect(work)
    con.execute("ALTER TABLE composerHeaders ADD COLUMN subagentTypeName TEXT")
    for cid, val in EXTRA_COLUMN_VALUES.items():
        con.execute(
            "UPDATE composerHeaders SET subagentTypeName=? WHERE composerId=?", (val, cid)
        )
    con.commit()
    con.close()
    s = storage.Storage.open_rw(
        work, backup_dir=tmp_path / "backups", cursor_running_check=lambda: False
    )
    s.ensure_session_backup()
    return s


def _extra_column(db_path: Path) -> dict[str, str | None]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute("SELECT composerId, subagentTypeName FROM composerHeaders").fetchall()
    con.close()
    return dict(rows)


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


def test_reassign_only_changes_workspace_fields_for_moved_rows(table_db, tmp_path):
    with _rw_with_extra_column(table_db, tmp_path) as s:
        with closing(sqlite3.connect(s.db_path)) as con:
            con.row_factory = sqlite3.Row
            with con:
                # Store metadata that cannot be recovered from the header JSON.
                con.execute("UPDATE composerHeaders SET recency = recency + 10, isArchived = 1")
            query = "SELECT * FROM composerHeaders ORDER BY composerId"
            before = [dict(row) for row in con.execute(query)]
            operations.reassign_chats(s, ["c-t1"], target_ws_id="ws-new")
            after = [dict(row) for row in con.execute(query)]

    moved, *untouched = after
    assert untouched == before[1:]
    expected = before[0]
    expected["workspaceId"] = "ws-new"
    expected_header = json.loads(expected.pop("value"))
    expected_header["workspaceIdentifier"] = json.loads(before[2]["value"])["workspaceIdentifier"]
    assert json.loads(moved.pop("value")) == expected_header
    assert moved == expected


def test_reassign_clears_membership_for_actual_moves_only(table_db, tmp_path):
    key = operations.PROJECT_MEMBERSHIP_KEY
    with _rw(table_db, tmp_path) as s:
        membership = {"c-t1": "proj-old", "c-t3": "proj-new", "c-other": "proj-x"}
        s.connection.execute("UPDATE ItemTable SET value = ? WHERE key = ?", (
            json.dumps(membership), key,
        ))
        s.connection.commit()

        res = operations.reassign_chats(s, ["c-other", "c-t3", "c-t1"], target_ws_id="ws-new")
        assert res.composer_ids == ["c-t1"]
        assert res.membership_entries_removed == 1
        assert json.loads(s.read_item(key)) == {"c-t3": "proj-new", "c-other": "proj-x"}


@pytest.mark.parametrize("table_mode", [False, True], ids=["blob", "table"])
def test_reassign_rolls_back_when_membership_write_fails(table_db, tmp_path, table_mode):
    key = operations.PROJECT_MEMBERSHIP_KEY
    with _rw(table_db, tmp_path) as s, closing(sqlite3.connect(s.db_path)) as con:
        with con:
            if not table_mode:
                con.execute(
                    "UPDATE ItemTable SET value = 'false' WHERE key = ?",
                    (storage.TABLE_GATE_KEY,),
                )
            con.execute("""
                CREATE TRIGGER block_membership_write
                BEFORE UPDATE ON ItemTable
                WHEN OLD.key = 'glass.localAgentProjectMembership.v1'
                BEGIN
                    SELECT RAISE(ABORT, 'membership write blocked');
                END
            """)

        headers = s.read_headers()
        membership = s.read_item(key)
        with pytest.raises(sqlite3.IntegrityError, match="membership write blocked"):
            operations.reassign_chats(s, ["c-t1"], target_ws_id="ws-new")

        with storage.Storage.open_readonly(s.db_path) as reopened:
            assert reopened.read_headers() == headers
            assert reopened.read_item(key) == membership

        with con:
            con.execute("DROP TRIGGER block_membership_write")
        res = operations.reassign_chats(s, ["c-t1"], target_ws_id="ws-new")
        assert res.composer_ids == ["c-t1"]
        assert res.membership_entries_removed == 1
        moved = next(h for h in s.read_headers()["allComposers"] if h["composerId"] == "c-t1")
        assert moved["workspaceIdentifier"]["id"] == "ws-new"


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


def test_reassign_preserves_unknown_table_columns(table_db, tmp_path):
    s = _rw_with_extra_column(table_db, tmp_path)
    operations.reassign_chats(s, ["c-t1"], target_ws_id="ws-new")
    s.close()
    # Preserve extra metadata on both moved and untouched rows.
    assert _extra_column(tmp_path / "state.vscdb") == EXTRA_COLUMN_VALUES


def test_merge_preserves_unknown_table_columns(table_db, tmp_path):
    s = _rw_with_extra_column(table_db, tmp_path)
    operations.merge_workspaces(s, source_ws_id="ws-old", target_ws_id="ws-new")
    s.close()
    assert _extra_column(tmp_path / "state.vscdb") == EXTRA_COLUMN_VALUES


def test_undo_roundtrip_preserves_unknown_table_columns(table_db, tmp_path):
    s = _rw_with_extra_column(table_db, tmp_path)
    res = operations.reassign_chats(s, ["c-t1"], target_ws_id="ws-new")
    backup = json.loads(Path(res.backup_path).read_text(encoding="utf-8"))
    s.write_headers(backup, op_label="undo")
    s.close()
    assert _extra_column(tmp_path / "state.vscdb") == EXTRA_COLUMN_VALUES


def test_schema_reports_unknown_table_columns(table_db, tmp_path):
    work = tmp_path / "state.vscdb"
    shutil.copy(table_db, work)
    con = sqlite3.connect(work)
    con.execute("ALTER TABLE composerHeaders ADD COLUMN subagentTypeName TEXT")
    con.commit()
    report = schema.detect_mismatch(con)
    con.close()
    # Informational only: extra columns are preserved on write, so nothing blocks.
    assert report.ok
    assert any("subagentTypeName" in u for u in report.unexpected)


@pytest.mark.parametrize("column", ["value", "recency"])
def test_schema_reports_missing_table_sample_column(column):
    columns = ", ".join(name for name in storage.HEADER_TABLE_COLUMNS if name != column)
    with closing(sqlite3.connect(":memory:")) as con:
        con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(f"CREATE TABLE composerHeaders ({columns})")
        con.execute("INSERT INTO ItemTable VALUES (?, 'true')", (storage.TABLE_GATE_KEY,))
        report = schema.detect_mismatch(con)

    assert not report.ok
    assert report.samples["header_source"] == "table"
    assert report.missing == [f"composerHeaders.column:{column}"]


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

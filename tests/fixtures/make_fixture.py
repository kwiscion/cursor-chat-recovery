"""Generate synthetic state.vscdb fixtures for tests. Run manually when fixtures need refresh.

Usage:
    python tests/fixtures/make_fixture.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

FIXTURES = Path(__file__).parent


def make_header(composer_id, name, created_ms, ws_id, ws_uri=None, ws_config_path=None,
                ws_remote_config=None):
    wid = {"id": ws_id}
    if ws_remote_config:
        # A remote (ssh) multi-folder workspace: configPath is a vscode-remote URI
        # that cannot be stat'd on the local machine.
        wid["configPath"] = {
            "external": ws_remote_config,
            "scheme": "vscode-remote",
            "authority": "ssh-remote+host",
            "path": ws_remote_config.split("host", 1)[-1],
        }
    elif ws_config_path:
        wid["configPath"] = {
            "external": f"file:///{ws_config_path.replace(chr(92), '/')}",
            "scheme": "file",
            "path": "/" + ws_config_path.replace("\\", "/"),
        }
    elif ws_uri:
        wid["uri"] = {"external": ws_uri, "scheme": ws_uri.split("://")[0]}
    return {
        "type": "head",
        "composerId": composer_id,
        "name": name,
        "createdAt": created_ms,
        "lastUpdatedAt": created_ms + 1000,
        "unifiedMode": "agent",
        "forceMode": "edit",
        "isArchived": False,
        "isDraft": False,
        "subtitle": f"Subtitle for {name}",
        "workspaceIdentifier": wid,
    }


def make_bubble(bubble_id, kind, text, ts_ms):
    # kind: 1=user, 2=assistant in Cursor's encoding
    return {
        "bubbleId": bubble_id,
        "type": kind,
        "text": text,
        "createdAt": ts_ms,
    }


def write_db(path: Path, headers: list[dict], composer_to_bubbles: dict[str, list[dict]]):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("composer.composerHeaders", json.dumps({"allComposers": headers})),
    )
    for cid, bubbles in composer_to_bubbles.items():
        composer_data = {
            "composerId": cid,
            "conversation": [b["bubbleId"] for b in bubbles],
            "createdAt": bubbles[0]["createdAt"] if bubbles else 0,
        }
        con.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (f"composerData:{cid}", json.dumps(composer_data)),
        )
        for b in bubbles:
            con.execute(
                "INSERT INTO cursorDiskKV VALUES (?, ?)",
                (f"bubbleId:{cid}:{b['bubbleId']}", json.dumps(b)),
            )
    con.commit()
    con.close()


def make_minimal():
    """3 workspaces: alpha (ok, 2 chats), beta (obsolete, 1 chat), gamma (orphan, 1 chat).
    Plus one empty-storage workspace dir (delta) with no headers.
    Plus one Untitled workspace pointing at a config path that won't exist (epsilon)."""
    headers = [
        make_header("c-alpha-1", "Alpha chat 1", 1_700_000_000_000, "ws-alpha",
                    ws_uri="file:///tmp/alpha"),
        make_header("c-alpha-2", "Alpha chat 2", 1_700_000_100_000, "ws-alpha",
                    ws_uri="file:///tmp/alpha"),
        make_header("c-beta-1", "Beta chat", 1_700_000_200_000, "ws-beta",
                    ws_uri="file:///tmp/beta"),
        make_header("c-gamma-1", "Gamma orphan chat", 1_700_000_300_000, "ws-gamma",
                    ws_uri="file:///tmp/gamma"),
        make_header("c-epsilon-1", "Untitled chat", 1_700_000_400_000, "ws-epsilon",
                    ws_config_path="Workspaces/1700000000000/workspace.json"),
        make_header("c-remote-1", "Remote chat", 1_700_000_050_000, "ws-remote",
                    ws_remote_config="vscode-remote://ssh-remote+host/srv/proj/proj.code-workspace"),
    ]
    bubbles = {
        "c-alpha-1": [
            make_bubble("b1", 1, "Hello", 1_700_000_000_000),
            make_bubble("b2", 2, "Hi there", 1_700_000_001_000),
        ],
        "c-alpha-2": [make_bubble("b3", 1, "Q", 1_700_000_100_000)],
        "c-beta-1": [make_bubble("b4", 1, "B", 1_700_000_200_000)],
        "c-gamma-1": [make_bubble("b5", 1, "G", 1_700_000_300_000)],
        "c-epsilon-1": [make_bubble("b6", 1, "E", 1_700_000_400_000)],
        "c-remote-1": [make_bubble("b7", 1, "R", 1_700_000_050_000)],
    }
    write_db(FIXTURES / "globalStorage_minimal.vscdb", headers, bubbles)

    ws_root = FIXTURES / "workspaceStorage"
    if ws_root.exists():
        shutil.rmtree(ws_root)
    for ws_id in ["ws-alpha", "ws-beta", "ws-delta", "ws-remote"]:
        (ws_root / ws_id).mkdir(parents=True)
        (ws_root / ws_id / "state.vscdb").write_bytes(b"")
    (ws_root / "ws-beta" / "obsolete").write_text("")
    (ws_root / "ws-epsilon").mkdir()
    (ws_root / "ws-epsilon" / "state.vscdb").write_bytes(b"")


def _table_row(header: dict) -> tuple:
    """Derive composerHeaders table columns from a header dict, the way Cursor does."""
    created = header.get("createdAt")
    updated = header.get("lastUpdatedAt")
    is_subagent = bool(
        not header.get("isBestOfNSubcomposer")
        and (str(header.get("composerId", "")).startswith("task-")
             or (header.get("subagentInfo") or {}).get("parentComposerId"))
    )
    return (
        header["composerId"],
        (header.get("workspaceIdentifier") or {}).get("id"),
        created,
        updated,
        1 if header.get("isArchived") else 0,
        1 if is_subagent else 0,
        updated if updated is not None else (created or 0),
        header.get("conversationCheckpointLastUpdatedAt"),
        json.dumps(header),
    )


def make_table_mode():
    """Newer Cursor layout: composer_header_typed_table gate ON.

    The typed composerHeaders table is authoritative; the legacy blob is stale
    (missing one chat, and left exactly as it was at migration time). Also seeds
    the agent-project membership key so reassign's cleanup path is exercised.
    """
    h1 = make_header("c-t1", "Table chat 1", 1_700_000_000_000, "ws-old",
                     ws_uri="file:///tmp/old")
    h2 = make_header("c-t2", "Table chat 2", 1_700_000_100_000, "ws-old",
                     ws_uri="file:///tmp/old")
    h3 = make_header("c-t3", "Table chat 3 (post-migration)", 1_700_000_200_000, "ws-new",
                     ws_uri="file:///tmp/new")
    bubbles = {
        "c-t1": [
            make_bubble("tb1", 1, "Hello", 1_700_000_000_000),
            make_bubble("tb2", 2, "Hi there", 1_700_000_001_000),
        ],
        "c-t2": [make_bubble("tb3", 1, "Q", 1_700_000_100_000)],
        "c-t3": [make_bubble("tb4", 1, "N", 1_700_000_200_000)],
    }
    path = FIXTURES / "globalStorage_table_mode.vscdb"
    # Blob deliberately diverges from the table: it predates c-t3.
    write_db(path, [h1, h2], bubbles)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE composerHeaders ("
        "composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER, "
        "lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER, "
        "recency INTEGER, checkpointAt INTEGER, value TEXT)"
    )
    for h in (h1, h2, h3):
        con.execute(
            "INSERT INTO composerHeaders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _table_row(h),
        )
    con.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("composer.composerHeaders.tableGateEnabled", "true"),
    )
    con.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("glass.localAgentProjectMembership.v1",
         json.dumps({"c-t1": "proj-old", "c-other": "proj-x"})),
    )
    con.commit()
    con.close()


def make_schema_drift():
    """Same shape but composerId renamed to composerID — exercises mismatch detection."""
    headers = [{
        "type": "head",
        "composerID": "c-drift",
        "createdAt": 1_700_000_000_000,
        "workspaceIdentifier": {"id": "ws-drift"},
    }]
    write_db(FIXTURES / "globalStorage_schema_drift.vscdb", headers, {})


def make_corrupt():
    """Missing tables entirely."""
    path = FIXTURES / "globalStorage_corrupt.vscdb"
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE wrong_table (k TEXT)")
    con.commit()
    con.close()


if __name__ == "__main__":
    make_minimal()
    make_table_mode()
    make_schema_drift()
    make_corrupt()
    print("Fixtures written to", FIXTURES)

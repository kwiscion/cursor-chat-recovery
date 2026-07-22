"""SQLite I/O for globalStorage state.vscdb. Read-only here; mutation in a later task."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

HEADERS_BLOB_KEY = "composer.composerHeaders"
HEADER_TABLE = "composerHeaders"
TABLE_GATE_KEY = "composer.composerHeaders.tableGateEnabled"

# Column order must match _header_row_params below.
HEADER_TABLE_COLUMNS = (
    "composerId", "workspaceId", "createdAt", "lastUpdatedAt",
    "isArchived", "isSubagent", "recency", "checkpointAt", "value",
)


def _prefix_range(prefix: str) -> tuple[str, str]:
    """Inclusive/exclusive key range covering all keys starting with prefix.

    Keys in cursorDiskKV are ASCII, so '￿' sorts after any suffix.
    """
    return prefix, prefix + "￿"


def _is_subagent_header(h: dict[str, Any]) -> bool:
    """Mirror of Cursor's isSubagent derivation for the composerHeaders table."""
    if h.get("isBestOfNSubcomposer"):
        return False
    cid = h.get("composerId")
    if isinstance(cid, str) and cid.startswith("task-"):
        return True
    return bool((h.get("subagentInfo") or {}).get("parentComposerId"))


def _header_row_params(h: dict[str, Any]) -> tuple[Any, ...]:
    """Derive the typed-table columns from a header dict, matching Cursor's own
    serialization (workspaceId column always mirrors value.workspaceIdentifier.id)."""
    created = h.get("createdAt")
    updated = h.get("lastUpdatedAt")
    return (
        h.get("composerId"),
        (h.get("workspaceIdentifier") or {}).get("id"),
        created,
        updated,
        1 if h.get("isArchived") else 0,
        1 if _is_subagent_header(h) else 0,
        updated if updated is not None else (created if created is not None else 0),
        h.get("conversationCheckpointLastUpdatedAt"),
        json.dumps(h, separators=(",", ":"), ensure_ascii=False),
    )


class ReadOnlyViolation(Exception):
    """Raised when a write is attempted on a read-only Storage handle."""


class CursorRunning(Exception):
    """Raised when Cursor is detected running and a mutation is attempted."""


def _default_cursor_running_check() -> bool:
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist.exe", "/FI", "IMAGENAME eq Cursor.exe"],
                capture_output=True, text=True, timeout=5,
            )
            return "Cursor.exe" in out.stdout
        else:
            out = subprocess.run(
                ["pgrep", "-x", "Cursor"], capture_output=True, text=True, timeout=5,
            )
            return out.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


class _ConWrapper:
    """Thin Python wrapper around sqlite3.Connection so methods are monkeypatchable."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def commit(self) -> None:
        self._con.commit()

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._con.execute(sql, *args, **kwargs)

    def close(self) -> None:
        self._con.close()


@dataclass
class Storage:
    db_path: Path
    _con: _ConWrapper
    _readonly: bool
    _backup_dir: Path | None = None
    _session_backup_path: Path | None = field(default=None)

    @classmethod
    def open_readonly(cls, db_path: Path) -> Storage:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
        return cls(db_path=Path(db_path), _con=_ConWrapper(con), _readonly=True)

    @classmethod
    def open_rw(
        cls,
        db_path: Path,
        backup_dir: Path,
        cursor_running_check: Callable[[], bool] = _default_cursor_running_check,
    ) -> Storage:
        if cursor_running_check():
            raise CursorRunning("Cursor is running; close it before mutating")
        backup_dir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db_path), timeout=10.0, isolation_level="DEFERRED")
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("ROLLBACK")
        except sqlite3.OperationalError as e:
            con.close()
            raise CursorRunning(f"could not take write lock: {e}") from e
        return cls(
            db_path=Path(db_path), _con=_ConWrapper(con), _readonly=False, _backup_dir=backup_dir
        )

    @property
    def connection(self) -> _ConWrapper:
        return self._con

    @property
    def readonly(self) -> bool:
        return self._readonly

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def ensure_session_backup(self) -> Path:
        if self._readonly:
            raise ReadOnlyViolation("ensure_session_backup on read-only Storage")
        if self._session_backup_path and self._session_backup_path.exists():
            return self._session_backup_path
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.db_path.with_name(self.db_path.name + f".fullbackup.{ts}")
        shutil.copy(self.db_path, path)
        self._session_backup_path = path
        return path

    def read_item(self, key: str) -> str | None:
        """Raw ItemTable read. Returns the value as text, or None when absent."""
        row = self._con.execute(
            "SELECT value FROM ItemTable WHERE key=?", (key,)
        ).fetchone()
        if not row or row[0] is None:
            return None
        val = row[0]
        return val.decode("utf-8") if isinstance(val, bytes) else str(val)

    def _header_table_exists(self) -> bool:
        row = self._con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (HEADER_TABLE,),
        ).fetchone()
        return row is not None

    def header_table_mode(self) -> bool:
        """True when Cursor's typed composerHeaders table is the authoritative store.

        Newer Cursor builds (behind the server-distributed composer_header_typed_table
        feature gate) migrate headers out of the 'composer.composerHeaders' ItemTable
        blob into a typed 'composerHeaders' table, after which the blob is neither
        read nor rewritten. Mutating the blob on such installs is a silent no-op.
        Mirrors Cursor's own check: gate key true + table present.
        """
        if not self._header_table_exists():
            return False
        return self.read_item(TABLE_GATE_KEY) == "true"

    def read_headers(self) -> dict[str, Any]:
        """Headers from the authoritative store, always as {"allComposers": [...]}."""
        if self.header_table_mode():
            return self._read_headers_from_table()
        return self._read_headers_from_blob()

    def _read_headers_from_blob(self) -> dict[str, Any]:
        raw = self.read_item(HEADERS_BLOB_KEY)
        if raw is None:
            return {"allComposers": []}
        return json.loads(raw)  # type: ignore[no-any-return]

    def _read_headers_from_table(self) -> dict[str, Any]:
        rows = self._con.execute(
            f"SELECT value FROM {HEADER_TABLE} ORDER BY recency DESC"
        ).fetchall()
        headers: list[dict[str, Any]] = []
        for (val,) in rows:
            if val is None:
                continue
            try:
                headers.append(json.loads(val))
            except json.JSONDecodeError:
                continue
        return {"allComposers": headers}

    def read_kv(self, key: str) -> dict[str, Any] | None:
        row = self._con.execute(
            "SELECT value FROM cursorDiskKV WHERE key=?", (key,)
        ).fetchone()
        if not row or row[0] is None:
            return None
        return json.loads(row[0])  # type: ignore[no-any-return]

    def read_kv_by_prefix(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        lo, hi = _prefix_range(prefix)
        rows = self._con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?",
            (lo, hi),
        ).fetchall()
        # Some rows carry NULL values in real Cursor DBs; skip those rather than crash.
        return [(str(k), json.loads(v)) for k, v in rows if v is not None]

    def count_kv_by_prefix(self, prefix: str) -> int:
        lo, hi = _prefix_range(prefix)
        row = self._con.execute(
            "SELECT COUNT(*) FROM cursorDiskKV WHERE key >= ? AND key < ?",
            (lo, hi),
        ).fetchone()
        return int(row[0])

    def count_kv_grouped(self, prefix: str) -> dict[str, int]:
        """Count keys under prefix, grouped by the first ':'-delimited segment
        after the prefix (e.g. composer id for 'bubbleId:'). One index-only scan."""
        lo, hi = _prefix_range(prefix)
        rows = self._con.execute(
            "SELECT key FROM cursorDiskKV WHERE key >= ? AND key < ?",
            (lo, hi),
        ).fetchall()
        counts: dict[str, int] = {}
        plen = len(prefix)
        for (key,) in rows:
            group = str(key)[plen:].split(":", 1)[0]
            counts[group] = counts.get(group, 0) + 1
        return counts

    def write_headers(self, headers: dict[str, Any], op_label: str = "op") -> Path:
        if self._readonly:
            raise ReadOnlyViolation("write_headers on read-only Storage")
        assert self._session_backup_path and self._session_backup_path.exists(), \
            "session backup must exist before mutating (call ensure_session_backup first)"
        assert self._backup_dir is not None
        before = self.read_headers()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        op_backup = self._backup_dir / f"composerHeaders_{ts}_{op_label}.json"
        op_backup.write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")
        if self.header_table_mode():
            self._write_headers_to_table(headers)
        else:
            self._write_headers_to_blob(headers)
        self._con.commit()
        self._con.execute("PRAGMA wal_checkpoint(FULL)")
        return op_backup

    def _write_headers_to_blob(self, headers: dict[str, Any]) -> None:
        new_raw = json.dumps(headers, separators=(",", ":"), ensure_ascii=False)
        self._con.execute(
            "UPDATE ItemTable SET value=? WHERE key=?",
            (new_raw, HEADERS_BLOB_KEY),
        )

    def _write_headers_to_table(self, headers: dict[str, Any]) -> None:
        """Upsert every header into the typed table, keeping the derived columns
        (workspaceId, recency, ...) consistent with the value JSON. Cursor regenerates
        the columns from the JSON on its next save, so writing only one of the two
        would be reverted."""
        cols = ", ".join(HEADER_TABLE_COLUMNS)
        placeholders = ", ".join("?" for _ in HEADER_TABLE_COLUMNS)
        sql = f"INSERT OR REPLACE INTO {HEADER_TABLE} ({cols}) VALUES ({placeholders})"
        for h in headers.get("allComposers", []):
            if not h.get("composerId"):
                continue
            self._con.execute(sql, _header_row_params(h))

    def write_item(self, key: str, value: str, op_label: str = "op") -> Path | None:
        """Upsert a raw ItemTable value, backing up the prior value first.

        Returns the backup path, or None when the key was previously absent
        (nothing to back up; the write still happens)."""
        if self._readonly:
            raise ReadOnlyViolation("write_item on read-only Storage")
        assert self._session_backup_path and self._session_backup_path.exists(), \
            "session backup must exist before mutating (call ensure_session_backup first)"
        assert self._backup_dir is not None
        before = self.read_item(key)
        op_backup: Path | None = None
        if before is not None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_key = key.replace("/", "_").replace(".", "_")
            op_backup = self._backup_dir / f"item_{safe_key}_{ts}_{op_label}.txt"
            op_backup.write_text(before, encoding="utf-8")
        self._con.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._con.commit()
        return op_backup

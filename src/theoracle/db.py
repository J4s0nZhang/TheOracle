from __future__ import annotations

import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL").fetchone()
    _apply_migrations(conn)
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   TEXT NOT NULL PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}

    for migration_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if migration_file.name not in applied:
            conn.executescript(migration_file.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)",
                (migration_file.name,),
            )
            conn.commit()

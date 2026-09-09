from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from flask import current_app, g

from .config import DEFAULT_SETTINGS

SCHEMA_VERSION = 1


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK(type IN
    ('ppt_file','pdf_deck','ppt_link','image','url','video')),
  source TEXT NOT NULL,
  embed_url TEXT,
  render_mode TEXT NOT NULL DEFAULT 'auto'
    CHECK(render_mode IN ('auto','live','converted','archive','screenshot')),
  duration_sec INTEGER NOT NULL DEFAULT 20 CHECK(duration_sec >= 0),
  slide_sec INTEGER NOT NULL DEFAULT 10 CHECK(slide_sec > 0),
  volume INTEGER NOT NULL DEFAULT 80 CHECK(volume BETWEEN 0 AND 100),
  fit_mode TEXT NOT NULL DEFAULT 'contain'
    CHECK(fit_mode IN ('contain','cover','stretch')),
  days_mask INTEGER NOT NULL DEFAULT 127 CHECK(days_mask BETWEEN 1 AND 127),
  start_time TEXT,
  end_time TEXT,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  sort_order INTEGER NOT NULL DEFAULT 0,
  active_generation_id INTEGER,
  last_checked TEXT,
  last_good_at TEXT,
  last_status TEXT NOT NULL DEFAULT 'never'
    CHECK(last_status IN ('never','queued','refreshing','ok','stale','error')),
  last_error TEXT,
  deleted_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_generations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  media_item_id INTEGER NOT NULL,
  generation_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  slide_count INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER,
  content_hash TEXT NOT NULL,
  source_etag TEXT,
  source_last_modified TEXT,
  created_at TEXT NOT NULL,
  verified_at TEXT NOT NULL,
  retire_after TEXT,
  FOREIGN KEY(media_item_id) REFERENCES media_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS display_schedule (
  weekday INTEGER PRIMARY KEY CHECK(weekday BETWEEN 0 AND 6),
  mode TEXT NOT NULL DEFAULT 'off' CHECK(mode IN ('off','always_on','window')),
  on_time TEXT,
  off_time TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  media_item_id INTEGER,
  state TEXT NOT NULL CHECK(state IN
    ('queued','running','succeeded','failed','cancelled')),
  progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
  stage TEXT NOT NULL DEFAULT '',
  attempt INTEGER NOT NULL DEFAULT 0,
  not_before TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(media_item_id) REFERENCES media_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_display_override (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  state TEXT NOT NULL CHECK(state IN ('on','off')),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_order ON media_items(enabled, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(state, not_before, id);
CREATE INDEX IF NOT EXISTS idx_generations_item ON media_generations(media_item_id, id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def migrate(path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(media_items)")}
        if "deleted_at" not in columns:
            conn.execute("ALTER TABLE media_items ADD COLUMN deleted_at TEXT")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utcnow()),
        )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )
        for weekday in range(7):
            default_mode = "window" if weekday < 5 else "off"
            conn.execute(
                """INSERT OR IGNORE INTO display_schedule(weekday, mode, on_time, off_time)
                   VALUES (?, ?, ?, ?)""",
                (
                    weekday,
                    default_mode,
                    "07:30" if weekday < 5 else None,
                    "16:00" if weekday < 5 else None,
                ),
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["RUNTIME_CONFIG"].db_path)
    return g.db


def close_db(_error: object = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        """INSERT INTO settings(key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, str(value)),
    )


def bump_playlist_version(conn: sqlite3.Connection) -> int:
    version = int(get_setting(conn, "playlist_version", "0")) + 1
    set_setting(conn, "playlist_version", version)
    return version


def backup_database(source: str | Path, destination: str | Path) -> None:
    src = connect(source)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

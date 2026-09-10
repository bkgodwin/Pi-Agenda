from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from flask import current_app, g

from .config import DEFAULT_SETTINGS

SCHEMA_VERSION = 3


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
    ('ppt_file','pdf_deck','ppt_link','image','url','video','announcement')),
  source TEXT NOT NULL,
  embed_url TEXT,
  render_mode TEXT NOT NULL DEFAULT 'auto'
    CHECK(render_mode IN ('auto','live','converted','archive','screenshot','scroll')),
  duration_sec INTEGER NOT NULL DEFAULT 20 CHECK(duration_sec >= 0),
  slide_sec INTEGER NOT NULL DEFAULT 10 CHECK(slide_sec > 0),
  volume INTEGER NOT NULL DEFAULT 80 CHECK(volume BETWEEN 0 AND 100),
  fit_mode TEXT NOT NULL DEFAULT 'contain'
    CHECK(fit_mode IN ('contain','cover','stretch','width','height','native')),
  web_zoom INTEGER NOT NULL DEFAULT 100 CHECK(web_zoom BETWEEN 50 AND 200),
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
  background_color TEXT NOT NULL DEFAULT '#12372a',
  text_color TEXT NOT NULL DEFAULT '#ffffff',
  text_size INTEGER NOT NULL DEFAULT 64,
  text_align TEXT NOT NULL DEFAULT 'center',
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

CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL COLLATE NOCASE UNIQUE,
  is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  days_mask INTEGER NOT NULL DEFAULT 127 CHECK(days_mask BETWEEN 1 AND 127),
  start_time TEXT,
  end_time TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_items (
  playlist_id INTEGER NOT NULL,
  media_item_id INTEGER NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(playlist_id, media_item_id),
  FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
  FOREIGN KEY(media_item_id) REFERENCES media_items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_media_order ON media_items(enabled, sort_order, id);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(state, not_before, id);
CREATE INDEX IF NOT EXISTS idx_generations_item ON media_generations(media_item_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_default_playlist
  ON playlists(is_default) WHERE is_default = 1;
CREATE INDEX IF NOT EXISTS idx_playlist_items_order
  ON playlist_items(playlist_id, sort_order, media_item_id);
"""


MEDIA_ITEMS_V3 = """
CREATE TABLE media_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK(type IN
    ('ppt_file','pdf_deck','ppt_link','image','url','video','announcement')),
  source TEXT NOT NULL,
  embed_url TEXT,
  render_mode TEXT NOT NULL DEFAULT 'auto'
    CHECK(render_mode IN ('auto','live','converted','archive','screenshot','scroll')),
  duration_sec INTEGER NOT NULL DEFAULT 20 CHECK(duration_sec >= 0),
  slide_sec INTEGER NOT NULL DEFAULT 10 CHECK(slide_sec > 0),
  volume INTEGER NOT NULL DEFAULT 80 CHECK(volume BETWEEN 0 AND 100),
  fit_mode TEXT NOT NULL DEFAULT 'contain'
    CHECK(fit_mode IN ('contain','cover','stretch','width','height','native')),
  web_zoom INTEGER NOT NULL DEFAULT 100 CHECK(web_zoom BETWEEN 50 AND 200),
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
  background_color TEXT NOT NULL DEFAULT '#12372a',
  text_color TEXT NOT NULL DEFAULT '#ffffff',
  text_size INTEGER NOT NULL DEFAULT 64,
  text_align TEXT NOT NULL DEFAULT 'center',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)
"""


def _upgrade_media_items_v3(conn: sqlite3.Connection) -> None:
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'media_items'"
    ).fetchone()[0]
    if (
        "announcement" in table_sql
        and "'native'" in table_sql
        and "'scroll'" in table_sql
    ):
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE media_items RENAME TO media_items_v1")
        conn.execute(MEDIA_ITEMS_V3)
        old_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(media_items_v1)")
        }
        new_columns = [
            row["name"] for row in conn.execute("PRAGMA table_info(media_items)")
        ]
        copied_columns = [name for name in new_columns if name in old_columns]
        column_list = ", ".join(copied_columns)
        conn.execute(
            f"INSERT INTO media_items({column_list}) "
            f"SELECT {column_list} FROM media_items_v1"  # nosec B608
        )
        conn.execute("DROP TABLE media_items_v1")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_order ON media_items(enabled, sort_order, id)"
    )


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
        _upgrade_media_items_v3(conn)
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
        now = utcnow()
        conn.execute(
            """INSERT OR IGNORE INTO playlists(
                 name, is_default, enabled, days_mask, sort_order, created_at, updated_at)
               VALUES ('Default', 1, 1, 127, 0, ?, ?)""",
            (now, now),
        )
        default_id = conn.execute(
            "SELECT id FROM playlists WHERE is_default = 1"
        ).fetchone()["id"]
        conn.execute(
            """INSERT OR IGNORE INTO playlist_items(playlist_id, media_item_id, sort_order)
               SELECT ?, m.id, m.sort_order FROM media_items m
               WHERE m.deleted_at IS NULL AND NOT EXISTS (
                 SELECT 1 FROM playlist_items pi WHERE pi.media_item_id = m.id
               )""",
            (default_id,),
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

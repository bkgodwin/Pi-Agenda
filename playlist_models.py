from __future__ import annotations

import sqlite3
from typing import Any

from .db import bump_playlist_version, transaction, utcnow
from .schedules import item_is_active, validate_window


def validate_playlist(
    values: dict[str, Any], *, partial: bool = False, is_default: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not partial or "name" in values:
        name = str(values.get("name", "")).strip()
        if not name or len(name) > 100:
            raise ValueError("Playlist name must be between 1 and 100 characters")
        result["name"] = name
    if "enabled" in values:
        try:
            enabled = int(values["enabled"])
        except (TypeError, ValueError) as exc:
            raise ValueError("enabled must be 0 or 1") from exc
        if enabled not in {0, 1}:
            raise ValueError("enabled must be 0 or 1")
        result["enabled"] = enabled
    if is_default:
        result.update(enabled=1, days_mask=127, start_time=None, end_time=None)
        return result
    if "days_mask" in values:
        try:
            mask = int(values["days_mask"])
        except (TypeError, ValueError) as exc:
            raise ValueError("days_mask must be a number") from exc
        if not 1 <= mask <= 127:
            raise ValueError("At least one playlist day must be selected")
        result["days_mask"] = mask
    if "start_time" in values or "end_time" in values:
        start = str(values.get("start_time") or "").strip() or None
        end = str(values.get("end_time") or "").strip() or None
        validate_window(start, end)
        result["start_time"] = start
        result["end_time"] = end
    return result


def create_playlist(conn: sqlite3.Connection, values: dict[str, Any]) -> int:
    clean = validate_playlist(values)
    now = utcnow()
    with transaction(conn):
        cursor = conn.execute(
            """INSERT INTO playlists(
                 name, is_default, enabled, days_mask, start_time, end_time,
                 sort_order, created_at, updated_at)
               VALUES (?, 0, ?, ?, ?, ?,
                 COALESCE((SELECT MAX(sort_order) + 1 FROM playlists), 0), ?, ?)""",
            (
                clean["name"],
                clean.get("enabled", 1),
                clean.get("days_mask", 127),
                clean.get("start_time"),
                clean.get("end_time"),
                now,
                now,
            ),
        )
        bump_playlist_version(conn)
    return int(cursor.lastrowid)


def update_playlist(
    conn: sqlite3.Connection, playlist_id: int, values: dict[str, Any]
) -> None:
    current = conn.execute(
        "SELECT * FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if not current:
        raise LookupError("Playlist not found")
    merged = dict(current)
    merged.update(values)
    if "start_time" in values or "end_time" in values:
        values = values | {
            "start_time": merged.get("start_time"),
            "end_time": merged.get("end_time"),
        }
    clean = validate_playlist(
        values, partial=True, is_default=bool(current["is_default"])
    )
    if not clean:
        return
    assignments = ", ".join(f"{key} = ?" for key in clean)
    with transaction(conn):
        conn.execute(
            f"UPDATE playlists SET {assignments}, updated_at = ? WHERE id = ?",  # nosec B608
            (*clean.values(), utcnow(), playlist_id),
        )
        bump_playlist_version(conn)


def delete_playlist(conn: sqlite3.Connection, playlist_id: int) -> None:
    row = conn.execute(
        "SELECT is_default FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if not row:
        raise LookupError("Playlist not found")
    if row["is_default"]:
        raise ValueError("The default playlist cannot be deleted")
    with transaction(conn):
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        default = conn.execute(
            "SELECT id FROM playlists WHERE is_default = 1"
        ).fetchone()
        if default:
            next_order = conn.execute(
                """SELECT COALESCE(MAX(sort_order) + 1, 0) value
                   FROM playlist_items WHERE playlist_id = ?""",
                (default["id"],),
            ).fetchone()["value"]
            orphan_ids = [
                item["id"]
                for item in conn.execute(
                    """SELECT m.id FROM media_items m
                       WHERE m.deleted_at IS NULL AND NOT EXISTS (
                         SELECT 1 FROM playlist_items pi WHERE pi.media_item_id = m.id
                       ) ORDER BY m.id"""
                )
            ]
            for offset, item_id in enumerate(orphan_ids):
                conn.execute(
                    """INSERT INTO playlist_items(playlist_id, media_item_id, sort_order)
                       VALUES (?, ?, ?)""",
                    (default["id"], item_id, next_order + offset),
                )
        bump_playlist_version(conn)


def set_default_playlist(conn: sqlite3.Connection, playlist_id: int) -> None:
    if not conn.execute(
        "SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone():
        raise LookupError("Playlist not found")
    with transaction(conn):
        # The former default has no meaningful schedule because defaults are
        # deliberately unscheduled. Keep it from becoming an always-active
        # scheduled playlist until an administrator configures and enables it.
        conn.execute(
            "UPDATE playlists SET is_default = 0, enabled = 0 WHERE is_default = 1"
        )
        conn.execute(
            """UPDATE playlists SET is_default = 1, enabled = 1, days_mask = 127,
                 start_time = NULL, end_time = NULL, updated_at = ? WHERE id = ?""",
            (utcnow(), playlist_id),
        )
        bump_playlist_version(conn)


def validate_playlist_ids(
    conn: sqlite3.Connection, playlist_ids: list[int]
) -> list[int]:
    normalized = list(dict.fromkeys(int(value) for value in playlist_ids))
    if not normalized:
        raise ValueError("Select at least one playlist")
    placeholders = ",".join("?" for _ in normalized)
    count = conn.execute(
        f"SELECT COUNT(*) count FROM playlists WHERE id IN ({placeholders})",  # nosec B608
        normalized,
    ).fetchone()["count"]
    if count != len(normalized):
        raise ValueError("One or more selected playlists do not exist")
    return normalized


def assign_item_to_playlists(
    conn: sqlite3.Connection, item_id: int, playlist_ids: list[int]
) -> None:
    normalized = validate_playlist_ids(conn, playlist_ids)
    existing = {
        row["playlist_id"]
        for row in conn.execute(
            "SELECT playlist_id FROM playlist_items WHERE media_item_id = ?",
            (item_id,),
        )
    }
    selected = set(normalized)
    with transaction(conn):
        for playlist_id in existing - selected:
            conn.execute(
                "DELETE FROM playlist_items WHERE playlist_id = ? AND media_item_id = ?",
                (playlist_id, item_id),
            )
        for playlist_id in (value for value in normalized if value not in existing):
            next_order = conn.execute(
                """SELECT COALESCE(MAX(sort_order) + 1, 0) next_order
                   FROM playlist_items WHERE playlist_id = ?""",
                (playlist_id,),
            ).fetchone()["next_order"]
            conn.execute(
                """INSERT INTO playlist_items(playlist_id, media_item_id, sort_order)
                   VALUES (?, ?, ?)""",
                (playlist_id, item_id, next_order),
            )
        bump_playlist_version(conn)


def playlist_ids_for_item(conn: sqlite3.Connection, item_id: int) -> list[int]:
    return [
        row["playlist_id"]
        for row in conn.execute(
            "SELECT playlist_id FROM playlist_items WHERE media_item_id = ?",
            (item_id,),
        )
    ]


def active_nondefault_playlists(conn: sqlite3.Connection, local_now) -> list[dict]:
    active = []
    for row in conn.execute(
        "SELECT * FROM playlists WHERE is_default = 0 ORDER BY sort_order, id"
    ):
        playlist = dict(row)
        if item_is_active(playlist, local_now):
            active.append(playlist)
    return active

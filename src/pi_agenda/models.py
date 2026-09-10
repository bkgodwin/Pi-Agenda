from __future__ import annotations

import re
import sqlite3
from typing import Any

from .db import bump_playlist_version, transaction, utcnow
from .schedules import validate_window

ITEM_TYPES = {
    "ppt_file",
    "pdf_deck",
    "ppt_link",
    "image",
    "url",
    "video",
    "announcement",
}
RENDER_MODES = {"auto", "live", "converted", "archive", "screenshot"}
FIT_MODES = {"contain", "cover", "stretch", "width", "height", "native"}


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return result


def validate_item(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    required = ("name", "type", "source")
    if not partial:
        for field in required:
            if not str(payload.get(field, "")).strip():
                raise ValueError(f"{field} is required")

    if "name" in payload:
        name = str(payload["name"]).strip()
        if not name or len(name) > 160:
            raise ValueError("Name must be between 1 and 160 characters")
        result["name"] = name
    if "type" in payload:
        item_type = str(payload["type"])
        if item_type not in ITEM_TYPES:
            raise ValueError("Unsupported content type")
        result["type"] = item_type
    if "source" in payload:
        source = str(payload["source"]).strip()
        if not source or len(source) > 4096:
            raise ValueError("Source is required and must be under 4096 characters")
        result["source"] = source
    for field in ("background_color", "text_color"):
        if field in payload:
            color = str(payload[field]).strip().lower()
            if not re.fullmatch(r"#[0-9a-f]{6}", color):
                raise ValueError(f"{field} must be a six-digit hex color")
            result[field] = color
    if "text_align" in payload:
        alignment = str(payload["text_align"])
        if alignment not in {"left", "center", "right"}:
            raise ValueError("Unsupported text alignment")
        result["text_align"] = alignment
    if "embed_url" in payload:
        result["embed_url"] = str(payload.get("embed_url") or "").strip() or None
    if "render_mode" in payload:
        mode = str(payload["render_mode"])
        if mode not in RENDER_MODES:
            raise ValueError("Unsupported render mode")
        result["render_mode"] = mode
    if "fit_mode" in payload:
        fit = str(payload["fit_mode"])
        if fit not in FIT_MODES:
            raise ValueError("Unsupported fit mode")
        result["fit_mode"] = fit

    numeric = {
        "duration_sec": (0, 86400),
        "slide_sec": (1, 3600),
        "volume": (0, 100),
        "days_mask": (1, 127),
        "sort_order": (0, 1_000_000),
        "enabled": (0, 1),
        "text_size": (24, 160),
        "web_zoom": (50, 200),
    }
    for field, bounds in numeric.items():
        if field in payload:
            result[field] = _integer(payload[field], field, *bounds)

    if "start_time" in payload or "end_time" in payload:
        start = str(payload.get("start_time") or "").strip() or None
        end = str(payload.get("end_time") or "").strip() or None
        validate_window(start, end)
        result["start_time"] = start
        result["end_time"] = end
    return result


def create_item(conn: sqlite3.Connection, values: dict[str, Any]) -> int:
    values = validate_item(values)
    now = utcnow()
    defaults = {
        "embed_url": None,
        "render_mode": "auto",
        "duration_sec": 20,
        "slide_sec": 10,
        "volume": 80,
        "fit_mode": "contain",
        "web_zoom": 100,
        "days_mask": 127,
        "start_time": None,
        "end_time": None,
        "enabled": 1,
        "sort_order": 0,
        "background_color": "#12372a",
        "text_color": "#ffffff",
        "text_size": 64,
        "text_align": "center",
    }
    defaults.update(values)
    with transaction(conn):
        cursor = conn.execute(
            """INSERT INTO media_items(
                 name, type, source, embed_url, render_mode, duration_sec, slide_sec,
                 volume, fit_mode, web_zoom, days_mask, start_time, end_time, enabled, sort_order,
                 background_color, text_color, text_size, text_align, created_at, updated_at)
               VALUES (:name, :type, :source, :embed_url, :render_mode, :duration_sec,
                 :slide_sec, :volume, :fit_mode, :web_zoom, :days_mask, :start_time, :end_time,
                 :enabled, :sort_order, :background_color, :text_color, :text_size,
                 :text_align, :created_at, :updated_at)""",
            defaults | {"created_at": now, "updated_at": now},
        )
        item_id = int(cursor.lastrowid)
        bump_playlist_version(conn)
    return item_id


def update_item(
    conn: sqlite3.Connection, item_id: int, payload: dict[str, Any]
) -> None:
    current = conn.execute(
        "SELECT * FROM media_items WHERE id = ?", (item_id,)
    ).fetchone()
    if not current:
        raise LookupError("Item not found")
    merged_for_window = dict(current)
    merged_for_window.update(payload)
    if "start_time" in payload or "end_time" in payload:
        payload = payload | {
            "start_time": merged_for_window.get("start_time"),
            "end_time": merged_for_window.get("end_time"),
        }
    values = validate_item(payload, partial=True)
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    with transaction(conn):
        # assignments contains only fixed-schema keys returned by validate_item().
        conn.execute(
            f"UPDATE media_items SET {assignments}, updated_at = ? WHERE id = ?",  # nosec B608
            (*values.values(), utcnow(), item_id),
        )
        bump_playlist_version(conn)


def delete_item(conn: sqlite3.Connection, item_id: int) -> list[str]:
    paths = [
        row["relative_path"]
        for row in conn.execute(
            "SELECT relative_path FROM media_generations WHERE media_item_id = ?",
            (item_id,),
        )
    ]
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE media_items SET enabled = 0, deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (utcnow(), utcnow(), item_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("Item not found")
        conn.execute(
            """UPDATE jobs SET state = 'cancelled', finished_at = ?
               WHERE media_item_id = ? AND state = 'queued'""",
            (utcnow(), item_id),
        )
        bump_playlist_version(conn)
    return paths


def serialize_item(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)

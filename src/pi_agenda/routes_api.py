from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import bcrypt
import psutil
from flask import Blueprint, current_app, jsonify, request, send_file, session

from .backup import BackupError, validate_backup
from .db import (
    backup_database,
    bump_playlist_version,
    get_db,
    get_setting,
    set_setting,
    transaction,
    utcnow,
)
from .jobs import enqueue_job
from .models import create_item, delete_item, serialize_item, update_item
from .pipelines.m365 import normalize_m365_input
from .playlist import build_playlist
from .playlist_models import (
    assign_item_to_playlists,
    create_playlist,
    delete_playlist,
    playlist_ids_for_item,
    set_default_playlist,
    update_playlist,
    validate_playlist_ids,
)
from .schedules import parse_hhmm, validate_window
from .security import api_login_required, validate_csrf, validate_remote_url

bp = Blueprint("api", __name__, url_prefix="/api")


def ok(data=None, status=200):
    return jsonify(ok=True, data=data, error=None), status


def error(code: str, message: str, status: int = 400):
    return jsonify(
        ok=False, data=None, error={"code": code, "message": message}
    ), status


def payload() -> dict:
    candidate = request.get_json(silent=True)
    return candidate if isinstance(candidate, dict) else request.form.to_dict()


def _playlist_selection(values: dict) -> tuple[list[int] | None, dict]:
    clean = dict(values)
    marker = clean.pop("playlist_selection", None)
    raw = clean.pop("playlist_ids", None)
    if request.form:
        raw_values = request.form.getlist("playlist_ids")
        if raw_values or marker is not None:
            raw = raw_values
    if raw is None and marker is None:
        return None, clean
    if not isinstance(raw, list):
        raw = [raw] if raw not in {None, ""} else []
    try:
        return [int(value) for value in raw], clean
    except (TypeError, ValueError) as exc:
        raise ValueError("Playlist selection is invalid") from exc


def _default_playlist_ids(conn) -> list[int]:
    row = conn.execute("SELECT id FROM playlists WHERE is_default = 1").fetchone()
    return [int(row["id"])] if row else []


@bp.get("/ready")
def ready():
    try:
        get_db().execute("SELECT 1").fetchone()
        return ok({"ready": True})
    except sqlite3.Error:
        return error("not_ready", "Database is not ready", 503)


@bp.get("/items")
@api_login_required
def list_items():
    rows = (
        get_db()
        .execute(
            "SELECT * FROM media_items WHERE deleted_at IS NULL ORDER BY sort_order, id"
        )
        .fetchall()
    )
    return ok([serialize_item(row) for row in rows])


def _create_upload() -> tuple[dict, object]:
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        raise ValueError("A file is required")
    extension = Path(uploaded.filename).suffix.lower()
    type_by_extension = {
        ".pptx": "ppt_file",
        ".pdf": "pdf_deck",
        ".jpg": "image",
        ".jpeg": "image",
        ".png": "image",
        ".webp": "image",
        ".gif": "image",
        ".mp4": "video",
        ".mov": "video",
        ".mkv": "video",
        ".webm": "video",
    }
    item_type = request.form.get("type") or type_by_extension.get(extension)
    if item_type != type_by_extension.get(extension):
        raise ValueError("File extension does not match the selected content type")
    values = request.form.to_dict()
    values["type"] = item_type
    values["source"] = "pending"
    return values, uploaded


def _check_storage(required_bytes: int = 0) -> None:
    config = current_app.config["RUNTIME_CONFIG"]
    conn = get_db()
    usage = shutil.disk_usage(config.data_dir)
    reserve = int(get_setting(conn, "minimum_free_bytes", str(1024 * 1024 * 1024)))
    if usage.free - required_bytes < reserve:
        raise ValueError(
            "Not enough free disk space to preserve the configured reserve"
        )


@bp.post("/items")
@api_login_required
def add_item():
    validate_csrf()
    conn = get_db()
    try:
        if request.files:
            values, uploaded = _create_upload()
            playlist_ids, values = _playlist_selection(values)
            assignment_ids = validate_playlist_ids(
                conn,
                _default_playlist_ids(conn) if playlist_ids is None else playlist_ids,
            )
            _check_storage(request.content_length or 0)
            item_id = create_item(conn, values)
            extension = Path(uploaded.filename).suffix.lower()
            relative = Path("uploads") / str(item_id) / f"original{extension}"
            target = current_app.config["RUNTIME_CONFIG"].data_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                uploaded.save(target)
                if target.stat().st_size == 0:
                    raise ValueError("Uploaded file is empty")
                update_item(conn, item_id, {"source": relative.as_posix()})
            except Exception:
                conn.execute("DELETE FROM media_items WHERE id = ?", (item_id,))
                shutil.rmtree(target.parent, ignore_errors=True)
                raise
            job_id = enqueue_job(conn, "convert", item_id)
        else:
            values = payload()
            playlist_ids, values = _playlist_selection(values)
            assignment_ids = validate_playlist_ids(
                conn,
                _default_playlist_ids(conn) if playlist_ids is None else playlist_ids,
            )
            item_type = values.get("type")
            if item_type not in {"url", "ppt_link", "announcement"}:
                raise ValueError("Uploads must use multipart form data")
            if item_type == "announcement":
                values["source"] = str(
                    values.pop("announcement_text", values.get("source", ""))
                ).strip()
                item_id = create_item(conn, values)
                now = utcnow()
                conn.execute(
                    """UPDATE media_items SET last_status = 'ok', last_checked = ?,
                         last_good_at = ? WHERE id = ?""",
                    (now, now, item_id),
                )
                job_id = None
            elif item_type == "url":
                allowlist = {
                    host.strip().lower()
                    for host in get_setting(conn, "intranet_allowlist", "").split(",")
                    if host.strip()
                }
                values["source"] = validate_remote_url(
                    str(values.get("source", "")), allowlist
                )
            else:
                normalized = normalize_m365_input(str(values.get("source", "")))
                values["source"] = normalized
                values["embed_url"] = normalized
            if item_type != "announcement":
                item_id = create_item(conn, values)
                job_id = enqueue_job(conn, "refresh", item_id)
        assign_item_to_playlists(conn, item_id, assignment_ids)
        return ok({"id": item_id, "job_id": job_id}, 201)
    except ValueError as exc:
        return error("invalid_item", str(exc))


@bp.get("/items/<int:item_id>")
@api_login_required
def get_item(item_id: int):
    row = (
        get_db()
        .execute(
            "SELECT * FROM media_items WHERE id = ? AND deleted_at IS NULL", (item_id,)
        )
        .fetchone()
    )
    if not row:
        return error("not_found", "Item not found", 404)
    result = serialize_item(row)
    result["playlist_ids"] = playlist_ids_for_item(get_db(), item_id)
    return ok(result)


@bp.patch("/items/<int:item_id>")
@api_login_required
def patch_item(item_id: int):
    validate_csrf()
    try:
        conn = get_db()
        playlist_ids, values = _playlist_selection(payload())
        current = conn.execute(
            "SELECT * FROM media_items WHERE id = ? AND deleted_at IS NULL", (item_id,)
        ).fetchone()
        if not current:
            raise LookupError("Item not found")
        if playlist_ids is not None:
            playlist_ids = validate_playlist_ids(conn, playlist_ids)
        if current["type"] == "announcement":
            values["source"] = values.pop("announcement_text", values.get("source", ""))
        else:
            values.pop("announcement_text", None)
        if current["type"] in {"ppt_file", "pdf_deck", "image", "video"}:
            # The source textarea exists but is hidden for uploaded media. Source
            # replacement is handled only by the dedicated upload endpoint.
            values.pop("source", None)
        elif "source" in values and current["type"] == "url":
            allowlist = {
                host.strip().lower()
                for host in get_setting(conn, "intranet_allowlist", "").split(",")
                if host.strip()
            }
            values["source"] = validate_remote_url(str(values["source"]), allowlist)
        elif "source" in values and current["type"] == "ppt_link":
            values["source"] = normalize_m365_input(str(values["source"]))
            values["embed_url"] = values["source"]
        update_item(conn, item_id, values)
        if playlist_ids is not None:
            assign_item_to_playlists(conn, item_id, playlist_ids)
        job_id = None
        if current["type"] in {"url", "ppt_link"} and (
            "source" in values or "render_mode" in values
        ):
            job_id = enqueue_job(conn, "refresh", item_id)
            conn.execute(
                "UPDATE jobs SET not_before = ? WHERE id = ? AND state = 'queued'",
                (utcnow(), job_id),
            )
        if current["type"] == "announcement":
            now = utcnow()
            conn.execute(
                """UPDATE media_items SET last_status = 'ok', last_checked = ?,
                     last_good_at = ?, last_error = NULL WHERE id = ?""",
                (now, now, item_id),
            )
        return ok({"id": item_id, "job_id": job_id})
    except ValueError as exc:
        return error("invalid_item", str(exc))
    except LookupError as exc:
        return error("not_found", str(exc), 404)


@bp.delete("/items/<int:item_id>")
@api_login_required
def remove_item(item_id: int):
    validate_csrf()
    conn = get_db()
    try:
        delete_item(conn, item_id)
        return ok({"id": item_id})
    except LookupError as exc:
        return error("not_found", str(exc), 404)


@bp.post("/items/<int:item_id>/refresh")
@api_login_required
def refresh_item(item_id: int):
    validate_csrf()
    conn = get_db()
    item = conn.execute(
        "SELECT type FROM media_items WHERE id = ? AND deleted_at IS NULL", (item_id,)
    ).fetchone()
    if not item:
        return error("not_found", "Item not found", 404)
    if item["type"] == "announcement":
        return error("not_refreshable", "Announcements do not require refresh", 400)
    job_id = enqueue_job(conn, "refresh", item_id)
    conn.execute(
        "UPDATE jobs SET not_before = ?, stage = 'queued manually' WHERE id = ? AND state = 'queued'",
        (utcnow(), job_id),
    )
    return ok({"job_id": job_id}, 202)


@bp.post("/items/<int:item_id>/replace")
@api_login_required
def replace_item(item_id: int):
    validate_csrf()
    conn = get_db()
    item = conn.execute(
        "SELECT * FROM media_items WHERE id = ? AND deleted_at IS NULL", (item_id,)
    ).fetchone()
    if not item:
        return error("not_found", "Item not found", 404)
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return error("invalid_upload", "A replacement file is required")
    allowed = {
        "ppt_file": {".pptx"},
        "pdf_deck": {".pdf"},
        "image": {".jpg", ".jpeg", ".png", ".webp", ".gif"},
        "video": {".mp4", ".mov", ".mkv", ".webm"},
    }
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in allowed.get(item["type"], set()):
        return error("invalid_upload", "Replacement file type does not match this item")
    try:
        _check_storage(request.content_length or 0)
        relative = (
            Path("uploads") / str(item_id) / f"original-{uuid.uuid4().hex}{suffix}"
        )
        target = current_app.config["RUNTIME_CONFIG"].data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        uploaded.save(target)
        if target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise ValueError("Uploaded file is empty")
        update_item(conn, item_id, {"source": relative.as_posix()})
        return ok({"job_id": enqueue_job(conn, "convert", item_id)}, 202)
    except ValueError as exc:
        return error("invalid_upload", str(exc))


@bp.post("/items/<int:item_id>/duplicate")
@api_login_required
def duplicate_item(item_id: int):
    validate_csrf()
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM media_items WHERE id = ? AND deleted_at IS NULL", (item_id,)
    ).fetchone()
    if not row:
        return error("not_found", "Item not found", 404)
    values = dict(row)
    values["name"] = f"{values['name']} (copy)"
    values["source"] = row["source"]
    new_id = create_item(conn, values)
    if row["type"] in {"ppt_file", "pdf_deck", "image", "video"}:
        source = current_app.config["RUNTIME_CONFIG"].data_dir / row["source"]
        suffix = source.suffix
        target_rel = Path("uploads") / str(new_id) / f"original{suffix}"
        target = current_app.config["RUNTIME_CONFIG"].data_dir / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        update_item(conn, new_id, {"source": target_rel.as_posix()})
    assign_item_to_playlists(conn, new_id, playlist_ids_for_item(conn, item_id))
    if row["type"] == "announcement":
        now = utcnow()
        conn.execute(
            """UPDATE media_items SET last_status = 'ok', last_checked = ?,
                 last_good_at = ? WHERE id = ?""",
            (now, now, new_id),
        )
        job_id = None
    else:
        job_id = enqueue_job(
            conn,
            "convert" if row["type"] not in {"url", "ppt_link"} else "refresh",
            new_id,
        )
    return ok({"id": new_id, "job_id": job_id}, 201)


@bp.post("/items/reorder")
@api_login_required
def reorder_items():
    validate_csrf()
    order = payload().get("order")
    if not isinstance(order, list) or not all(
        isinstance(value, int) for value in order
    ):
        return error("invalid_order", "order must be a list of item IDs")
    conn = get_db()
    existing = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM media_items WHERE deleted_at IS NULL ORDER BY id"
        )
    ]
    if sorted(order) != existing:
        return error("invalid_order", "order must contain every item ID exactly once")
    with transaction(conn):
        for position, item_id in enumerate(order):
            conn.execute(
                "UPDATE media_items SET sort_order = ?, updated_at = ? WHERE id = ?",
                (position, utcnow(), item_id),
            )
        bump_playlist_version(conn)
    return ok({"order": order})


@bp.get("/playlists")
@api_login_required
def list_playlists():
    rows = get_db().execute(
        """SELECT p.*, COUNT(m.id) item_count
           FROM playlists p LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
           LEFT JOIN media_items m ON m.id = pi.media_item_id AND m.deleted_at IS NULL
           GROUP BY p.id ORDER BY p.is_default DESC, p.sort_order, p.id"""
    )
    return ok([dict(row) for row in rows])


@bp.post("/playlists")
@api_login_required
def add_playlist():
    validate_csrf()
    try:
        return ok({"id": create_playlist(get_db(), payload())}, 201)
    except (ValueError, sqlite3.IntegrityError) as exc:
        return error("invalid_playlist", str(exc))


@bp.patch("/playlists/<int:playlist_id>")
@api_login_required
def patch_playlist(playlist_id: int):
    validate_csrf()
    try:
        update_playlist(get_db(), playlist_id, payload())
        return ok({"id": playlist_id})
    except (ValueError, sqlite3.IntegrityError) as exc:
        return error("invalid_playlist", str(exc))
    except LookupError as exc:
        return error("not_found", str(exc), 404)


@bp.delete("/playlists/<int:playlist_id>")
@api_login_required
def remove_playlist(playlist_id: int):
    validate_csrf()
    try:
        delete_playlist(get_db(), playlist_id)
        return ok({"id": playlist_id})
    except ValueError as exc:
        return error("invalid_playlist", str(exc))
    except LookupError as exc:
        return error("not_found", str(exc), 404)


@bp.post("/playlists/<int:playlist_id>/default")
@api_login_required
def make_default_playlist(playlist_id: int):
    validate_csrf()
    try:
        set_default_playlist(get_db(), playlist_id)
        return ok({"id": playlist_id})
    except LookupError as exc:
        return error("not_found", str(exc), 404)


@bp.post("/playlists/<int:playlist_id>/reorder")
@api_login_required
def reorder_playlist_items(playlist_id: int):
    validate_csrf()
    order = payload().get("order")
    if not isinstance(order, list) or not all(
        isinstance(value, int) for value in order
    ):
        return error("invalid_order", "order must be a list of item IDs")
    conn = get_db()
    existing = [
        row["media_item_id"]
        for row in conn.execute(
            """SELECT pi.media_item_id FROM playlist_items pi
               JOIN media_items m ON m.id = pi.media_item_id
               WHERE pi.playlist_id = ? AND m.deleted_at IS NULL""",
            (playlist_id,),
        )
    ]
    if sorted(order) != sorted(existing) or len(order) != len(set(order)):
        return error("invalid_order", "order must contain every playlist item once")
    with transaction(conn):
        for position, item_id in enumerate(order):
            conn.execute(
                """UPDATE playlist_items SET sort_order = ?
                   WHERE playlist_id = ? AND media_item_id = ?""",
                (position, playlist_id, item_id),
            )
        bump_playlist_version(conn)
    return ok({"order": order})


@bp.get("/jobs")
@api_login_required
def list_jobs():
    rows = get_db().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 100").fetchall()
    return ok([dict(row) for row in rows])


@bp.get("/jobs/<int:job_id>")
@api_login_required
def get_job(job_id: int):
    row = get_db().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return ok(dict(row)) if row else error("not_found", "Job not found", 404)


@bp.post("/jobs/<int:job_id>/cancel")
@api_login_required
def cancel_job(job_id: int):
    validate_csrf()
    cursor = get_db().execute(
        "UPDATE jobs SET state = 'cancelled', finished_at = ? WHERE id = ? AND state = 'queued'",
        (utcnow(), job_id),
    )
    return ok({"cancelled": cursor.rowcount == 1})


@bp.get("/schedule")
@api_login_required
def get_schedule():
    rows = (
        get_db().execute("SELECT * FROM display_schedule ORDER BY weekday").fetchall()
    )
    return ok([dict(row) for row in rows])


@bp.put("/schedule")
@api_login_required
def put_schedule():
    validate_csrf()
    rules = payload().get("days")
    if not isinstance(rules, list) or len(rules) != 7:
        return error("invalid_schedule", "Seven weekday rules are required")
    try:
        normalized = []
        for weekday, rule in enumerate(rules):
            mode = rule.get("mode")
            if mode not in {"off", "always_on", "window"}:
                raise ValueError("Invalid display mode")
            on_time = rule.get("on_time") or None
            off_time = rule.get("off_time") or None
            if mode == "window":
                validate_window(on_time, off_time)
            else:
                on_time = off_time = None
            normalized.append((mode, on_time, off_time, weekday))
    except (AttributeError, ValueError) as exc:
        return error("invalid_schedule", str(exc))
    conn = get_db()
    with transaction(conn):
        conn.executemany(
            "UPDATE display_schedule SET mode = ?, on_time = ?, off_time = ? WHERE weekday = ?",
            normalized,
        )
    return ok({"days": rules})


@bp.post("/display/override")
@api_login_required
def set_override():
    validate_csrf()
    values = payload()
    state = values.get("state")
    if state not in {"on", "off"}:
        return error("invalid_override", "state must be on or off")
    try:
        minutes = max(1, min(1440, int(values.get("minutes", 240))))
    except (TypeError, ValueError):
        return error("invalid_override", "minutes must be a number")
    expires = datetime.now(UTC) + timedelta(minutes=minutes)
    get_db().execute(
        """INSERT INTO manual_display_override(singleton, state, expires_at, created_at)
           VALUES (1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET
           state = excluded.state, expires_at = excluded.expires_at, created_at = excluded.created_at""",
        (state, expires.isoformat(timespec="seconds"), utcnow()),
    )
    return ok({"state": state, "expires_at": expires.isoformat(timespec="seconds")})


@bp.delete("/display/override")
@api_login_required
def clear_override():
    validate_csrf()
    get_db().execute("DELETE FROM manual_display_override WHERE singleton = 1")
    return ok({"cleared": True})


@bp.get("/display/status")
@api_login_required
def display_status():
    conn = get_db()
    playlist = build_playlist(
        conn, cache_port=current_app.config["RUNTIME_CONFIG"].cache_port
    )
    current_text = get_setting(conn, "current_item_id", "")
    current_id = int(current_text) if current_text.isdigit() else None
    current_index = next(
        (
            index
            for index, item in enumerate(playlist["items"])
            if item["id"] == current_id
        ),
        None,
    )
    up_next = None
    if playlist["items"]:
        up_next = playlist["items"][
            0 if current_index is None else (current_index + 1) % len(playlist["items"])
        ]
    current = playlist["items"][current_index] if current_index is not None else None
    return ok(
        {
            "display_on": playlist["display_on"],
            "current": current,
            "up_next": up_next,
            "heartbeat": get_setting(conn, "player_heartbeat", ""),
            "power_state": get_setting(conn, "display_power_state", "unknown"),
            "display_off_reason": playlist["display_off_reason"],
            "active_playlists": playlist["active_playlists"],
            "holiday_until": get_setting(conn, "holiday_until", ""),
            "blank_test_enabled": get_setting(conn, "blank_test_enabled", "0") == "1",
        }
    )


@bp.post("/display/test-blank")
@api_login_required
def test_screen_blank():
    validate_csrf()
    state = payload().get("state")
    if state not in {"start", "stop"}:
        return error("invalid_blank_test", "state must be start or stop")
    conn = get_db()
    set_setting(conn, "blank_test_enabled", "1" if state == "start" else "0")
    bump_playlist_version(conn)
    return ok({"active": state == "start"})


@bp.post("/display/holiday")
@api_login_required
def start_holiday():
    validate_csrf()
    try:
        days = int(payload().get("days", 1))
    except (TypeError, ValueError):
        return error("invalid_holiday", "Holiday length must be a number")
    if not 1 <= days <= 365:
        return error("invalid_holiday", "Holiday length must be between 1 and 365 days")
    until = datetime.now(UTC) + timedelta(days=days)
    conn = get_db()
    set_setting(conn, "holiday_until", until.isoformat(timespec="seconds"))
    bump_playlist_version(conn)
    return ok({"until": until.isoformat(timespec="seconds")})


@bp.delete("/display/holiday")
@api_login_required
def cancel_holiday():
    validate_csrf()
    conn = get_db()
    set_setting(conn, "holiday_until", "")
    bump_playlist_version(conn)
    return ok({"cancelled": True})


SETTABLE_SETTINGS = {
    "resolution",
    "timezone",
    "check_time",
    "site_name",
    "default_duration_sec",
    "default_slide_sec",
    "default_volume",
    "content_quota_bytes",
    "minimum_free_bytes",
    "intranet_allowlist",
    "remote_player_enabled",
    "boot_splash_seconds",
}


@bp.get("/settings")
@api_login_required
def get_settings():
    rows = get_db().execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return ok(
        {row["key"]: row["value"] for row in rows if row["key"] != "password_hash"}
    )


@bp.put("/settings")
@api_login_required
def put_settings():
    validate_csrf()
    values = payload()
    unknown = set(values) - SETTABLE_SETTINGS
    if unknown:
        return error(
            "invalid_setting", f"Unknown settings: {', '.join(sorted(unknown))}"
        )
    if values.get("resolution") not in {None, "720p", "1080p"}:
        return error("invalid_setting", "Resolution must be 720p or 1080p")
    try:
        if "timezone" in values:
            ZoneInfo(str(values["timezone"]))
        if "check_time" in values and parse_hhmm(str(values["check_time"])) is None:
            raise ValueError("Daily refresh time is required")
        if (
            "site_name" in values
            and not 1 <= len(str(values["site_name"]).strip()) <= 100
        ):
            raise ValueError("Site name must be between 1 and 100 characters")
        for key, minimum, maximum in (
            ("default_duration_sec", 1, 86400),
            ("default_slide_sec", 1, 3600),
            ("default_volume", 0, 100),
            ("boot_splash_seconds", 5, 60),
            ("content_quota_bytes", 1024 * 1024, 10**13),
            ("minimum_free_bytes", 0, 10**13),
        ):
            if key in values and not minimum <= int(values[key]) <= maximum:
                raise ValueError(f"{key} is outside its allowed range")
        if "remote_player_enabled" in values and str(
            values["remote_player_enabled"]
        ) not in {"0", "1"}:
            raise ValueError("remote_player_enabled must be 0 or 1")
    except (ValueError, ZoneInfoNotFoundError) as exc:
        return error("invalid_setting", str(exc))
    conn = get_db()
    old_resolution = get_setting(conn, "resolution", "720p")
    with transaction(conn):
        for key, value in values.items():
            set_setting(conn, key, value)
        bump_playlist_version(conn)
        if values.get("resolution") and values["resolution"] != old_resolution:
            rows = conn.execute(
                """SELECT id FROM media_items
                   WHERE deleted_at IS NULL AND type != 'announcement'"""
            ).fetchall()
            for row in rows:
                enqueue_job(conn, "rerender", row["id"])
    return ok(values)


@bp.get("/system/status")
@api_login_required
def system_status():
    config = current_app.config["RUNTIME_CONFIG"]
    conn = get_db()
    usage = shutil.disk_usage(config.data_dir)
    temperature = None
    thermal = Path("/sys/class/thermal/thermal_zone0/temp")
    if thermal.exists():
        try:
            temperature = int(thermal.read_text().strip()) / 1000
        except ValueError:
            pass
    return ok(
        {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory": psutil.virtual_memory()._asdict(),
            "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
            "temperature_c": temperature,
            "uptime_sec": int(datetime.now(UTC).timestamp() - psutil.boot_time()),
            "online": get_setting(conn, "internet_online", "0") == "1",
            "display_power_state": get_setting(conn, "display_power_state", "unknown"),
            "blank_test_enabled": get_setting(conn, "blank_test_enabled", "0") == "1",
            "holiday_until": get_setting(conn, "holiday_until", ""),
        }
    )


@bp.post("/system/password")
@api_login_required
def change_password():
    validate_csrf()
    values = payload()
    new_password = str(values.get("password", ""))
    if len(new_password) < 8 or new_password != values.get("confirm"):
        return error(
            "invalid_password",
            "Passwords must match and contain at least eight characters",
        )
    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    set_setting(get_db(), "password_hash", hashed)
    session.clear()
    return ok({"changed": True})


@bp.post("/system/refresh-all")
@api_login_required
def refresh_all():
    validate_csrf()
    conn = get_db()
    ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM media_items WHERE deleted_at IS NULL AND type != 'announcement'"
        )
    ]
    jobs = [enqueue_job(conn, "refresh", item_id) for item_id in ids]
    return ok({"jobs": jobs}, 202)


@bp.get("/system/backup")
@api_login_required
def backup():
    config = current_app.config["RUNTIME_CONFIG"]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    temp_dir = Path(tempfile.mkdtemp(prefix="pi-agenda-backup-"))
    try:
        snapshot = temp_dir / "db.sqlite"
        backup_database(config.db_path, snapshot)
        archive = temp_dir / f"pi-agenda-backup-{stamp}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(snapshot, "db.sqlite")
            manifest = {
                "created_at": utcnow(),
                "schema": 1,
                "includes_generated_cache": True,
            }
            bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
            uploads = config.data_dir / "uploads"
            if uploads.exists():
                for path in uploads.rglob("*"):
                    if path.is_file():
                        bundle.write(path, Path("uploads") / path.relative_to(uploads))
            snapshot_conn = sqlite3.connect(snapshot)
            try:
                active_paths = [
                    row[0]
                    for row in snapshot_conn.execute(
                        """SELECT g.relative_path FROM media_items m
                           JOIN media_generations g ON g.id = m.active_generation_id
                           WHERE m.deleted_at IS NULL"""
                    )
                ]
            finally:
                snapshot_conn.close()
            generation_root = config.data_dir / "generations"
            for relative in active_paths:
                generation_path = (config.data_dir / relative).resolve()
                if generation_root.resolve() not in generation_path.parents:
                    continue
                for path in generation_path.rglob("*"):
                    if path.is_file():
                        bundle.write(
                            path,
                            Path("generations") / path.relative_to(generation_root),
                        )
        response = send_file(archive, as_attachment=True, download_name=archive.name)
        response.call_on_close(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return response
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


@bp.post("/system/restore")
@api_login_required
def restore():
    validate_csrf()
    uploaded = request.files.get("backup")
    if not uploaded or not uploaded.filename:
        return error("invalid_backup", "A backup ZIP file is required")
    config = current_app.config["RUNTIME_CONFIG"]
    pending = config.data_dir / "backups" / "restore-pending.zip"
    temporary = config.data_dir / "staging" / "restore-upload.zip"
    try:
        uploaded.save(temporary)
        validate_backup(
            temporary, max_bytes=int(get_setting(get_db(), "content_quota_bytes"))
        )
        os.replace(temporary, pending)
    except (BackupError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        return error("invalid_backup", str(exc))
    helper = Path("/usr/local/libexec/pi-agenda-service-control")
    if not helper.is_file():
        pending.unlink(missing_ok=True)
        return error("unavailable", "Restore helper is not installed", 503)
    import subprocess

    result = subprocess.run(
        ["/usr/bin/sudo", str(helper), "restore"], timeout=10, check=False
    )
    if result.returncode != 0:
        pending.unlink(missing_ok=True)
        return error("restore_failed", "Restore could not be scheduled", 500)
    return ok({"scheduled": True}, 202)


@bp.get("/system/logs")
@api_login_required
def system_logs():
    import subprocess

    try:
        result = subprocess.run(
            [
                "/usr/bin/journalctl",
                "--no-pager",
                "-n",
                "200",
                "-u",
                "pi-agenda-web.service",
                "-u",
                "pi-agenda-worker.service",
                "-u",
                "pi-agenda-cache.service",
                "-u",
                "pi-agenda-kiosk.service",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        return ok({"text": result.stdout[-50000:]})
    except (OSError, subprocess.SubprocessError):
        return ok({"text": "System journal is unavailable in this environment."})


@bp.post("/system/services/restart")
@api_login_required
def restart_services():
    validate_csrf()
    helper = Path("/usr/local/libexec/pi-agenda-service-control")
    if not helper.is_file():
        return error("unavailable", "Service control helper is not installed", 503)
    import subprocess

    result = subprocess.run(
        ["/usr/bin/sudo", str(helper), "restart"], timeout=30, check=False
    )
    return (
        ok({"requested": result.returncode == 0})
        if result.returncode == 0
        else error("service_failed", "Service restart failed", 500)
    )


@bp.post("/system/reboot")
@api_login_required
def reboot():
    validate_csrf()
    helper = Path("/usr/local/libexec/pi-agenda-reboot")
    if not helper.is_file():
        return error("unavailable", "Reboot helper is not installed", 503)
    import subprocess

    result = subprocess.run(["/usr/bin/sudo", str(helper)], timeout=10, check=False)
    return (
        ok({"requested": True}, 202)
        if result.returncode == 0
        else error("reboot_failed", "Reboot request failed", 500)
    )


@bp.post("/system/update")
@api_login_required
def update_system():
    validate_csrf()
    helper = Path("/usr/local/libexec/pi-agenda-update")
    if not helper.is_file():
        return error(
            "unavailable",
            "Update helper is not installed. Run sudo ./start.sh --repair once over SSH.",
            503,
        )
    import subprocess

    try:
        check = subprocess.run(
            ["/usr/bin/sudo", str(helper), "check"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return error("update_check_failed", "Could not contact GitHub for updates", 503)
    if check.returncode != 0:
        return error(
            "update_check_failed",
            (check.stderr.strip() or "Could not contact GitHub for updates")[-500:],
            503,
        )
    versions = {}
    for line in check.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"current", "latest"}:
            versions[key] = value.strip()
    latest = versions.get("latest", "")
    current = versions.get("current", "unknown")
    if len(latest) != 40 or any(
        character not in "0123456789abcdef" for character in latest
    ):
        return error(
            "update_check_failed", "Update service returned an invalid version", 503
        )
    if current == latest:
        return ok({"up_to_date": True, "version": current})
    scheduled = subprocess.run(
        ["/usr/bin/sudo", str(helper), "update", latest],
        timeout=15,
        check=False,
    )
    if scheduled.returncode != 0:
        return error("update_failed", "The update could not be scheduled", 500)
    return ok(
        {"up_to_date": False, "scheduled": True, "from": current, "to": latest},
        202,
    )

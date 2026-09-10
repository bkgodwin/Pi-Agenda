from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import get_setting
from .playlist_models import active_nondefault_playlists
from .schedules import (
    display_off_reason,
    display_should_be_on,
    item_is_active,
    parse_hhmm,
)


def _widget_settings(conn) -> dict:
    return {
        "clock": {
            "enabled": get_setting(conn, "clock_widget_enabled", "0") == "1",
            "position": get_setting(conn, "clock_widget_position", "top-right"),
            "size": int(get_setting(conn, "clock_widget_size", "48")),
        },
        "progress": {
            "enabled": get_setting(conn, "progress_widget_enabled", "0") == "1",
            "position": get_setting(conn, "progress_widget_position", "bottom"),
            "height": int(get_setting(conn, "progress_widget_height", "8")),
            "color": get_setting(conn, "progress_widget_color", "#40c057"),
        },
        "ticker": {
            "enabled": get_setting(conn, "ticker_widget_enabled", "0") == "1",
            "position": get_setting(conn, "ticker_widget_position", "bottom"),
            "text": get_setting(conn, "ticker_widget_text", ""),
            "speed": int(get_setting(conn, "ticker_widget_speed", "20")),
        },
    }


def _progress_window(playlists: list[dict], local_now: datetime) -> dict | None:
    """Find the first active, timed non-default playlist window."""
    current_time = local_now.timetz().replace(tzinfo=None)
    for playlist in playlists:
        if (
            playlist["is_default"]
            or not playlist.get("start_time")
            or not playlist.get("end_time")
        ):
            continue
        start_time = parse_hhmm(playlist["start_time"])
        end_time = parse_hhmm(playlist["end_time"])
        if start_time is None or end_time is None:
            continue
        if start_time < end_time:
            if not (start_time <= current_time < end_time):
                continue
            start = datetime.combine(local_now.date(), start_time, local_now.tzinfo)
            end = datetime.combine(local_now.date(), end_time, local_now.tzinfo)
        elif current_time >= start_time:
            start = datetime.combine(local_now.date(), start_time, local_now.tzinfo)
            end = datetime.combine(
                local_now.date() + timedelta(days=1), end_time, local_now.tzinfo
            )
        elif current_time < end_time:
            start = datetime.combine(
                local_now.date() - timedelta(days=1), start_time, local_now.tzinfo
            )
            end = datetime.combine(local_now.date(), end_time, local_now.tzinfo)
        else:
            continue
        return {
            "playlist_id": playlist["id"],
            "playlist_name": playlist["name"],
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
        }
    return None


def _render_descriptor(
    item: dict, generation: dict | None, *, online: bool, cache_port: int
) -> dict:
    if item["type"] == "announcement":
        return {
            "render_kind": "announcement",
            "render_url": None,
            "slides": [],
            "message": item["source"],
            "background_color": item["background_color"],
            "text_color": item["text_color"],
            "text_size": item["text_size"],
            "text_align": item["text_align"],
        }

    item_type = item["type"]
    mode = item["render_mode"]
    use_live = online and item_type in {"url", "ppt_link"} and mode == "live"
    if use_live:
        return {
            "render_kind": "iframe",
            "render_url": item.get("embed_url") or item["source"],
            "slides": [],
            "web_zoom": item.get("web_zoom", 100),
        }
    if not generation:
        return {"render_kind": "unavailable", "render_url": None, "slides": []}

    key = generation["generation_key"]
    item_id = item["id"]
    kind = generation["kind"]
    base = f"/media/{item_id}/{key}"
    if kind == "slides":
        slides = [
            f"{base}/slide-{index:04d}.png"
            for index in range(1, generation["slide_count"] + 1)
        ]
        return {
            "render_kind": "deck",
            "render_url": slides[0] if slides else None,
            "slides": slides,
        }
    if kind == "image":
        return {"render_kind": "image", "render_url": f"{base}/content", "slides": []}
    if kind == "video":
        return {
            "render_kind": "video",
            "render_url": f"{base}/content.mp4",
            "slides": [],
        }
    if kind == "website_bundle":
        if mode == "auto":
            return {
                "render_kind": "image",
                "render_url": f"{base}/screenshot.png",
                "slides": [],
            }
        scrolling = mode == "scroll"
        scroll_duration = max(10, int(item.get("duration_sec") or 20))
        render_url = f"http://127.0.0.1:{cache_port}/{item_id}/{key}/index.html"
        if scrolling:
            render_url += f"?pi_agenda_scroll=1&duration={scroll_duration}"
        return {
            "render_kind": "iframe",
            "render_url": render_url,
            "remote_fallback_url": f"{base}/screenshot.png",
            "slides": [],
            "web_zoom": item.get("web_zoom", 100),
            "scrolling": scrolling,
        }
    if kind == "screenshot":
        return {
            "render_kind": "image",
            "render_url": f"{base}/screenshot.png",
            "slides": [],
        }
    return {"render_kind": "unavailable", "render_url": None, "slides": []}


def _paused_selection(conn) -> list[dict] | None:
    """Return pinned playlists while the schedule is paused, else None."""
    if get_setting(conn, "schedule_paused", "0") != "1":
        return None
    forced_text = get_setting(conn, "forced_playlist_id", "")
    if forced_text.isdigit():
        row = conn.execute(
            "SELECT * FROM playlists WHERE id = ?", (int(forced_text),)
        ).fetchone()
        if row:
            return [dict(row)]
    try:
        pinned = [
            int(value)
            for value in json.loads(get_setting(conn, "paused_playlist_ids", "[]"))
        ]
    except (ValueError, TypeError):
        pinned = []
    if pinned:
        placeholders = ",".join("?" for _ in pinned)
        rows = conn.execute(
            f"SELECT * FROM playlists WHERE id IN ({placeholders})",  # nosec B608
            pinned,
        ).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        ordered = [by_id[pid] for pid in pinned if pid in by_id]
        if ordered:
            return ordered
    return []


def _selected_playlists(conn, local_now: datetime) -> list[dict]:
    paused = _paused_selection(conn)
    if paused is not None:
        return paused
    active = active_nondefault_playlists(conn, local_now)
    if active:
        return active
    default = conn.execute(
        "SELECT * FROM playlists WHERE is_default = 1 AND enabled = 1"
    ).fetchone()
    return [dict(default)] if default else []


def build_playlist(conn, *, cache_port: int, now_utc: datetime | None = None) -> dict:
    now_utc = now_utc or datetime.now(UTC)
    timezone_name = get_setting(conn, "timezone", "America/Chicago")
    local_now = now_utc.astimezone(ZoneInfo(timezone_name))
    online = get_setting(conn, "internet_online", "0") == "1"
    display_on = display_should_be_on(conn, now_utc, timezone_name)
    selected_playlists = _selected_playlists(conn, local_now)
    playlist_ids = [playlist["id"] for playlist in selected_playlists]
    items: list[dict] = []
    seen_items: set[int] = set()
    rows = []
    if playlist_ids:
        placeholders = ",".join("?" for _ in playlist_ids)
        rows = conn.execute(
            f"""SELECT m.*, g.generation_key, g.kind generation_kind,
                      g.relative_path generation_path,
                      g.slide_count generation_slide_count,
                      g.duration_ms generation_duration_ms,
                      pi.playlist_id, pi.sort_order membership_order,
                      p.sort_order playlist_order
               FROM playlist_items pi
               JOIN playlists p ON p.id = pi.playlist_id
               JOIN media_items m ON m.id = pi.media_item_id
               LEFT JOIN media_generations g ON g.id = m.active_generation_id
               WHERE pi.playlist_id IN ({placeholders})
                 AND m.enabled = 1 AND m.deleted_at IS NULL
               ORDER BY p.sort_order, p.id, pi.sort_order, m.id""",  # nosec B608
            playlist_ids,
        ).fetchall()
    for row in rows:
        item = dict(row)
        if item["id"] in seen_items or not item_is_active(item, local_now):
            continue
        generation = None
        if item["generation_key"]:
            generation = {
                "generation_key": item["generation_key"],
                "kind": item["generation_kind"],
                "relative_path": item["generation_path"],
                "slide_count": item["generation_slide_count"],
                "duration_ms": item["generation_duration_ms"],
            }
        descriptor = _render_descriptor(
            item, generation, online=online, cache_port=cache_port
        )
        if descriptor["render_kind"] == "unavailable":
            continue
        dwell = item["duration_sec"]
        if descriptor.get("scrolling"):
            dwell = max(10, dwell or 20)
        if descriptor["render_kind"] == "deck":
            dwell = item["slide_sec"] * len(descriptor["slides"])
        elif descriptor["render_kind"] == "video" and dwell == 0 and generation:
            dwell = max(1, int((generation.get("duration_ms") or 20_000) / 1000))
        items.append(
            {
                "id": item["id"],
                "name": item["name"],
                "type": item["type"],
                "render_mode": item["render_mode"],
                "dwell_sec": dwell,
                "slide_sec": item["slide_sec"],
                "volume": item["volume"],
                "fit_mode": item["fit_mode"],
                "status": item["last_status"],
                "last_good_at": item["last_good_at"],
                **descriptor,
            }
        )
        seen_items.add(item["id"])

    version = int(get_setting(conn, "playlist_version", "1"))
    selection_payload = {
        "version": version,
        "display_on": display_on,
        "playlist_ids": playlist_ids,
        "item_ids": [item["id"] for item in items],
        "widgets": _widget_settings(conn),
    }
    selection_key = hashlib.sha256(
        json.dumps(selection_payload, sort_keys=True).encode()
    ).hexdigest()[:20]
    return {
        "time": now_utc.isoformat(timespec="seconds"),
        "local_time": local_now.isoformat(timespec="seconds"),
        "timezone": timezone_name,
        "online": online,
        "display_on": display_on,
        "display_off_reason": display_off_reason(conn, now_utc, timezone_name),
        "version": version,
        "selection_key": selection_key,
        "active_playlists": [
            {
                "id": value["id"],
                "name": value["name"],
                "default": bool(value["is_default"]),
            }
            for value in selected_playlists
        ],
        "widgets": _widget_settings(conn),
        "progress_window": _progress_window(selected_playlists, local_now),
        "items": items,
    }

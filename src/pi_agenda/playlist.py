from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .db import get_setting
from .playlist_models import active_nondefault_playlists
from .schedules import display_off_reason, display_should_be_on, item_is_active


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


def _selected_playlists(conn, local_now: datetime) -> list[dict]:
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
        "items": items,
    }

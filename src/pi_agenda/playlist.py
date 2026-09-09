from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .db import get_setting
from .schedules import display_should_be_on, item_is_active


def _render_descriptor(
    item: dict, generation: dict | None, *, online: bool, cache_port: int
) -> dict:
    item_type = item["type"]
    mode = item["render_mode"]
    use_live = online and mode in {"auto", "live"} and item_type in {"url", "ppt_link"}
    if use_live:
        return {
            "render_kind": "iframe",
            "render_url": item.get("embed_url") or item["source"],
            "slides": [],
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
        return {
            "render_kind": "iframe",
            "render_url": f"http://127.0.0.1:{cache_port}/{item_id}/{key}/index.html",
            "remote_fallback_url": f"{base}/screenshot.png",
            "slides": [],
        }
    if kind == "screenshot":
        return {
            "render_kind": "image",
            "render_url": f"{base}/screenshot.png",
            "slides": [],
        }
    return {"render_kind": "unavailable", "render_url": None, "slides": []}


def build_playlist(conn, *, cache_port: int, now_utc: datetime | None = None) -> dict:
    now_utc = now_utc or datetime.now(UTC)
    timezone_name = get_setting(conn, "timezone", "America/Chicago")
    local_now = now_utc.astimezone(ZoneInfo(timezone_name))
    online = get_setting(conn, "internet_online", "0") == "1"
    display_on = display_should_be_on(conn, now_utc, timezone_name)
    items: list[dict] = []
    rows = conn.execute(
        """SELECT m.*, g.generation_key, g.kind generation_kind,
                  g.relative_path generation_path, g.slide_count generation_slide_count,
                  g.duration_ms generation_duration_ms
           FROM media_items m
           LEFT JOIN media_generations g ON g.id = m.active_generation_id
           WHERE m.enabled = 1 AND m.deleted_at IS NULL ORDER BY m.sort_order, m.id"""
    ).fetchall()
    for row in rows:
        item = dict(row)
        if not item_is_active(item, local_now):
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
    return {
        "time": now_utc.isoformat(timespec="seconds"),
        "local_time": local_now.isoformat(timespec="seconds"),
        "timezone": timezone_name,
        "online": online,
        "display_on": display_on,
        "version": int(get_setting(conn, "playlist_version", "1")),
        "items": items,
    }

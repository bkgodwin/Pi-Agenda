from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .db import get_db, get_setting
from .playlist_models import playlist_ids_for_item
from .security import login_required

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _item_defaults(conn) -> dict[str, int]:
    return {
        "duration_sec": int(get_setting(conn, "default_duration_sec", "20")),
        "slide_sec": int(get_setting(conn, "default_slide_sec", "10")),
        "volume": int(get_setting(conn, "default_volume", "80")),
    }


@bp.get("")
@login_required
def dashboard():
    conn = get_db()
    counts = conn.execute(
        """SELECT COUNT(*) total,
           SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) enabled,
           SUM(CASE WHEN last_status = 'error' THEN 1 ELSE 0 END) errors
           FROM media_items WHERE deleted_at IS NULL"""
    ).fetchone()
    recent_jobs = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 8").fetchall()
    return render_template("dashboard.html", counts=counts, recent_jobs=recent_jobs)


@bp.get("/playlist")
@login_required
def playlist():
    row = (
        get_db()
        .execute(
            "SELECT id FROM playlists ORDER BY is_default DESC, sort_order, id LIMIT 1"
        )
        .fetchone()
    )
    return playlist_detail(row["id"]) if row else redirect(url_for("admin.playlists"))


@bp.get("/playlists")
@login_required
def playlists():
    rows = (
        get_db()
        .execute(
            """SELECT p.*, COUNT(m.id) item_count
           FROM playlists p LEFT JOIN playlist_items pi ON pi.playlist_id = p.id
           LEFT JOIN media_items m ON m.id = pi.media_item_id AND m.deleted_at IS NULL
           GROUP BY p.id ORDER BY p.is_default DESC, p.sort_order, p.id"""
        )
        .fetchall()
    )
    return render_template("playlists.html", playlists=rows)


@bp.get("/playlists/<int:playlist_id>")
@login_required
def playlist_detail(playlist_id: int):
    conn = get_db()
    selected = conn.execute(
        "SELECT * FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if not selected:
        abort(404)
    items = conn.execute(
        """SELECT m.*, g.generation_key, g.kind generation_kind, g.slide_count,
                  pi.sort_order membership_order
           FROM playlist_items pi
           JOIN media_items m ON m.id = pi.media_item_id
           LEFT JOIN media_generations g ON g.id = m.active_generation_id
           WHERE pi.playlist_id = ? AND m.deleted_at IS NULL
           ORDER BY pi.sort_order, m.id""",
        (playlist_id,),
    ).fetchall()
    return render_template("playlist.html", playlist=selected, items=items)


@bp.get("/items/new")
@login_required
def add_item():
    conn = get_db()
    playlists = conn.execute(
        "SELECT * FROM playlists ORDER BY is_default DESC, sort_order, id"
    ).fetchall()
    requested = request.args.get("playlist", type=int)
    if requested is None:
        default = next((row for row in playlists if row["is_default"]), None)
        requested = default["id"] if default else None
    return render_template(
        "item_form.html",
        item=None,
        playlists=playlists,
        selected_playlist_ids={requested} if requested else set(),
        defaults=_item_defaults(conn),
    )


@bp.get("/items/<int:item_id>/edit")
@login_required
def edit_item(item_id: int):
    conn = get_db()
    item = conn.execute(
        "SELECT * FROM media_items WHERE id = ? AND deleted_at IS NULL", (item_id,)
    ).fetchone()
    if not item:
        abort(404)
    playlists = conn.execute(
        "SELECT * FROM playlists ORDER BY is_default DESC, sort_order, id"
    ).fetchall()
    return render_template(
        "item_form.html",
        item=item,
        playlists=playlists,
        selected_playlist_ids=set(playlist_ids_for_item(conn, item_id)),
        defaults=_item_defaults(conn),
    )


@bp.get("/items/<int:item_id>/preview")
@login_required
def preview_item(item_id: int):
    conn = get_db()
    row = conn.execute(
        """SELECT m.*, g.generation_key, g.kind generation_kind, g.slide_count
           FROM media_items m LEFT JOIN media_generations g ON g.id = m.active_generation_id
           WHERE m.id = ? AND m.deleted_at IS NULL""",
        (item_id,),
    ).fetchone()
    if not row:
        abort(404)
    return render_template(
        "preview.html",
        item=row,
        cache_port=current_app.config["RUNTIME_CONFIG"].cache_port,
        online=get_setting(conn, "internet_online", "0") == "1",
    )


@bp.get("/items/<int:item_id>/preview-media/<generation_key>/<path:filename>")
@login_required
def preview_media(item_id: int, generation_key: str, filename: str):
    row = (
        get_db()
        .execute(
            """SELECT g.relative_path FROM media_generations g
           JOIN media_items m ON m.active_generation_id = g.id
           WHERE m.id = ? AND m.deleted_at IS NULL AND g.generation_key = ?""",
            (item_id, generation_key),
        )
        .fetchone()
    )
    if not row:
        abort(404)
    config = current_app.config["RUNTIME_CONFIG"]
    root = (config.data_dir / row["relative_path"]).resolve()
    generations_root = (config.data_dir / "generations").resolve()
    if generations_root not in root.parents:
        abort(404)
    if filename == "content":
        candidates = list(root.glob("content.*"))
        if len(candidates) != 1:
            abort(404)
        target = candidates[0]
    else:
        target = (root / filename).resolve()
    if (target != root and root not in target.parents) or not target.is_file():
        abort(404)
    return send_file(target, conditional=True)


@bp.get("/schedules")
@login_required
def schedules():
    rows = (
        get_db().execute("SELECT * FROM display_schedule ORDER BY weekday").fetchall()
    )
    return render_template("schedules.html", schedules=rows)


@bp.get("/settings")
@login_required
def settings():
    rows = get_db().execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return render_template(
        "settings.html",
        settings={row["key"]: row["value"] for row in rows},
        player_token=current_app.config.get("PLAYER_TOKEN", ""),
    )


@bp.get("/system")
@login_required
def system():
    return render_template("system.html")

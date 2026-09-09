from __future__ import annotations

from flask import Blueprint, current_app, render_template

from .db import get_db
from .security import login_required

bp = Blueprint("admin", __name__, url_prefix="/admin")


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
    items = (
        get_db()
        .execute(
            """SELECT m.*, g.generation_key, g.kind generation_kind, g.slide_count
           FROM media_items m LEFT JOIN media_generations g ON g.id = m.active_generation_id
           WHERE m.deleted_at IS NULL ORDER BY m.sort_order, m.id"""
        )
        .fetchall()
    )
    return render_template("playlist.html", items=items)


@bp.get("/items/new")
@login_required
def add_item():
    return render_template("item_form.html", item=None)


@bp.get("/items/<int:item_id>/edit")
@login_required
def edit_item(item_id: int):
    item = (
        get_db()
        .execute("SELECT * FROM media_items WHERE id = ?", (item_id,))
        .fetchone()
    )
    if not item:
        from flask import abort

        abort(404)
    return render_template("item_form.html", item=item)


@bp.get("/items/<int:item_id>/preview")
@login_required
def preview_item(item_id: int):
    row = (
        get_db()
        .execute(
            """SELECT m.*, g.generation_key, g.kind generation_kind, g.slide_count
           FROM media_items m LEFT JOIN media_generations g ON g.id = m.active_generation_id
           WHERE m.id = ? AND m.deleted_at IS NULL""",
            (item_id,),
        )
        .fetchone()
    )
    if not row:
        from flask import abort

        abort(404)
    return render_template("preview.html", item=row)


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

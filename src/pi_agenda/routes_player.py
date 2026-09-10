from __future__ import annotations

import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import psutil
from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .db import get_db, get_setting, set_setting, utcnow
from .playlist import build_playlist
from .security import is_loopback_request, player_access_required

bp = Blueprint("player", __name__)


def _addresses() -> list[str]:
    result: set[str] = set()
    for entries in psutil.net_if_addrs().values():
        for entry in entries:
            if entry.family.name == "AF_INET" and entry.address != "127.0.0.1":
                result.add(entry.address)
    return sorted(result)


@bp.get("/startup")
@player_access_required
def startup():
    seconds = max(5, int(get_setting(get_db(), "boot_splash_seconds", "5")))
    return render_template(
        "startup.html",
        addresses=_addresses(),
        seconds=seconds,
        port=current_app.config["RUNTIME_CONFIG"].port,
    )


@bp.get("/player")
@player_access_required
def player():
    return render_template(
        "player.html", exit_token=current_app.config.get("PLAYER_TOKEN", "")
    )


@bp.route("/player/connect", methods=["GET", "POST"])
def player_connect():
    conn = get_db()
    if get_setting(conn, "remote_player_enabled", "0") != "1":
        abort(404)
    message = None
    if request.method == "POST":
        expected = current_app.config.get("PLAYER_TOKEN", "")
        supplied = request.form.get("token", "")
        if expected and secrets.compare_digest(expected, supplied):
            response = make_response(redirect(url_for("player.player")))
            response.set_cookie(
                "pi_agenda_player",
                expected,
                max_age=30 * 24 * 60 * 60,
                httponly=True,
                samesite="Strict",
            )
            return response
        message = "The device token was not accepted."
    return render_template("player_connect.html", message=message)


@bp.get("/api/playlist-now")
@player_access_required
def playlist_now():
    payload = build_playlist(
        get_db(), cache_port=current_app.config["RUNTIME_CONFIG"].cache_port
    )
    if not is_loopback_request():
        for item in payload["items"]:
            fallback = item.pop("remote_fallback_url", None)
            if fallback:
                item["render_kind"] = "image"
                item["render_url"] = fallback
                item["slides"] = []
    else:
        for item in payload["items"]:
            item.pop("remote_fallback_url", None)
    bucket = int(datetime.now(UTC).timestamp() // 15)
    # Include the selection itself so a schedule boundary invalidates a cached
    # response immediately, even when it falls within the same time bucket.
    etag = f'"playlist-{payload["version"]}-{payload["selection_key"]}-{bucket}"'
    if request.headers.get("If-None-Match") == etag:
        return "", 304, {"ETag": etag, "Cache-Control": "no-store"}
    response = jsonify(ok=True, data=payload, error=None)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/health/player", methods=["GET", "POST"])
@player_access_required
def player_health():
    conn = get_db()
    if request.method == "POST":
        values = request.get_json(silent=True) or {}
        set_setting(conn, "player_heartbeat", utcnow())
        set_setting(
            conn, "player_visual_state", str(values.get("state", "playing"))[:32]
        )
        current_id = values.get("item_id")
        try:
            current_id = None if current_id is None else int(current_id)
        except (TypeError, ValueError):
            current_id = None
        set_setting(conn, "current_item_id", "" if current_id is None else current_id)
    return jsonify(
        ok=True,
        data={
            "online": get_setting(conn, "internet_online", "0") == "1",
            "checked_at": get_setting(conn, "internet_checked_at", ""),
        },
        error=None,
    )


@bp.post("/api/kiosk/exit")
def exit_kiosk():
    if not is_loopback_request():
        abort(403)
    expected = current_app.config.get("PLAYER_TOKEN", "")
    supplied = request.headers.get("X-Pi-Agenda-Player-Token", "")
    if not expected or not secrets.compare_digest(expected, supplied):
        abort(403)
    helper = Path("/usr/local/libexec/pi-agenda-kiosk-control")
    if not helper.is_file():
        return jsonify(
            ok=False,
            data=None,
            error={"code": "unavailable", "message": "Kiosk helper is unavailable"},
        ), 503
    result = subprocess.run(
        ["/usr/bin/sudo", str(helper), "exit"], timeout=10, check=False
    )
    if result.returncode != 0:
        return jsonify(
            ok=False,
            data=None,
            error={"code": "exit_failed", "message": "Could not exit the kiosk"},
        ), 500
    return jsonify(ok=True, data={"exiting": True}, error=None), 202


@bp.get("/media/<int:item_id>/<generation_key>/<path:filename>")
@player_access_required
def media(item_id: int, generation_key: str, filename: str):
    row = (
        get_db()
        .execute(
            """SELECT relative_path FROM media_generations
           WHERE media_item_id = ? AND generation_key = ?""",
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
    if target.parent != root or not target.is_file():
        abort(404)
    return send_file(target, conditional=True)

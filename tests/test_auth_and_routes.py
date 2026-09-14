from __future__ import annotations

import logging
import re
import subprocess
from types import SimpleNamespace

from pi_agenda import routes_api, routes_player
from pi_agenda.db import set_setting


def test_login_and_logout(client):
    response = client.get("/admin")
    assert response.status_code == 302
    login_page = client.get("/login")
    token = (
        re.search(rb'name="csrf_token" value="([^"]+)"', login_page.data)
        .group(1)
        .decode()
    )
    bad = client.post("/login", data={"csrf_token": token, "password": "wrong"})
    assert bad.status_code == 200
    good = client.post(
        "/login", data={"csrf_token": token, "password": "classroom-pass"}
    )
    assert good.status_code == 302
    assert client.get("/admin").status_code == 200


def test_admin_pages_render(authenticated_client):
    for path in (
        "/admin",
        "/admin/playlist",
        "/admin/items/new",
        "/admin/schedules",
        "/admin/widgets",
        "/admin/settings",
        "/admin/system",
    ):
        assert authenticated_client.get(path).status_code == 200


def test_api_requires_auth_and_csrf(app, authenticated_client):
    assert app.test_client().get("/api/items").status_code == 401
    response = authenticated_client.post("/api/system/refresh-all", json={})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "bad_request"


def test_player_is_loopback_only(client):
    assert client.get("/player").status_code == 200
    assert (
        client.get("/player", environ_base={"REMOTE_ADDR": "192.168.1.50"}).status_code
        == 403
    )
    assert (
        client.get(
            "/api/playlist-now", environ_base={"REMOTE_ADDR": "192.168.1.50"}
        ).status_code
        == 403
    )


def test_kiosk_exit_requires_player_token(client, app):
    app.config["PLAYER_TOKEN"] = "local-kiosk-secret"
    assert client.post("/api/kiosk/exit").status_code == 403
    response = client.post(
        "/api/kiosk/exit",
        headers={"X-Pi-Agenda-Player-Token": "local-kiosk-secret"},
    )
    assert response.status_code == 503


def test_remote_player_requires_enabled_device_token(app, client, db):
    remote = {"REMOTE_ADDR": "192.168.1.50"}
    assert client.get("/player/connect", environ_base=remote).status_code == 404
    set_setting(db, "remote_player_enabled", "1")
    app.config["PLAYER_TOKEN"] = "remote-device-secret"

    rejected = client.post(
        "/player/connect", data={"token": "wrong"}, environ_base=remote
    )
    assert rejected.status_code == 200
    connected = client.post(
        "/player/connect",
        data={"token": "remote-device-secret"},
        environ_base=remote,
    )
    assert connected.status_code == 302
    assert "token=" not in connected.headers["Location"]
    assert client.get("/player", environ_base=remote).status_code == 200
    assert client.get("/api/playlist-now", environ_base=remote).status_code == 200


def test_invalid_override_minutes_returns_validation_error(authenticated_client):
    response = authenticated_client.post(
        "/api/display/override",
        json={"state": "on", "minutes": "not-a-number"},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_override"


def test_schedule_api(authenticated_client):
    days = [
        {"mode": "window", "on_time": "07:30", "off_time": "16:00"}
        if day < 5
        else {"mode": "off", "on_time": "", "off_time": ""}
        for day in range(7)
    ]
    response = authenticated_client.put(
        "/api/schedule", json={"days": days}, headers={"X-CSRF-Token": "test-csrf"}
    )
    assert response.status_code == 200
    assert len(authenticated_client.get("/api/schedule").get_json()["data"]) == 7


def test_settings_validation_and_resolution_jobs(authenticated_client, db):
    invalid = authenticated_client.put(
        "/api/settings",
        json={"timezone": "Not/AZone"},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert invalid.status_code == 400
    valid = authenticated_client.put(
        "/api/settings",
        json={"resolution": "1080p", "boot_splash_seconds": "7"},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert valid.status_code == 200
    settings = authenticated_client.get("/api/settings").get_json()["data"]
    assert settings["resolution"] == "1080p"


def test_player_heartbeat_and_admin_display_status(authenticated_client):
    heartbeat = authenticated_client.post(
        "/api/health/player", json={"state": "playing", "item_id": None}
    )
    assert heartbeat.status_code == 200
    status = authenticated_client.get("/api/display/status")
    assert status.status_code == 200
    assert status.get_json()["data"]["heartbeat"]


def test_playlist_etag_changes_immediately_with_scheduled_selection(
    client, monkeypatch
):
    selection = {"key": "morning"}

    def playlist(_conn, *, cache_port):
        return {
            "version": 1,
            "selection_key": selection["key"],
            "items": [],
        }

    monkeypatch.setattr(routes_player, "build_playlist", playlist)
    first = client.get("/api/playlist-now")
    assert first.status_code == 200

    selection["key"] = "default"
    changed = client.get(
        "/api/playlist-now", headers={"If-None-Match": first.headers["ETag"]}
    )
    assert changed.status_code == 200
    assert changed.get_json()["data"]["selection_key"] == "default"


def test_update_reports_already_current(authenticated_client, monkeypatch):
    version = "a" * 40
    monkeypatch.setattr(routes_api.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"current={version}\nlatest={version}\n",
            stderr="",
        ),
    )
    response = authenticated_client.post(
        "/api/system/update",
        json={},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["up_to_date"] is True


def test_update_schedules_new_version_and_logs(
    authenticated_client, monkeypatch, caplog
):
    current = "a" * 40
    latest = "b" * 40
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[2] == "check":
            return SimpleNamespace(
                returncode=0,
                stdout=f"current={current}\nlatest={latest}\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="Running as unit x\n", stderr="")

    monkeypatch.setattr(routes_api.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    caplog.set_level(logging.INFO, logger="pi_agenda.routes_api")

    response = authenticated_client.post(
        "/api/system/update",
        json={},
        headers={"X-CSRF-Token": "test-csrf"},
    )

    assert response.status_code == 202
    assert response.get_json()["data"]["to"] == latest
    assert calls == [
        ["/usr/bin/sudo", "/usr/local/libexec/pi-agenda-update", "check"],
        ["/usr/bin/sudo", "/usr/local/libexec/pi-agenda-update", "update", latest],
    ]
    assert "update scheduled successfully" in caplog.text


def test_update_schedule_failure_returns_helper_stderr(
    authenticated_client, monkeypatch
):
    current = "a" * 40
    latest = "b" * 40

    def fake_run(args, **_kwargs):
        if args[2] == "check":
            return SimpleNamespace(
                returncode=0,
                stdout=f"current={current}\nlatest={latest}\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="unit already exists")

    monkeypatch.setattr(routes_api.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(subprocess, "run", fake_run)

    response = authenticated_client.post(
        "/api/system/update",
        json={},
        headers={"X-CSRF-Token": "test-csrf"},
    )

    assert response.status_code == 500
    assert response.get_json()["error"]["message"] == "unit already exists"

from __future__ import annotations

import io
import zipfile

from PIL import Image

from pi_agenda.backup import restore_backup
from pi_agenda.db import connect, get_setting, set_setting
from pi_agenda.jobs import claim_job, finish_job
from pi_agenda.worker import process_item_job


def png_upload() -> io.BytesIO:
    output = io.BytesIO()
    Image.new("RGB", (320, 180), (25, 120, 90)).save(output, format="PNG")
    output.seek(0)
    return output


def test_image_upload_conversion_and_playlist(authenticated_client, runtime):
    response = authenticated_client.post(
        "/api/items",
        data={
            "name": "Class photo",
            "type": "image",
            "file": (png_upload(), "class.png"),
            "days_mask": "127",
            "duration_sec": "15",
            "slide_sec": "10",
            "volume": "80",
            "fit_mode": "contain",
            "render_mode": "auto",
            "enabled": "1",
        },
        headers={"X-CSRF-Token": "test-csrf"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201, response.get_json()
    item_id = response.get_json()["data"]["id"]

    conn = connect(runtime.db_path)
    try:
        job = claim_job(conn)
        assert job is not None
        process_item_job(runtime, conn, job)
        finish_job(conn, job["id"])
        item = conn.execute(
            "SELECT * FROM media_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert item["last_status"] == "ok"
        generation = conn.execute(
            "SELECT * FROM media_generations WHERE id = ?",
            (item["active_generation_id"],),
        ).fetchone()
        assert generation["kind"] == "image"
    finally:
        conn.close()

    playlist = authenticated_client.get("/api/playlist-now").get_json()["data"]
    matching = [item for item in playlist["items"] if item["id"] == item_id]
    assert matching and matching[0]["render_kind"] == "image"
    media_url = matching[0]["render_url"]
    assert authenticated_client.get(media_url).status_code == 200

    backup = authenticated_client.get("/api/system/backup")
    assert backup.status_code == 200
    with zipfile.ZipFile(io.BytesIO(backup.data)) as bundle:
        names = set(bundle.namelist())
        assert "db.sqlite" in names
        assert "manifest.json" in names
        assert any(name.startswith(f"uploads/{item_id}/") for name in names)
        assert any(name.startswith(f"generations/{item_id}/") for name in names)

    archive = runtime.data_dir / "backups" / "test-restore.zip"
    archive.write_bytes(backup.data)
    conn = connect(runtime.db_path)
    set_setting(conn, "site_name", "Changed after backup")
    conn.close()
    restore_backup(runtime, archive)
    conn = connect(runtime.db_path)
    try:
        assert get_setting(conn, "site_name") == "Pi-Agenda"
        restored = conn.execute(
            "SELECT active_generation_id FROM media_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert restored["active_generation_id"]
    finally:
        conn.close()


def test_soft_delete_preserves_generation_during_grace(authenticated_client, runtime):
    response = authenticated_client.post(
        "/api/items",
        data={"name": "Photo", "type": "image", "file": (png_upload(), "photo.png")},
        headers={"X-CSRF-Token": "test-csrf"},
        content_type="multipart/form-data",
    )
    item_id = response.get_json()["data"]["id"]
    conn = connect(runtime.db_path)
    job = claim_job(conn)
    process_item_job(runtime, conn, job)
    finish_job(conn, job["id"])
    generation = conn.execute(
        "SELECT generation_key FROM media_generations WHERE media_item_id = ?",
        (item_id,),
    ).fetchone()["generation_key"]
    conn.close()
    assert (
        authenticated_client.delete(
            f"/api/items/{item_id}", headers={"X-CSRF-Token": "test-csrf"}
        ).status_code
        == 200
    )
    assert (
        authenticated_client.get(f"/media/{item_id}/{generation}/content").status_code
        == 200
    )
    assert all(
        item["id"] != item_id
        for item in authenticated_client.get("/api/items").get_json()["data"]
    )

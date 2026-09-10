from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from pi_agenda.db import connect, migrate, set_setting
from pi_agenda.jobs import claim_job, enqueue_job, fail_job
from pi_agenda.models import create_item
from pi_agenda.playlist import _render_descriptor, build_playlist
from pi_agenda.playlist_models import (
    assign_item_to_playlists,
    create_playlist,
    playlist_ids_for_item,
)


def _announcement(name: str, text: str, **extra):
    return {
        "name": name,
        "type": "announcement",
        "source": text,
        "duration_sec": 10,
        **extra,
    }


def test_playlist_and_item_schedules_combine_with_default_fallback(db):
    set_setting(db, "timezone", "UTC")
    db.execute("UPDATE display_schedule SET mode = 'always_on' WHERE weekday = 0")
    default_id = db.execute("SELECT id FROM playlists WHERE is_default = 1").fetchone()[
        "id"
    ]
    default_item = create_item(db, _announcement("Fallback", "Welcome"))
    assign_item_to_playlists(db, default_item, [default_id])

    class_id = create_playlist(
        db,
        {
            "name": "First period",
            "days_mask": 1,
            "start_time": "09:00",
            "end_time": "10:00",
        },
    )
    class_item = create_item(
        db,
        _announcement(
            "Opening reminder",
            "Turn in homework",
            days_mask=1,
            start_time="09:10",
            end_time="09:20",
        ),
    )
    assign_item_to_playlists(db, class_item, [class_id])

    before_item = build_playlist(
        db, cache_port=8002, now_utc=datetime(2026, 9, 7, 9, 5, tzinfo=UTC)
    )
    assert [value["name"] for value in before_item["active_playlists"]] == [
        "First period"
    ]
    assert before_item["items"] == []

    during_item = build_playlist(
        db, cache_port=8002, now_utc=datetime(2026, 9, 7, 9, 15, tzinfo=UTC)
    )
    assert [value["name"] for value in during_item["items"]] == ["Opening reminder"]

    after_class = build_playlist(
        db, cache_port=8002, now_utc=datetime(2026, 9, 7, 10, 5, tzinfo=UTC)
    )
    assert after_class["active_playlists"][0]["default"] is True
    assert [value["name"] for value in after_class["items"]] == ["Fallback"]
    assert before_item["selection_key"] != during_item["selection_key"]


def test_simultaneous_playlists_combine_and_deduplicate_shared_items(db):
    set_setting(db, "timezone", "UTC")
    db.execute("UPDATE display_schedule SET mode = 'always_on' WHERE weekday = 0")
    first = create_playlist(db, {"name": "First", "days_mask": 1})
    second = create_playlist(db, {"name": "Second", "days_mask": 1})
    shared = create_item(db, _announcement("Shared", "Once"))
    second_only = create_item(db, _announcement("Second only", "After shared"))
    assign_item_to_playlists(db, shared, [first, second])
    assign_item_to_playlists(db, second_only, [second])

    result = build_playlist(
        db, cache_port=8002, now_utc=datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    )
    assert [value["name"] for value in result["active_playlists"]] == [
        "First",
        "Second",
    ]
    assert [value["name"] for value in result["items"]] == ["Shared", "Second only"]


def test_announcement_api_and_playlist_management(authenticated_client, db):
    default_id = db.execute("SELECT id FROM playlists WHERE is_default = 1").fetchone()[
        "id"
    ]
    playlist_response = authenticated_client.post(
        "/api/playlists",
        json={"name": "Art class", "days_mask": 31},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert playlist_response.status_code == 201
    playlist_id = playlist_response.get_json()["data"]["id"]

    response = authenticated_client.post(
        "/api/items",
        json={
            "name": "Reminder",
            "type": "announcement",
            "announcement_text": "Bring your sketchbook",
            "background_color": "#112233",
            "text_color": "#fefefe",
            "text_size": 72,
            "text_align": "left",
            "days_mask": 127,
            "playlist_selection": 1,
            "playlist_ids": [default_id, playlist_id],
        },
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["data"]["job_id"] is None
    item = authenticated_client.get(
        f"/api/items/{response.get_json()['data']['id']}"
    ).get_json()["data"]
    assert item["source"] == "Bring your sketchbook"
    assert item["playlist_ids"] == [default_id, playlist_id]

    made_default = authenticated_client.post(
        f"/api/playlists/{playlist_id}/default",
        json={},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert made_default.status_code == 200
    new_default = db.execute(
        "SELECT id, start_time FROM playlists WHERE is_default = 1"
    ).fetchone()
    assert new_default["id"] == playlist_id
    assert new_default["start_time"] is None
    former_default = db.execute(
        "SELECT is_default, enabled FROM playlists WHERE id = ?", (default_id,)
    ).fetchone()
    assert dict(former_default) == {"is_default": 0, "enabled": 0}


def test_hidden_announcement_field_does_not_replace_uploaded_source(
    authenticated_client, db
):
    default_id = db.execute("SELECT id FROM playlists WHERE is_default = 1").fetchone()[
        "id"
    ]
    item_id = create_item(
        db,
        {
            "name": "Photo",
            "type": "image",
            "source": "uploads/1/original.png",
        },
    )
    assign_item_to_playlists(db, item_id, [default_id])
    second_id = create_item(db, _announcement("Second", "Second item"))
    assign_item_to_playlists(db, second_id, [default_id])
    order_before = [
        row["media_item_id"]
        for row in db.execute(
            "SELECT media_item_id FROM playlist_items WHERE playlist_id = ? ORDER BY sort_order",
            (default_id,),
        )
    ]
    response = authenticated_client.patch(
        f"/api/items/{item_id}",
        json={
            "name": "Updated photo",
            "source": "",
            "announcement_text": "",
            "playlist_ids": [default_id],
            "playlist_selection": 1,
        },
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == 200, response.get_json()
    item = db.execute(
        "SELECT name, source FROM media_items WHERE id = ?", (item_id,)
    ).fetchone()
    assert dict(item) == {
        "name": "Updated photo",
        "source": "uploads/1/original.png",
    }
    order_after = [
        row["media_item_id"]
        for row in db.execute(
            "SELECT media_item_id FROM playlist_items WHERE playlist_id = ? ORDER BY sort_order",
            (default_id,),
        )
    ]
    assert order_after == order_before


def test_invalid_playlist_assignment_does_not_create_item(authenticated_client, db):
    before = db.execute("SELECT COUNT(*) count FROM media_items").fetchone()["count"]
    response = authenticated_client.post(
        "/api/items",
        json={
            "name": "Invalid assignment",
            "type": "announcement",
            "announcement_text": "Should not persist",
            "playlist_selection": 1,
            "playlist_ids": [999999],
        },
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == 400
    after = db.execute("SELECT COUNT(*) count FROM media_items").fetchone()["count"]
    assert after == before


def test_blank_test_and_holiday_force_display_off(authenticated_client, db):
    set_setting(db, "timezone", "UTC")
    db.execute("UPDATE display_schedule SET mode = 'always_on'")
    headers = {"X-CSRF-Token": "test-csrf"}

    assert (
        authenticated_client.post(
            "/api/display/test-blank", json={"state": "start"}, headers=headers
        ).status_code
        == 200
    )
    blanked = build_playlist(db, cache_port=8002)
    assert blanked["display_on"] is False
    assert blanked["display_off_reason"] == "blanking_test"
    authenticated_client.post(
        "/api/display/test-blank", json={"state": "stop"}, headers=headers
    )

    holiday = authenticated_client.post(
        "/api/display/holiday", json={"days": 7}, headers=headers
    )
    assert holiday.status_code == 200
    paused = build_playlist(db, cache_port=8002)
    assert paused["display_on"] is False
    assert paused["display_off_reason"] == "holiday"
    assert (
        authenticated_client.delete("/api/display/holiday", headers=headers).status_code
        == 200
    )


def test_every_render_kind_has_a_playable_descriptor():
    base = {
        "id": 7,
        "type": "image",
        "source": "uploads/7/original.png",
        "render_mode": "auto",
        "background_color": "#000000",
        "text_color": "#ffffff",
        "text_size": 64,
        "text_align": "center",
        "web_zoom": 110,
    }
    generation = {
        "generation_key": "abc",
        "kind": "image",
        "slide_count": 0,
        "duration_ms": None,
    }
    assert (
        _render_descriptor(base, generation, online=True, cache_port=8002)[
            "render_kind"
        ]
        == "image"
    )
    assert (
        _render_descriptor(
            base | {"type": "video"},
            generation | {"kind": "video"},
            online=True,
            cache_port=8002,
        )["render_kind"]
        == "video"
    )
    assert (
        _render_descriptor(
            base | {"type": "pdf_deck"},
            generation | {"kind": "slides", "slide_count": 2},
            online=True,
            cache_port=8002,
        )["render_kind"]
        == "deck"
    )
    assert (
        _render_descriptor(
            base | {"type": "url"},
            generation | {"kind": "website_bundle"},
            online=True,
            cache_port=8002,
        )["render_kind"]
        == "image"
    )
    assert (
        _render_descriptor(
            base | {"type": "url", "render_mode": "archive"},
            generation | {"kind": "website_bundle"},
            online=True,
            cache_port=8002,
        )["render_kind"]
        == "iframe"
    )
    assert (
        _render_descriptor(
            base | {"type": "url", "render_mode": "live"},
            None,
            online=True,
            cache_port=8002,
        )["render_kind"]
        == "iframe"
    )
    assert (
        _render_descriptor(
            base | {"type": "url", "render_mode": "auto"},
            None,
            online=True,
            cache_port=8002,
        )["render_kind"]
        == "unavailable"
    )
    assert (
        _render_descriptor(
            base | {"type": "announcement", "source": "Hello"},
            None,
            online=False,
            cache_port=8002,
        )["render_kind"]
        == "announcement"
    )


def test_failed_job_retries_before_becoming_terminal(runtime):
    migrate(runtime.db_path)
    conn = connect(runtime.db_path)
    try:
        item_id = create_item(conn, _announcement("Retry", "Test"))
        job_id = enqueue_job(conn, "refresh", item_id)
        job = claim_job(conn)
        fail_job(conn, job, "temporary failure")
        retry = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert retry["state"] == "queued"
        assert retry["stage"] == "retrying"
        assert retry["attempt"] == 1
    finally:
        conn.close()


def test_v1_database_migrates_items_into_default_playlist(tmp_path):
    database = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(database)
    legacy.executescript(
        """
        CREATE TABLE media_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          type TEXT NOT NULL CHECK(type IN ('ppt_file','pdf_deck','ppt_link','image','url','video')),
          source TEXT NOT NULL,
          embed_url TEXT,
          render_mode TEXT NOT NULL DEFAULT 'auto',
          duration_sec INTEGER NOT NULL DEFAULT 20,
          slide_sec INTEGER NOT NULL DEFAULT 10,
          volume INTEGER NOT NULL DEFAULT 80,
          fit_mode TEXT NOT NULL DEFAULT 'contain',
          days_mask INTEGER NOT NULL DEFAULT 127,
          start_time TEXT,
          end_time TEXT,
          enabled INTEGER NOT NULL DEFAULT 1,
          sort_order INTEGER NOT NULL DEFAULT 0,
          active_generation_id INTEGER,
          last_checked TEXT,
          last_good_at TEXT,
          last_status TEXT NOT NULL DEFAULT 'never',
          last_error TEXT,
          deleted_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE media_generations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          media_item_id INTEGER NOT NULL,
          generation_key TEXT NOT NULL UNIQUE,
          kind TEXT NOT NULL,
          relative_path TEXT NOT NULL,
          slide_count INTEGER NOT NULL DEFAULT 0,
          duration_ms INTEGER,
          content_hash TEXT NOT NULL,
          source_etag TEXT,
          source_last_modified TEXT,
          created_at TEXT NOT NULL,
          verified_at TEXT NOT NULL,
          retire_after TEXT,
          FOREIGN KEY(media_item_id) REFERENCES media_items(id) ON DELETE CASCADE
        );
        INSERT INTO media_items(name, type, source, created_at, updated_at)
        VALUES ('Existing photo', 'image', 'uploads/1/original.png', 'now', 'now');
        INSERT INTO media_generations(
          media_item_id, generation_key, kind, relative_path, content_hash,
          created_at, verified_at)
        VALUES (1, 'legacy-key', 'image', 'generations/1/legacy-key', 'hash', 'now', 'now');
        UPDATE media_items SET active_generation_id = 1 WHERE id = 1;
        """
    )
    legacy.close()

    migrate(database)
    upgraded = connect(database)
    try:
        item = upgraded.execute("SELECT * FROM media_items WHERE id = 1").fetchone()
        assert item["name"] == "Existing photo"
        assert item["web_zoom"] == 100
        assert item["active_generation_id"] == 1
        assert (
            upgraded.execute(
                "SELECT media_item_id FROM media_generations WHERE id = 1"
            ).fetchone()["media_item_id"]
            == 1
        )
        default = upgraded.execute(
            "SELECT id FROM playlists WHERE is_default = 1"
        ).fetchone()
        assert upgraded.execute(
            """SELECT 1 FROM playlist_items
               WHERE playlist_id = ? AND media_item_id = 1""",
            (default["id"],),
        ).fetchone()
        create_item(upgraded, _announcement("New announcement", "Hello"))
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        upgraded.close()


def test_repeated_migration_preserves_exclusive_playlist_membership(runtime):
    migrate(runtime.db_path)
    conn = connect(runtime.db_path)
    try:
        playlist_id = create_playlist(conn, {"name": "Only here"})
        item_id = create_item(conn, _announcement("Exclusive", "Hello"))
        assign_item_to_playlists(conn, item_id, [playlist_id])
    finally:
        conn.close()

    migrate(runtime.db_path)
    checked = connect(runtime.db_path)
    try:
        assert playlist_ids_for_item(checked, item_id) == [playlist_id]
    finally:
        checked.close()

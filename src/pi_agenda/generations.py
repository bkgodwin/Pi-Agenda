from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .db import bump_playlist_version, transaction, utcnow

STALE_STAGING_PREFIXES = ("pi-agenda-chromium-", "restore-", "pi-agenda-backup-")


def directory_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(file_path.relative_to(path).as_posix().encode())
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def publish_generation(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    item_id: int,
    staging_dir: Path,
    kind: str,
    slide_count: int = 0,
    duration_ms: int | None = None,
    source_etag: str | None = None,
    source_last_modified: str | None = None,
) -> int:
    files = [path for path in staging_dir.rglob("*") if path.is_file()]
    if not files or any(path.stat().st_size <= 0 for path in files):
        raise ValueError("Generation did not produce valid files")
    content_hash = directory_hash(staging_dir)
    generation_key = uuid.uuid4().hex
    final_parent = data_dir / "generations" / str(item_id)
    final_parent.mkdir(parents=True, exist_ok=True)
    final_path = final_parent / generation_key
    os.replace(staging_dir, final_path)
    now = utcnow()
    retire_at = (datetime.now(UTC) + timedelta(hours=24)).isoformat(timespec="seconds")
    relative_path = final_path.relative_to(data_dir).as_posix()
    try:
        with transaction(conn):
            previous = conn.execute(
                "SELECT active_generation_id FROM media_items WHERE id = ?", (item_id,)
            ).fetchone()
            if not previous:
                raise LookupError("Media item no longer exists")
            cursor = conn.execute(
                """INSERT INTO media_generations(
                     media_item_id, generation_key, kind, relative_path, slide_count,
                     duration_ms, content_hash, source_etag, source_last_modified,
                     created_at, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    generation_key,
                    kind,
                    relative_path,
                    slide_count,
                    duration_ms,
                    content_hash,
                    source_etag,
                    source_last_modified,
                    now,
                    now,
                ),
            )
            generation_id = int(cursor.lastrowid)
            if previous["active_generation_id"]:
                conn.execute(
                    "UPDATE media_generations SET retire_after = ? WHERE id = ?",
                    (retire_at, previous["active_generation_id"]),
                )
            conn.execute(
                """UPDATE media_items SET active_generation_id = ?, last_checked = ?,
                       last_good_at = ?, last_status = 'ok', last_error = NULL, updated_at = ?
                   WHERE id = ?""",
                (generation_id, now, now, now, item_id),
            )
            bump_playlist_version(conn)
        return generation_id
    except Exception:
        shutil.rmtree(final_path, ignore_errors=True)
        raise


def cleanup_retired(conn: sqlite3.Connection, data_dir: Path) -> int:
    delete_before = (datetime.now(UTC) - timedelta(hours=24)).isoformat(
        timespec="seconds"
    )
    deleted_items = conn.execute(
        "SELECT id FROM media_items WHERE deleted_at IS NOT NULL AND deleted_at <= ?",
        (delete_before,),
    ).fetchall()
    for item in deleted_items:
        shutil.rmtree(data_dir / "uploads" / str(item["id"]), ignore_errors=True)
        shutil.rmtree(data_dir / "generations" / str(item["id"]), ignore_errors=True)
        conn.execute("DELETE FROM media_items WHERE id = ?", (item["id"],))
    rows = conn.execute(
        """SELECT g.id, g.relative_path FROM media_generations g
           LEFT JOIN media_items m ON m.active_generation_id = g.id
           WHERE m.id IS NULL AND g.retire_after IS NOT NULL AND g.retire_after <= ?""",
        (utcnow(),),
    ).fetchall()
    removed = 0
    for row in rows:
        path = (data_dir / row["relative_path"]).resolve()
        root = (data_dir / "generations").resolve()
        if path != root and root in path.parents:
            shutil.rmtree(path, ignore_errors=True)
        conn.execute("DELETE FROM media_generations WHERE id = ?", (row["id"],))
        removed += 1
    return removed + len(deleted_items) + cleanup_staging(data_dir)


def cleanup_staging(data_dir: Path, *, older_than_hours: int = 6) -> int:
    staging = data_dir / "staging"
    if not staging.exists():
        return 0
    cutoff = datetime.now(UTC).timestamp() - older_than_hours * 3600
    removed = 0
    for path in staging.iterdir():
        if not path.exists():
            continue
        name = path.name
        is_job_staging = "-" in name and name.split("-", 1)[0].isdigit()
        is_temp_staging = name.startswith(STALE_STAGING_PREFIXES)
        if not is_job_staging and not is_temp_staging:
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed

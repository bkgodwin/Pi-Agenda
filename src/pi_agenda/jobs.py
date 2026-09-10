from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from .db import utcnow


def enqueue_job(
    conn: sqlite3.Connection,
    kind: str,
    media_item_id: int | None = None,
    *,
    not_before: str | None = None,
    coalesce: bool = True,
) -> int:
    if coalesce:
        existing = conn.execute(
            """SELECT id FROM jobs
               WHERE kind = ? AND media_item_id IS ? AND state IN ('queued','running')
               ORDER BY id LIMIT 1""",
            (kind, media_item_id),
        ).fetchone()
        if existing:
            return int(existing["id"])
    created = utcnow()
    cursor = conn.execute(
        """INSERT INTO jobs(kind, media_item_id, state, progress, stage, attempt,
                            not_before, created_at)
           VALUES (?, ?, 'queued', 0, 'queued', 0, ?, ?)""",
        (kind, media_item_id, not_before or created, created),
    )
    if media_item_id is not None:
        conn.execute(
            "UPDATE media_items SET last_status = 'queued', updated_at = ? WHERE id = ?",
            (created, media_item_id),
        )
    return int(cursor.lastrowid)


def claim_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    now = utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT * FROM jobs
               WHERE state = 'queued' AND not_before <= ?
               ORDER BY id LIMIT 1""",
            (now,),
        ).fetchone()
        if not row:
            conn.commit()
            return None
        updated = conn.execute(
            """UPDATE jobs SET state = 'running', progress = 1, stage = 'starting',
                               attempt = attempt + 1, started_at = ?, error = NULL
               WHERE id = ? AND state = 'queued'""",
            (now, row["id"]),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return None
        if row["media_item_id"] is not None:
            conn.execute(
                """UPDATE media_items SET last_status = 'refreshing', last_checked = ?,
                                      updated_at = ? WHERE id = ?""",
                (now, now, row["media_item_id"]),
            )
        conn.commit()
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
    except Exception:
        conn.rollback()
        raise


def update_job(
    conn: sqlite3.Connection, job_id: int, progress: int, stage: str
) -> None:
    conn.execute(
        "UPDATE jobs SET progress = ?, stage = ? WHERE id = ? AND state = 'running'",
        (max(0, min(100, int(progress))), stage[:120], job_id),
    )


def finish_job(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        """UPDATE jobs SET state = 'succeeded', progress = 100, stage = 'complete',
                           finished_at = ? WHERE id = ?""",
        (utcnow(), job_id),
    )


def fail_job(conn: sqlite3.Connection, job: sqlite3.Row, error: str) -> None:
    now = utcnow()
    safe_error = error.strip()[:1000] or "Unknown job failure"
    if int(job["attempt"]) < 3:
        delay = 30 * (2 ** max(0, int(job["attempt"]) - 1))
        retry_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(
            timespec="seconds"
        )
        conn.execute(
            """UPDATE jobs SET state = 'queued', progress = 0, stage = 'retrying',
                 not_before = ?, started_at = NULL, error = ? WHERE id = ?""",
            (retry_at, safe_error, job["id"]),
        )
        if job["media_item_id"] is not None:
            has_generation = conn.execute(
                "SELECT active_generation_id FROM media_items WHERE id = ?",
                (job["media_item_id"],),
            ).fetchone()
            status = "stale" if has_generation and has_generation[0] else "queued"
            conn.execute(
                """UPDATE media_items SET last_status = ?, last_error = ?,
                     last_checked = ?, updated_at = ? WHERE id = ?""",
                (status, safe_error, now, now, job["media_item_id"]),
            )
        return
    conn.execute(
        """UPDATE jobs SET state = 'failed', stage = 'failed', finished_at = ?, error = ?
           WHERE id = ?""",
        (now, safe_error, job["id"]),
    )
    if job["media_item_id"] is not None:
        conn.execute(
            """UPDATE media_items SET last_status = 'error', last_error = ?,
                                  last_checked = ?, updated_at = ? WHERE id = ?""",
            (safe_error, now, now, job["media_item_id"]),
        )


def recover_interrupted_jobs(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """UPDATE jobs SET state = 'queued', progress = 0, stage = 'recovered',
                           started_at = NULL, not_before = ?
           WHERE state = 'running'""",
        (utcnow(),),
    )
    return cursor.rowcount

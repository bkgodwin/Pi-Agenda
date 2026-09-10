from __future__ import annotations

import logging
import shutil
import signal
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import RuntimeConfig
from .db import connect, get_setting, migrate, set_setting, utcnow
from .generations import cleanup_retired, publish_generation
from .jobs import (
    claim_job,
    enqueue_job,
    fail_job,
    finish_job,
    recover_interrupted_jobs,
    update_job,
)
from .pipelines.common import PipelineError, staging_directory
from .pipelines.image import process_image
from .pipelines.m365 import refresh_m365
from .pipelines.presentation import render_presentation
from .pipelines.video import process_video
from .pipelines.website import archive_website
from .schedules import display_should_be_on
from .security import validate_remote_url

LOG = logging.getLogger("pi_agenda.worker")
STOP = False
WORKER_STARTED = time.monotonic()


def _stop(_signum, _frame) -> None:
    global STOP
    STOP = True


def _allowlist(conn) -> set[str]:
    return {
        value.strip().lower()
        for value in get_setting(conn, "intranet_allowlist", "").split(",")
        if value.strip()
    }


def _resolution(conn) -> str:
    value = get_setting(conn, "resolution", "720p")
    return value if value in {"720p", "1080p"} else "720p"


def _source_path(config: RuntimeConfig, item: dict) -> Path:
    path = (config.data_dir / item["source"]).resolve()
    uploads = (config.data_dir / "uploads").resolve()
    if uploads not in path.parents:
        raise PipelineError("Stored source path is outside the upload directory")
    if not path.is_file():
        raise PipelineError("Original upload is missing")
    return path


def process_item_job(config: RuntimeConfig, conn, job) -> None:
    item_row = conn.execute(
        "SELECT * FROM media_items WHERE id = ? AND deleted_at IS NULL",
        (job["media_item_id"],),
    ).fetchone()
    if not item_row:
        raise PipelineError("Media item was deleted")
    item = dict(item_row)
    staging = staging_directory(config.data_dir, int(job["id"]))
    resolution = _resolution(conn)
    try:
        update_job(conn, job["id"], 10, "preparing")
        kind = ""
        slide_count = 0
        duration_ms = None
        if item["type"] in {"ppt_file", "pdf_deck"}:
            update_job(conn, job["id"], 25, "rendering slides")
            slide_count = render_presentation(
                _source_path(config, item), staging, resolution=resolution
            )
            kind = "slides"
        elif item["type"] == "image":
            update_job(conn, job["id"], 30, "optimizing image")
            process_image(_source_path(config, item), staging, resolution=resolution)
            kind = "image"
        elif item["type"] == "video":
            update_job(conn, job["id"], 20, "processing video")
            duration_ms = process_video(
                _source_path(config, item), staging, resolution=resolution
            )
            kind = "video"
        elif item["type"] == "ppt_link":
            update_job(conn, job["id"], 20, "refreshing Microsoft presentation")
            kind, slide_count = refresh_m365(
                item["embed_url"] or item["source"],
                staging,
                resolution=resolution,
                max_bytes=config.max_remote_bytes,
                mode=item["render_mode"],
            )
        elif item["type"] == "url":
            update_job(conn, job["id"], 20, "capturing website")
            url = validate_remote_url(item["source"], _allowlist(conn))
            size = (1280, 720) if resolution == "720p" else (1920, 1080)
            archive_website(
                url,
                staging,
                allowlist=_allowlist(conn),
                max_total_bytes=config.max_archive_bytes,
                screenshot_size=size,
            )
            screenshot = staging / "screenshot.png"
            if (
                item["render_mode"] in {"auto", "screenshot"}
                and not screenshot.is_file()
            ):
                raise PipelineError("Website screenshot could not be generated")
            if item["render_mode"] == "screenshot":
                for child in list(staging.iterdir()):
                    if child.name not in {"screenshot.png", "thumbnail.jpg"}:
                        shutil.rmtree(
                            child, ignore_errors=True
                        ) if child.is_dir() else child.unlink()
                kind = "screenshot"
            else:
                kind = "website_bundle"
        else:
            raise PipelineError("Unsupported item type")
        update_job(conn, job["id"], 90, "publishing")
        publish_generation(
            conn,
            data_dir=config.data_dir,
            item_id=item["id"],
            staging_dir=staging,
            kind=kind,
            slide_count=slide_count,
            duration_ms=duration_ms,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _probe_internet(conn) -> None:
    online = False
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=3):
                online = True
                break
        except OSError:
            continue
    set_setting(conn, "internet_online", "1" if online else "0")
    set_setting(conn, "internet_checked_at", utcnow())


def _enqueue_periodic(conn) -> None:
    timezone_name = get_setting(conn, "timezone", "America/Chicago")
    local_now = datetime.now(UTC).astimezone(ZoneInfo(timezone_name))
    check_time = get_setting(conn, "check_time", "06:00")
    refresh_date = get_setting(conn, "last_daily_refresh_date", "")
    if (
        local_now.strftime("%H:%M") >= check_time
        and refresh_date != local_now.date().isoformat()
    ):
        rows = conn.execute(
            "SELECT id FROM media_items WHERE type IN ('url','ppt_link') AND deleted_at IS NULL"
        ).fetchall()
        for row in rows:
            enqueue_job(conn, "refresh", row["id"])
        set_setting(conn, "last_daily_refresh_date", local_now.date().isoformat())

    retry_cutoff = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(
        timespec="seconds"
    )
    failed = conn.execute(
        """SELECT id FROM media_items WHERE type IN ('url','ppt_link') AND deleted_at IS NULL
           AND last_status = 'error' AND (last_checked IS NULL OR last_checked <= ?)""",
        (retry_cutoff,),
    ).fetchall()
    for row in failed:
        enqueue_job(conn, "refresh", row["id"])


def _apply_power_state(conn) -> None:
    timezone_name = get_setting(conn, "timezone", "America/Chicago")
    desired = display_should_be_on(conn, datetime.now(UTC), timezone_name)
    current = get_setting(conn, "display_power_state", "unknown")
    desired_name = "on" if desired else "off"
    set_setting(conn, "display_desired_state", desired_name)
    if current == desired_name:
        set_setting(conn, "display_power_pending_at", "")
        return
    if (
        desired_name == "off"
        and get_setting(conn, "player_visual_state", "") != "black"
    ):
        pending_text = get_setting(conn, "display_power_pending_at", "")
        if not pending_text:
            set_setting(conn, "display_power_pending_at", utcnow())
            return
        try:
            pending_at = datetime.fromisoformat(pending_text)
            if pending_at.tzinfo is None:
                pending_at = pending_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - pending_at < timedelta(seconds=20):
                return
        except ValueError:
            set_setting(conn, "display_power_pending_at", utcnow())
            return
    helper = Path("/usr/local/libexec/pi-agenda-display")
    if helper.is_file():
        result = subprocess.run(
            ["/usr/bin/sudo", str(helper), desired_name], check=False, timeout=20
        )
        if result.returncode != 0:
            LOG.warning("display helper failed with exit code %s", result.returncode)
            return
    set_setting(conn, "display_power_state", desired_name)
    set_setting(conn, "display_power_pending_at", "")


def _watchdog_player(conn) -> None:
    if get_setting(conn, "display_desired_state", "on") != "on":
        return
    heartbeat_text = get_setting(conn, "player_heartbeat", "")
    if not heartbeat_text:
        if time.monotonic() - WORKER_STARTED < 180:
            return
    else:
        try:
            heartbeat = datetime.fromisoformat(heartbeat_text)
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=UTC)
        except ValueError:
            return
        if datetime.now(UTC) - heartbeat < timedelta(seconds=120):
            return
    last_restart_text = get_setting(conn, "last_kiosk_restart", "")
    if last_restart_text:
        try:
            last_restart = datetime.fromisoformat(last_restart_text)
            if last_restart.tzinfo is None:
                last_restart = last_restart.replace(tzinfo=UTC)
            if datetime.now(UTC) - last_restart < timedelta(minutes=5):
                return
        except ValueError:
            pass
    helper = Path("/usr/local/libexec/pi-agenda-kiosk-control")
    if helper.is_file():
        result = subprocess.run(
            ["/usr/bin/sudo", str(helper), "restart"], check=False, timeout=20
        )
        if result.returncode == 0:
            set_setting(conn, "last_kiosk_restart", utcnow())


def run_worker(config: RuntimeConfig) -> None:
    config.ensure_directories()
    migrate(config.db_path)
    conn = connect(config.db_path)
    recover_interrupted_jobs(conn)
    # This value describes real hardware and cannot be trusted across an X
    # session restart or reboot. Reapply and verify the desired state.
    set_setting(conn, "display_power_state", "unknown")
    set_setting(conn, "display_power_pending_at", "")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    last_periodic = 0.0
    last_health = 0.0
    last_cleanup = 0.0
    last_power = 0.0
    try:
        while not STOP:
            now = time.monotonic()
            if now - last_periodic >= 30:
                _enqueue_periodic(conn)
                last_periodic = now
            if now - last_health >= 60:
                _probe_internet(conn)
                last_health = now
            if now - last_power >= 2:
                _apply_power_state(conn)
                _watchdog_player(conn)
                last_power = now
            if now - last_cleanup >= 3600:
                cleanup_retired(conn, config.data_dir)
                last_cleanup = now
            job = claim_job(conn)
            if not job:
                time.sleep(1)
                continue
            try:
                if job["kind"] in {"convert", "refresh", "rerender"}:
                    process_item_job(config, conn, job)
                elif job["kind"] == "cleanup":
                    cleanup_retired(conn, config.data_dir)
                else:
                    raise PipelineError(f"Unknown job type: {job['kind']}")
                finish_job(conn, job["id"])
            except Exception as exc:
                LOG.exception("job %s failed", job["id"])
                fail_job(conn, job, str(exc))
    finally:
        conn.close()

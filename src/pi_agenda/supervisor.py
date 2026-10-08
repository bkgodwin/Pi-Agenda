from __future__ import annotations

import logging
import signal
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import RuntimeConfig
from .db import connect, get_setting, migrate, set_setting, utcnow
from .schedules import display_should_be_on

LOG = logging.getLogger("pi_agenda.supervisor")
STOP = False
SUPERVISOR_STARTED = time.monotonic()
POWER_REASSERT_SECONDS = 300


def _stop(_signum, _frame) -> None:
    global STOP
    STOP = True


def _apply_power_state(conn) -> None:
    timezone_name = get_setting(conn, "timezone", "America/Chicago")
    desired = display_should_be_on(conn, datetime.now(UTC), timezone_name)
    current = get_setting(conn, "display_power_state", "unknown")
    desired_name = "on" if desired else "off"
    set_setting(conn, "display_desired_state", desired_name)
    now_utc = datetime.now(UTC)
    should_reassert_on = False
    if desired_name == "on" and current == "on":
        last_applied_text = get_setting(conn, "display_power_applied_at", "")
        try:
            last_applied = datetime.fromisoformat(last_applied_text)
            if last_applied.tzinfo is None:
                last_applied = last_applied.replace(tzinfo=UTC)
            should_reassert_on = should_reassert_on or (
                now_utc - last_applied > timedelta(seconds=POWER_REASSERT_SECONDS)
            )
        except ValueError:
            should_reassert_on = True
    if current == desired_name and not should_reassert_on:
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
            if now_utc - pending_at < timedelta(seconds=20):
                return
        except ValueError:
            set_setting(conn, "display_power_pending_at", utcnow())
            return
    helper = Path("/usr/local/libexec/pi-agenda-display")
    if helper.is_file():
        try:
            result = subprocess.run(
                ["/usr/bin/sudo", str(helper), desired_name], check=False, timeout=20
            )
        except (OSError, subprocess.TimeoutExpired):
            LOG.exception("Display helper failed; will retry")
            return
        if result.returncode != 0:
            LOG.warning("display helper failed with exit code %s", result.returncode)
            return
    set_setting(conn, "display_power_state", desired_name)
    set_setting(conn, "display_power_applied_at", utcnow())
    set_setting(conn, "display_power_pending_at", "")


def _watchdog_player(conn) -> None:
    if get_setting(conn, "display_desired_state", "on") != "on":
        set_setting(conn, "player_unhealthy_since", "")
        return
    if time.monotonic() - SUPERVISOR_STARTED < 180:
        return
    now = datetime.now(UTC)
    unhealthy = get_setting(conn, "player_visual_state", "") in {"black", "error"}
    unhealthy_since = get_setting(conn, "player_unhealthy_since", "")
    if unhealthy and not unhealthy_since:
        unhealthy_since = utcnow()
        set_setting(conn, "player_unhealthy_since", unhealthy_since)
    elif not unhealthy:
        set_setting(conn, "player_unhealthy_since", "")
    unhealthy_too_long = False
    if unhealthy_since and unhealthy:
        try:
            unhealthy_too_long = now - datetime.fromisoformat(
                unhealthy_since
            ) >= timedelta(seconds=120)
        except (ValueError, TypeError):
            set_setting(conn, "player_unhealthy_since", utcnow())
    heartbeat_text = get_setting(conn, "player_heartbeat", "")
    if heartbeat_text:
        try:
            heartbeat = datetime.fromisoformat(heartbeat_text)
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=UTC)
        except ValueError:
            heartbeat = now - timedelta(seconds=120)
        if now - heartbeat < timedelta(seconds=120) and not unhealthy_too_long:
            return
    last_restart_text = get_setting(conn, "last_kiosk_restart_attempt", "")
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
        LOG.warning(
            "Restarting kiosk: missing heartbeat or persistent unhealthy display"
        )
        set_setting(conn, "last_kiosk_restart_attempt", utcnow())
        try:
            result = subprocess.run(
                ["/usr/bin/sudo", str(helper), "restart"], check=False, timeout=20
            )
        except (OSError, subprocess.TimeoutExpired):
            LOG.exception("Kiosk restart failed")
            return
        if result.returncode == 0:
            set_setting(conn, "last_kiosk_restart", utcnow())
            set_setting(conn, "player_unhealthy_since", "")
        else:
            LOG.error(
                "Kiosk restart helper failed with exit code %s", result.returncode
            )


def run_supervisor(config: RuntimeConfig) -> None:
    """Keep power scheduling and crash recovery independent of conversion jobs."""
    config.ensure_directories()
    migrate(config.db_path)
    conn = connect(config.db_path)
    set_setting(conn, "display_power_state", "unknown")
    set_setting(conn, "display_power_pending_at", "")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        while not STOP:
            try:
                _apply_power_state(conn)
                _watchdog_player(conn)
            except Exception:
                LOG.exception("Display supervision failed; retrying")
            time.sleep(2)
    finally:
        conn.close()

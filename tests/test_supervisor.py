from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from pi_agenda import supervisor
from pi_agenda.db import get_setting, set_setting, utcnow


@pytest.fixture
def helpers(monkeypatch, db):
    calls = []
    set_setting(db, "display_desired_state", "on")
    monkeypatch.setattr(supervisor, "SUPERVISOR_STARTED", time.monotonic() - 300)
    monkeypatch.setattr(supervisor.Path, "is_file", lambda _path: True)

    def run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(supervisor.subprocess, "run", run)
    return calls


def ago(seconds):
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def test_stale_heartbeat_restarts_once_with_cooldown(db, helpers):
    set_setting(db, "player_heartbeat", ago(180))
    supervisor._watchdog_player(db)
    supervisor._watchdog_player(db)
    assert len(helpers) == 1
    assert helpers[0][-1] == "restart"
    assert get_setting(db, "last_kiosk_restart")


@pytest.mark.parametrize("heartbeat", ["", "invalid"])
def test_missing_or_invalid_heartbeat_recovers(db, helpers, heartbeat):
    set_setting(db, "player_heartbeat", heartbeat)
    supervisor._watchdog_player(db)
    assert len(helpers) == 1


@pytest.mark.parametrize("state", ["playing", "transition", "standby"])
def test_healthy_player_is_not_restarted(db, helpers, state):
    set_setting(db, "player_heartbeat", utcnow())
    set_setting(db, "player_visual_state", state)
    supervisor._watchdog_player(db)
    assert helpers == []


@pytest.mark.parametrize("state", ["black", "error"])
def test_recent_heartbeat_does_not_mask_stuck_black_or_error_state(db, helpers, state):
    set_setting(db, "player_heartbeat", utcnow())
    set_setting(db, "player_visual_state", state)
    supervisor._watchdog_player(db)
    assert helpers == []  # Allow normal wake-up/recovery time.
    set_setting(db, "player_unhealthy_since", ago(121))
    supervisor._watchdog_player(db)
    assert len(helpers) == 1


def test_scheduled_off_and_boot_grace_do_not_trigger_restarts(db, helpers, monkeypatch):
    set_setting(db, "player_heartbeat", ago(180))
    set_setting(db, "display_desired_state", "off")
    supervisor._watchdog_player(db)
    assert helpers == []
    set_setting(db, "display_desired_state", "on")
    monkeypatch.setattr(supervisor, "SUPERVISOR_STARTED", time.monotonic())
    supervisor._watchdog_player(db)
    assert helpers == []


def test_failed_restart_is_rate_limited_and_does_not_crash(db, helpers, monkeypatch):
    def fail(*_args, **_kwargs):
        helpers.append("attempt")
        raise subprocess.TimeoutExpired("restart", 20)

    monkeypatch.setattr(supervisor.subprocess, "run", fail)
    supervisor._watchdog_player(db)
    supervisor._watchdog_player(db)
    assert helpers == ["attempt"]
    assert not get_setting(db, "last_kiosk_restart")


def test_wake_applies_power_once_even_with_old_black_heartbeat(
    db, helpers, monkeypatch
):
    monkeypatch.setattr(supervisor, "display_should_be_on", lambda *_args: True)
    set_setting(db, "display_power_state", "off")
    set_setting(db, "player_visual_state", "black")
    for _ in range(100):
        supervisor._apply_power_state(db)
    assert len(helpers) == 1
    assert helpers[0][-1] == "on"
    set_setting(db, "display_power_applied_at", ago(301))
    supervisor._apply_power_state(db)
    assert len(helpers) == 2


def test_power_timeout_preserves_actual_state_and_retries(db, helpers, monkeypatch):
    monkeypatch.setattr(supervisor, "display_should_be_on", lambda *_args: True)
    set_setting(db, "display_power_state", "off")

    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("display", 20)

    monkeypatch.setattr(supervisor.subprocess, "run", fail)
    supervisor._apply_power_state(db)
    assert get_setting(db, "display_power_state") == "off"
    assert get_setting(db, "display_desired_state") == "on"


def test_supervisor_continues_after_a_transient_failure(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(supervisor, "STOP", False)
    monkeypatch.setattr(supervisor.signal, "signal", lambda *_args: None)

    def apply(_conn):
        calls.append("power")
        if len(calls) == 1:
            raise OSError("temporary X11 failure")

    def sleep(_seconds):
        if len(calls) >= 3:
            supervisor.STOP = True

    monkeypatch.setattr(supervisor, "_apply_power_state", apply)
    monkeypatch.setattr(
        supervisor, "_watchdog_player", lambda _conn: calls.append("watchdog")
    )
    monkeypatch.setattr(supervisor.time, "sleep", sleep)
    supervisor.run_supervisor(runtime)
    assert calls == ["power", "power", "watchdog"]

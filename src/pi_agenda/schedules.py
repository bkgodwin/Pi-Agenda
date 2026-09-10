from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .db import get_setting, set_setting


def parse_hhmm(value: str | None) -> time | None:
    if value is None or value == "":
        return None
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value)
    if not match:
        raise ValueError("Time must use 24-hour HH:MM format")
    try:
        return time(int(match.group(1)), int(match.group(2)))
    except ValueError as exc:
        raise ValueError("Time must use 24-hour HH:MM format") from exc


def validate_window(start: str | None, end: str | None) -> None:
    start_time = parse_hhmm(start)
    end_time = parse_hhmm(end)
    if (start_time is None) != (end_time is None):
        raise ValueError("Start and end time must both be set or both be empty")
    if start_time is not None and start_time == end_time:
        raise ValueError("Start and end time cannot be equal")


def item_is_active(item: dict, now: datetime) -> bool:
    if not bool(item["enabled"]):
        return False
    days_mask = int(item["days_mask"])
    start = parse_hhmm(item.get("start_time"))
    end = parse_hhmm(item.get("end_time"))
    if start is None or end is None:
        return bool(days_mask & (1 << now.weekday()))

    current = now.timetz().replace(tzinfo=None)
    if start < end:
        return bool(days_mask & (1 << now.weekday())) and start <= current < end

    if current >= start:
        return bool(days_mask & (1 << now.weekday()))
    previous_weekday = (now.weekday() - 1) % 7
    return current < end and bool(days_mask & (1 << previous_weekday))


def display_rule_is_on(rule: dict, now: datetime) -> bool:
    mode = rule["mode"]
    if mode == "always_on":
        return True
    if mode == "off":
        return False
    start = parse_hhmm(rule["on_time"])
    end = parse_hhmm(rule["off_time"])
    if start is None or end is None:
        return False
    current = now.timetz().replace(tzinfo=None)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def display_should_be_on(conn, now_utc: datetime, timezone_name: str) -> bool:
    if forced_display_off_reason(conn, now_utc):
        return False
    local_now = now_utc.astimezone(ZoneInfo(timezone_name))
    override = conn.execute(
        "SELECT state, expires_at FROM manual_display_override WHERE singleton = 1"
    ).fetchone()
    if override:
        expires = datetime.fromisoformat(override["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=ZoneInfo("UTC"))
        if expires > now_utc:
            return override["state"] == "on"
        conn.execute("DELETE FROM manual_display_override WHERE singleton = 1")

    rule = conn.execute(
        "SELECT * FROM display_schedule WHERE weekday = ?", (local_now.weekday(),)
    ).fetchone()
    if not rule:
        return False
    rule_dict = dict(rule)
    current = local_now.timetz().replace(tzinfo=None)
    if rule_dict["mode"] == "always_on":
        return True
    if rule_dict["mode"] == "window":
        start = parse_hhmm(rule_dict["on_time"])
        end = parse_hhmm(rule_dict["off_time"])
        if start and end:
            if start < end and start <= current < end:
                return True
            if start > end and current >= start:
                return True

    previous = conn.execute(
        "SELECT * FROM display_schedule WHERE weekday = ?",
        ((local_now.weekday() - 1) % 7,),
    ).fetchone()
    if previous and previous["mode"] == "window":
        previous_start = parse_hhmm(previous["on_time"])
        previous_end = parse_hhmm(previous["off_time"])
        if previous_start and previous_end and previous_start > previous_end:
            return current < previous_end
    return False


def forced_display_off_reason(conn, now_utc: datetime) -> str | None:
    if get_setting(conn, "blank_test_enabled", "0") == "1":
        return "blanking_test"
    holiday_text = get_setting(conn, "holiday_until", "")
    if holiday_text:
        try:
            holiday_until = datetime.fromisoformat(holiday_text)
            if holiday_until.tzinfo is None:
                holiday_until = holiday_until.replace(tzinfo=ZoneInfo("UTC"))
            if holiday_until > now_utc:
                return "holiday"
        except ValueError:
            pass
        set_setting(conn, "holiday_until", "")
    return None


def display_off_reason(conn, now_utc: datetime, timezone_name: str) -> str | None:
    forced = forced_display_off_reason(conn, now_utc)
    if forced:
        return forced
    return None if display_should_be_on(conn, now_utc, timezone_name) else "schedule"


def default_override_expiry(now: datetime) -> datetime:
    return now + timedelta(hours=4)

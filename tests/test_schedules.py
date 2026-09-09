from __future__ import annotations

from datetime import UTC, datetime

from pi_agenda.schedules import display_should_be_on, item_is_active, validate_window


def test_item_same_day_and_boundaries():
    item = {"enabled": 1, "days_mask": 1, "start_time": "08:00", "end_time": "09:00"}
    assert item_is_active(item, datetime(2026, 9, 7, 8, 0, tzinfo=UTC))
    assert item_is_active(item, datetime(2026, 9, 7, 8, 59, tzinfo=UTC))
    assert not item_is_active(item, datetime(2026, 9, 7, 9, 0, tzinfo=UTC))


def test_item_overnight_uses_start_weekday():
    item = {"enabled": 1, "days_mask": 1, "start_time": "22:00", "end_time": "02:00"}
    assert item_is_active(item, datetime(2026, 9, 7, 23, 0, tzinfo=UTC))
    assert item_is_active(item, datetime(2026, 9, 8, 1, 59, tzinfo=UTC))
    assert not item_is_active(item, datetime(2026, 9, 8, 2, 0, tzinfo=UTC))


def test_display_overnight_reads_previous_day(db):
    db.execute("UPDATE display_schedule SET mode='off', on_time=NULL, off_time=NULL")
    db.execute(
        "UPDATE display_schedule SET mode='window', on_time='22:00', off_time='02:00' WHERE weekday=0"
    )
    # 06:30 UTC is 01:30 Tuesday in America/Chicago during daylight time.
    now = datetime(2026, 9, 8, 6, 30, tzinfo=UTC)
    assert display_should_be_on(db, now, "America/Chicago")


def test_window_validation():
    validate_window(None, None)
    validate_window("23:00", "02:00")
    for start, end in (("08:00", None), ("08:00", "08:00"), ("bad", "09:00")):
        try:
            validate_window(start, end)
        except ValueError:
            pass
        else:
            raise AssertionError((start, end))

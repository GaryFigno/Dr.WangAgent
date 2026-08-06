"""Schedule arithmetic and job persistence."""

from __future__ import annotations

from datetime import datetime

import pytest

from aiharness.scheduler.cron import CronExpression, Schedule, ScheduleError, parse_weekday
from aiharness.scheduler.jobs import JobStore
from aiharness.ui.commands import parse_schedule

# A Wednesday, so weekday arithmetic is unambiguous.
WEDNESDAY = datetime(2026, 8, 5, 10, 0)


def test_daily_schedule_finds_the_next_slot_today():
    schedule = Schedule.daily("09:00", "18:00")
    assert schedule.next_after(WEDNESDAY) == datetime(2026, 8, 5, 18, 0)


def test_daily_schedule_rolls_over_to_tomorrow():
    schedule = Schedule.daily("09:00")
    assert schedule.next_after(WEDNESDAY) == datetime(2026, 8, 6, 9, 0)


def test_weekly_schedule_picks_the_next_matching_weekday():
    schedule = Schedule.weekly(["mon", "thu"], "09:30")
    assert schedule.next_after(WEDNESDAY) == datetime(2026, 8, 6, 9, 30)  # Thursday


def test_weekly_schedule_wraps_to_next_week():
    schedule = Schedule.weekly(["mon"], "09:30")
    assert schedule.next_after(WEDNESDAY) == datetime(2026, 8, 10, 9, 30)


def test_chinese_weekday_names_are_understood():
    assert parse_weekday("周一") == 0
    assert parse_weekday("星期四") == 3
    schedule = Schedule.weekly(["周一", "周四"], "09:30")
    assert schedule.weekdays == [0, 3]


def test_interval_schedule_spaces_runs_from_the_last_one():
    schedule = Schedule.every(90)
    last = datetime(2026, 8, 5, 9, 0).timestamp()
    assert schedule.next_after(WEDNESDAY, last_run=last) == datetime(2026, 8, 5, 10, 30)


def test_cron_weekday_range_matches_weekdays_only():
    schedule = Schedule.from_cron("0 9 * * 1-5")
    # Friday 2026-08-07 10:00 -> next weekday 09:00 is Monday the 10th.
    friday = datetime(2026, 8, 7, 10, 0)
    assert schedule.next_after(friday) == datetime(2026, 8, 10, 9, 0)


def test_cron_step_syntax():
    expression = CronExpression.parse("*/15 * * * *")
    assert expression.minutes == {0, 15, 30, 45}


def test_cron_sunday_is_normalised():
    # Cron's 0 and 7 both mean Sunday; Python's weekday() calls it 6.
    assert CronExpression.parse("0 9 * * 0").days_of_week == {6}
    assert CronExpression.parse("0 9 * * 7").days_of_week == {6}


def test_once_schedule_stops_firing_after_it_passes():
    moment = datetime(2026, 8, 5, 12, 0)
    schedule = Schedule.once_at(moment)
    assert schedule.next_after(WEDNESDAY) == moment
    assert schedule.next_after(datetime(2026, 8, 6, 0, 0)) is None


@pytest.mark.parametrize(
    "text, kind",
    [
        ("daily 09:00", "daily"),
        ("weekly mon,thu 09:30", "weekly"),
        ("every 30m", "interval"),
        ("every 4h", "interval"),
        ("cron */5 * * * *", "cron"),
        ("once 2026-08-05 09:00", "once"),
    ],
)
def test_schedule_mini_language(text, kind):
    assert parse_schedule(text).kind == kind


def test_every_4h_is_240_minutes():
    assert parse_schedule("every 4h").interval_minutes == 240


@pytest.mark.parametrize(
    "text", ["", "weekly", "every", "every 5x", "cron 1 2 3", "once tomorrow", "nonsense"]
)
def test_bad_schedules_are_rejected(text):
    with pytest.raises(ScheduleError):
        parse_schedule(text)


def test_invalid_cron_field_is_rejected():
    with pytest.raises(ScheduleError):
        Schedule.from_cron("99 * * * *")


def test_jobs_persist_across_store_reloads(tmp_path, workspace):
    path = tmp_path / "jobs.json"
    store = JobStore(path)
    job = store.add(
        "deps", "check dependencies", Schedule.weekly(["mon"], "09:00"), workspace
    )
    assert job.next_run > 0

    reloaded = JobStore(path)
    restored = reloaded.get(job.id)
    assert restored is not None
    assert restored.name == "deps"
    assert restored.schedule.weekdays == [0]
    assert restored.prompt == "check dependencies"


def test_jobs_can_be_addressed_by_name(tmp_path, workspace):
    store = JobStore(tmp_path / "jobs.json")
    store.add("nightly", "do the thing", Schedule.daily("02:00"), workspace)
    assert store.get("nightly") is not None


def test_disabling_a_job_stops_it_being_due(tmp_path, workspace):
    import time

    store = JobStore(tmp_path / "jobs.json")
    job = store.add("x", "y", Schedule.every(1), workspace)
    job.next_run = time.time() - 1
    assert job.is_due(time.time(), grace=300) is True

    store.set_enabled(job.id, False)
    assert job.is_due(time.time(), grace=300) is False


def test_overdue_jobs_are_detected_outside_the_grace_period(tmp_path, workspace):
    import time

    store = JobStore(tmp_path / "jobs.json")
    job = store.add("x", "y", Schedule.daily("09:00"), workspace)
    job.next_run = time.time() - 1000
    assert job.is_due(time.time(), grace=300) is False
    assert job.is_overdue(time.time(), grace=300) is True


def test_describe_renders_both_languages():
    schedule = Schedule.weekly(["mon", "thu"], "09:30")
    assert "Mon" in schedule.describe()
    assert "周一" in schedule.describe(chinese=True)

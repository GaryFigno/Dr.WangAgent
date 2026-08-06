"""Schedule expressions.

Two ways to say when something runs:

* a friendly spec — "every Monday and Thursday at 09:30", "every 4 hours" —
  which is what the UI builds;
* a standard five-field cron expression, for people who already think in cron.

Both compile to the same matcher, so :meth:`Schedule.next_after` is the only
thing the runner needs to know about.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

#: Longest horizon searched when looking for the next matching minute.
MAX_LOOKAHEAD_DAYS = 400
MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 24
DAYS_PER_WEEK = 7
#: Cron accepts 7 as an alias for Sunday alongside 0.
CRON_SUNDAY_ALIAS = 7
#: A standard cron expression has five whitespace-separated fields.
CRON_FIELD_COUNT = 5
#: Longest possible day-of-month field, used to detect an unrestricted one.
DAYS_IN_LONGEST_MONTH = 31
#: Minute and hour bounds for cron fields.
MINUTE_MAX = 59
HOUR_MAX = 23
MONTH_MIN = 1
MONTH_MAX = 12
DAY_MIN = 1

WEEKDAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

#: Chinese weekday names, since the UI is bilingual.
WEEKDAY_NAMES_ZH = {
    "周一": 0, "星期一": 0, "礼拜一": 0,
    "周二": 1, "星期二": 1, "礼拜二": 1,
    "周三": 2, "星期三": 2, "礼拜三": 2,
    "周四": 3, "星期四": 3, "礼拜四": 3,
    "周五": 4, "星期五": 4, "礼拜五": 4,
    "周六": 5, "星期六": 5, "礼拜六": 5,
    "周日": 6, "星期日": 6, "周天": 6, "星期天": 6,
}

WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_LABELS_ZH = ["一", "二", "三", "四", "五", "六", "日"]

TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


class ScheduleError(ValueError):
    """Raised when a schedule cannot be understood."""


def parse_weekday(token: str) -> int:
    """Convert a weekday name or number to 0=Monday..6=Sunday."""
    key = token.strip().lower()
    if key in WEEKDAY_NAMES:
        return WEEKDAY_NAMES[key]
    if token.strip() in WEEKDAY_NAMES_ZH:
        return WEEKDAY_NAMES_ZH[token.strip()]
    if key.isdigit():
        value = int(key)
        if 0 <= value < DAYS_PER_WEEK:
            return value
        if value == CRON_SUNDAY_ALIAS:
            return DAYS_PER_WEEK - 1
    raise ScheduleError(f"unrecognised weekday: {token!r}")


def parse_time_of_day(token: str) -> tuple[int, int]:
    """Parse ``HH:MM`` into an (hour, minute) pair."""
    match = TIME_RE.match(token.strip())
    if not match:
        raise ScheduleError(f"time must look like HH:MM, got {token!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 0 <= hour < HOURS_PER_DAY or not 0 <= minute < MINUTES_PER_HOUR:
        raise ScheduleError(f"time out of range: {token!r}")
    return hour, minute


# --------------------------------------------------------------------------
# cron field parsing
# --------------------------------------------------------------------------


def _expand_field(field_text: str, low: int, high: int) -> set[int]:
    """Expand one cron field into the set of values it matches."""
    values: set[int] = set()
    for raw_part in field_text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(f"bad step in cron field: {field_text!r}")
            step = int(step_text)
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(part)
        if start < low or end > high or start > end:
            raise ScheduleError(f"cron field out of range: {field_text!r}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ScheduleError(f"cron field matched nothing: {field_text!r}")
    return values


@dataclass
class CronExpression:
    """A parsed five-field cron expression: minute hour dom month dow."""

    minutes: set[int]
    hours: set[int]
    days_of_month: set[int]
    months: set[int]
    days_of_week: set[int]
    source: str = ""

    @classmethod
    def parse(cls, text: str) -> CronExpression:
        fields = text.split()
        if len(fields) != CRON_FIELD_COUNT:
            raise ScheduleError(
                f"cron needs 5 fields (minute hour day month weekday), got {len(fields)}"
            )
        minute, hour, dom, month, dow = fields
        # Cron numbers Sunday as 0 or 7; we normalise to Python's Monday=0.
        weekdays = {
            (value - 1) % DAYS_PER_WEEK
            for value in _expand_field(dow, 0, CRON_SUNDAY_ALIAS)
        }
        return cls(
            minutes=_expand_field(minute, 0, MINUTE_MAX),
            hours=_expand_field(hour, 0, HOUR_MAX),
            days_of_month=_expand_field(dom, DAY_MIN, DAYS_IN_LONGEST_MONTH),
            months=_expand_field(month, MONTH_MIN, MONTH_MAX),
            days_of_week=weekdays,
            source=text,
        )

    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        # Standard cron semantics: when both day fields are restricted, either
        # may match; when only one is, it must match.
        dom_restricted = len(self.days_of_month) < DAYS_IN_LONGEST_MONTH
        dow_restricted = len(self.days_of_week) < DAYS_PER_WEEK
        dom_hit = moment.day in self.days_of_month
        dow_hit = moment.weekday() in self.days_of_week
        if dom_restricted and dow_restricted:
            return dom_hit or dow_hit
        if dom_restricted:
            return dom_hit
        if dow_restricted:
            return dow_hit
        return True


# --------------------------------------------------------------------------
# the schedule
# --------------------------------------------------------------------------


@dataclass
class Schedule:
    """When a job should run.

    Exactly one style is active, chosen by :attr:`kind`:

    ``daily``
      Run at each time in :attr:`times`, every day.
    ``weekly``
      Run at each time in :attr:`times`, on each day in :attr:`weekdays`.
    ``interval``
      Run every :attr:`interval_minutes` minutes.
    ``cron``
      Run when :attr:`cron` matches.
    ``once``
      Run a single time, at :attr:`at`.
    """

    kind: str = "daily"
    times: list[str] = field(default_factory=lambda: ["09:00"])
    weekdays: list[int] = field(default_factory=list)
    interval_minutes: int = 0
    cron: str = ""
    at: float = 0.0  # epoch seconds, for kind="once"

    # -- construction -----------------------------------------------------

    @classmethod
    def daily(cls, *times: str) -> Schedule:
        return cls(kind="daily", times=list(times) or ["09:00"])

    @classmethod
    def weekly(cls, weekdays: Iterable[str | int], *times: str) -> Schedule:
        days = sorted({parse_weekday(str(day)) for day in weekdays})
        if not days:
            raise ScheduleError("weekly schedule needs at least one weekday")
        return cls(kind="weekly", weekdays=days, times=list(times) or ["09:00"])

    @classmethod
    def every(cls, minutes: int) -> Schedule:
        if minutes < 1:
            raise ScheduleError("interval must be at least one minute")
        return cls(kind="interval", interval_minutes=minutes)

    @classmethod
    def from_cron(cls, expression: str) -> Schedule:
        CronExpression.parse(expression)  # validate eagerly
        return cls(kind="cron", cron=expression)

    @classmethod
    def once_at(cls, moment: datetime) -> Schedule:
        return cls(kind="once", at=moment.timestamp())

    def validate(self) -> None:
        """Raise :class:`ScheduleError` if the schedule is unusable."""
        if self.kind in ("daily", "weekly"):
            if not self.times:
                raise ScheduleError(f"{self.kind} schedule needs at least one time")
            for value in self.times:
                parse_time_of_day(value)
            if self.kind == "weekly" and not self.weekdays:
                raise ScheduleError("weekly schedule needs at least one weekday")
        elif self.kind == "interval":
            if self.interval_minutes < 1:
                raise ScheduleError("interval must be at least one minute")
        elif self.kind == "cron":
            CronExpression.parse(self.cron)
        elif self.kind == "once":
            if self.at <= 0:
                raise ScheduleError("one-off schedule needs a timestamp")
        else:
            raise ScheduleError(f"unknown schedule kind: {self.kind!r}")

    # -- evaluation -------------------------------------------------------

    def next_after(self, after: datetime, *, last_run: float = 0.0) -> datetime | None:
        """Return the next moment this schedule fires strictly after ``after``.

        Args:
          after: The reference time.
          last_run: Epoch seconds of the previous run, used by interval
            schedules to space runs from the last execution.

        Returns:
          The next firing time, or ``None`` when the schedule can never fire
          again (a one-off that already passed).
        """
        self.validate()
        if self.kind == "once":
            moment = datetime.fromtimestamp(self.at)
            return moment if moment > after else None
        if self.kind == "interval":
            anchor = datetime.fromtimestamp(last_run) if last_run else after
            candidate = anchor + timedelta(minutes=self.interval_minutes)
            return candidate if candidate > after else after + timedelta(minutes=1)
        if self.kind == "cron":
            return self._next_cron(after)
        return self._next_clock(after)

    def _next_cron(self, after: datetime) -> datetime | None:
        expression = CronExpression.parse(self.cron)
        cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        horizon = after + timedelta(days=MAX_LOOKAHEAD_DAYS)
        while cursor <= horizon:
            if expression.matches(cursor):
                return cursor
            cursor += timedelta(minutes=1)
        return None

    def _next_clock(self, after: datetime) -> datetime | None:
        """Next firing for daily/weekly schedules."""
        wanted_days = set(self.weekdays) if self.kind == "weekly" else set(range(DAYS_PER_WEEK))
        slots = sorted(parse_time_of_day(value) for value in self.times)
        day = after.date()
        for offset in range(MAX_LOOKAHEAD_DAYS):
            current = day + timedelta(days=offset)
            if current.weekday() not in wanted_days:
                continue
            for hour, minute in slots:
                candidate = datetime(current.year, current.month, current.day, hour, minute)
                if candidate > after:
                    return candidate
        return None

    # -- display ----------------------------------------------------------

    def describe(self, chinese: bool = False) -> str:
        """Render the schedule as a short human-readable phrase."""
        if self.kind == "once":
            moment = datetime.fromtimestamp(self.at)
            return moment.strftime("%Y-%m-%d %H:%M")
        if self.kind == "interval":
            minutes = self.interval_minutes
            if minutes % MINUTES_PER_HOUR == 0:
                hours = minutes // MINUTES_PER_HOUR
                return f"每 {hours} 小时" if chinese else f"every {hours}h"
            return f"每 {minutes} 分钟" if chinese else f"every {minutes}m"
        if self.kind == "cron":
            return f"cron: {self.cron}"
        times = ", ".join(self.times)
        if self.kind == "daily":
            return f"每天 {times}" if chinese else f"daily at {times}"
        if chinese:
            days = "、".join("周" + WEEKDAY_LABELS_ZH[d] for d in sorted(self.weekdays))
            return f"{days} {times}"
        days = ", ".join(WEEKDAY_LABELS[d] for d in sorted(self.weekdays))
        return f"{days} at {times}"

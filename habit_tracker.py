#!/usr/bin/env python3
"""Todoist habit tracker.

For every recurring task carrying the tracking label (default: "track"), keep a
single comment on the task with habit stats: when tracking started, the
completion rate, the current and best streak, and the last 7 results. Tasks
with "test" in their name get a detailed comment instead: the full split of
the completion rate, the last 10 results, and the time of the update.

Every occurrence of the task gets one result, recorded on the day it was
scheduled for:
  completed - done on that day, or earlier.
  late      - done after that day, but before the next occurrence's day.
  failed    - the next occurrence's day arrived and it still wasn't done.
Postponing a task doesn't change the day it was scheduled for. Moving it back
to the day it was last completed, or earlier (which is also what undoing a
completion in Todoist does), takes that completion back. Occurrence days come
from the task's repeat text ("every monday"); if the script can't read it, an
occurrence fails the day after it was due and there is no late.

Todoist doesn't record which occurrence is still open, so the per-day results,
the open occurrence, the time of the last check and the day of the last
completion are stored in the stats comment itself: in the link behind the 📊,
or on the last line of a detailed comment. Run the script at least once a day.

Usage:
  TODOIST_API_TOKEN=... python3 habit_tracker.py [--dry-run] [--label track]
                                                 [--task ID_OR_LINK] [--rebuild]
  TODOIST_API_TOKEN_LU=... python3 habit_tracker.py --account lu [...]
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

API_URL = "https://api.todoist.com/api/v1"

DONE, LATE, FAILED, EMPTY = "+", "~", "-", "."
DATE = r"\d{4}-\d{2}-\d{2}"
MARKER_RE = re.compile(r"habit-tracker:v\d:")
# v3 had no last-completion field.
DATA_RE = re.compile(rf"habit-tracker:v[34]:({DATE}):({DATE}|-):(\d+):(?:({DATE}|-):)?([+~.\-]*)")
# Written by the first version, before late results existed.
V1_RE = re.compile(rf"habit-tracker:v1:({DATE}):({DATE}):([+.\-]*)")
UPDATED_RE = re.compile(r"_Updated (\d{1,2} \w{3} \d{4} \d{2}:\d{2})_")
# Completions this recent wait for the next run, in case the activity log
# hasn't caught up yet.
SETTLE_TIME = timedelta(minutes=5)
RECENT_RESULTS = 7
# Tasks with this in their name get the detailed comment, with the data in view.
DETAILED_NAME = "test"
DETAILED_RECENT_RESULTS = 10
# The short comment keeps its data in the link behind the 📊, which leads here.
ABOUT_URL = "https://github.com/spolenoque/todoist"
SQUARES = {DONE: "🟩", LATE: "🟨", FAILED: "🟥"}
OPEN_SQUARE = "⬜"  # the occurrence due now, not done yet

WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}
# Russian repeat text, translated into the English forms the parser reads.
RUSSIAN_PHRASES = {
    r"будний день|будни": "weekday", r"рабочий день": "workday", r"выходные": "weekend",
    r"через день": "every other day", r"ежедневно": "daily", r"еженедельно": "weekly",
    r"ежемесячно": "monthly", r"ежегодно": "yearly",
    r"(?:в|во) (?=\d)": "at ", r"(?:начиная )?с (?=\d)": "starting ", r"до (?=\d)": "until ",
}
RUSSIAN_WORDS = {
    "every": "каждый каждая каждое каждую каждые",
    "day": "день", "days": "дня дней", "week": "неделя неделю", "weeks": "недели недель",
    "month": "месяц", "months": "месяца месяцев", "year": "год", "years": "года лет",
    "morning": "утро", "evening": "вечер", "on": "в во", "and": "и",
    "mon": "понедельник пн", "tue": "вторник вт", "wed": "среда среду ср", "thu": "четверг чт",
    "fri": "пятница пятницу пт", "sat": "суббота субботу сб", "sun": "воскресенье вс",
}
RUSSIAN_WORDS = {ru: en for en, words in RUSSIAN_WORDS.items() for ru in words.split()}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1}
ALL_DAYS = frozenset(range(7))
WORKDAYS = frozenset(range(5))


class Event(NamedTuple):
    """Something done to a task, from the activity log."""

    at: datetime  # UTC
    kind: str  # "completed", "uncompleted" (undone in Todoist) or "moved" (new due date)
    occurrence: date | None = None  # completed: local day the completed occurrence was due
    due_at: datetime | None = None  # completed: the next due time; moved: the new one


class Todoist:
    def __init__(self, token: str):
        self.token = token
        self._user = None

    def _request(self, method, path, params=None, body=None, retries=3):
        url = f"{API_URL}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                retryable = e.code == 429 or e.code >= 500
                if retryable and attempt < retries - 1:
                    time.sleep(int(e.headers.get("Retry-After") or 2 ** (attempt + 1)))
                    continue
                detail = e.read().decode(errors="replace")[:300]
                raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from e

    def _paginate(self, path, params, key="results"):
        params = dict(params)
        while True:
            page = self._request("GET", path, params)
            yield from page.get(key, [])
            if not page.get("next_cursor"):
                return
            params["cursor"] = page["next_cursor"]

    def user(self) -> dict:
        if self._user is None:
            self._user = self._request("GET", "user")
        return self._user

    def user_id(self) -> str:
        return self.user()["id"]

    def user_timezone(self) -> str | None:
        return (self.user().get("tz_info") or {}).get("timezone")

    def tasks_with_label(self, label: str) -> list[dict]:
        return list(self._paginate("tasks", {"label": label, "limit": 200}))

    def task(self, task_id: str) -> dict:
        return self._request("GET", f"tasks/{task_id}")

    def events(self, task_id: str, since: date) -> list[Event]:
        """Completions, undone completions and due date changes of the task
        on or after `since`."""
        # Recurring completions are not returned by /tasks/completed/*; they
        # only show up in the activity log as item:completed events.
        # object_id only narrows the results down to the task together with
        # object_type; object_event_types would make the API ignore it.
        params = {"object_type": "item", "object_id": task_id, "date_from": since.isoformat(), "limit": 100}
        result = []
        for e in self._paginate("activities", params):
            if e.get("object_id") != task_id:
                continue
            extra = e.get("extra_data") or {}
            due_at = parse_utc(extra["due_date"]) if extra.get("due_date") else None
            at = parse_utc(e["event_date"])
            if e.get("event_type") == "completed":
                occurrence = extra.get("completed_due_date_local")
                result.append(Event(at, "completed", date.fromisoformat(occurrence[:10]) if occurrence else None, due_at))
            elif e.get("event_type") == "uncompleted":
                result.append(Event(at, "uncompleted"))
            elif e.get("event_type") == "updated" and due_at:  # other edits carry no due date
                result.append(Event(at, "moved", due_at=due_at))
        return result

    def comments(self, task_id: str) -> list[dict]:
        return list(self._paginate("comments", {"task_id": task_id, "limit": 200}))

    def add_comment(self, task_id: str, content: str):
        self._request("POST", "comments", body={"task_id": task_id, "content": content})

    def update_comment(self, comment_id: str, content: str):
        self._request("POST", f"comments/{comment_id}", body={"content": content})


def parse_utc(value: str) -> datetime:
    # Python 3.9's fromisoformat() does not accept a trailing "Z".
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def due_day(due: dict, tz: tzinfo) -> date:
    """Local day of `due["date"]`: a full-day date ("2026-10-16"), a floating
    local datetime ("2026-10-16T09:00:00") or a UTC datetime ending in "Z".
    """
    raw = due["date"]
    if raw.endswith("Z"):
        return parse_utc(raw).astimezone(tz).date()
    return date.fromisoformat(raw[:10])


def local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def add_months(day: date, months: int) -> date:
    year, month = divmod(day.month - 1 + months, 12)
    year, month = day.year + year, month + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def nth_day(year: int, month: int, ordinal: int, weekdays: frozenset, only_month: int) -> date | None:
    """The `ordinal`-th day (-1: last) of the month that falls on `weekdays`."""
    if only_month and only_month != month:
        return None
    last = calendar.monthrange(year, month)[1]
    days = [d for d in (date(year, month, n) for n in range(1, last + 1)) if d.weekday() in weekdays]
    if ordinal == -1 or (ordinal > len(days) and weekdays == ALL_DAYS):
        return days[-1]  # "every 31st" falls on the last day of shorter months
    return days[ordinal - 1] if ordinal <= len(days) else None


@dataclass(frozen=True)
class Schedule:
    """When a recurring task falls due.

    Unit "day", "week", "month" or "year": every `every` units after the last
    occurrence; "week" with `weekdays` only on those days. Unit "nth": on the
    days given by `rules`, each (ordinal, weekdays, month or 0), such as the
    2nd Monday of every month (2, {mon}, 0) or the last Sunday of February
    (-1, {sun}, 2). A plain day of the month uses all weekdays.
    """

    unit: str
    every: int = 1
    weekdays: frozenset = frozenset()
    rules: tuple = ()

    @property
    def on_calendar(self) -> bool:
        """Falls on set calendar days rather than counting from the last
        occurrence, so postponing the task doesn't shift later occurrences."""
        return self.unit == "nth" or bool(self.weekdays) or (self.unit == "day" and self.every == 1)

    def after(self, day: date) -> date | None:
        """The first occurrence after `day`."""
        if self.unit == "day":
            return day + timedelta(days=self.every)
        if self.unit == "month":
            return add_months(day, self.every)
        if self.unit == "year":
            return add_months(day, 12 * self.every)
        if self.unit == "week" and not self.weekdays:
            return day + timedelta(weeks=self.every)
        if self.unit == "week":
            first_monday = day - timedelta(days=day.weekday())
            nxt = day + timedelta(days=1)
            while nxt.weekday() not in self.weekdays or (nxt - first_monday).days // 7 % self.every:
                nxt += timedelta(days=1)
            return nxt
        for k in range(25):  # "nth": the first month, within two years, with a match
            year, month = divmod(day.month - 1 + k, 12)
            found = []
            for rule in self.rules:
                d = nth_day(day.year + year, month + 1, *rule)
                if d and d > day:
                    found.append(d)
            if found:
                return min(found)
        return None


def scheduled_occurrence(
    schedule: Schedule | None, day: date, next_due: date | None = None, earliest: date | None = None
) -> date:
    """The scheduled day an occurrence Todoist has on `day` stands for. A task
    on set calendar days may have been moved off its day: then it's the
    scheduled day before or after `day` that Todoist's `next_due` follows, or
    the one after if that's unknown. Nothing before `earliest` (when the task
    was created) counts."""
    if not (schedule and schedule.on_calendar):
        return day
    following = schedule.after(day - timedelta(days=1))
    if following == day:
        return day
    if next_due:
        previous = next((d for d in (day - timedelta(days=k) for k in range(1, 367)) if schedule.after(d - timedelta(days=1)) == d), None)
        if previous and earliest and previous < earliest:
            previous = None
        for candidate in (previous, following):
            if candidate and schedule.after(candidate) == next_due:
                return candidate
    return following or day


def parse_weekdays(text: str) -> frozenset | None:
    words = [w for w in re.findall(r"\w+", text) if w != "and"]
    if words and all(w in WEEKDAYS for w in words):
        return frozenset(WEEKDAYS[w] for w in words)
    return None


def parse_rule(text: str) -> tuple | None:
    """'2nd monday', 'last sun feb', '15th', 'jan 1', 'last workday' as an
    (ordinal, weekdays, month or 0) rule."""
    month, words = 0, []
    for word in text.split():
        if word in MONTHS:
            if month:
                return None
            month = MONTHS[word]
        elif word not in ("the", "of", "month"):
            words.append(word)
    if not 1 <= len(words) <= 2:
        return None
    number = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", words[0])
    ordinal = ORDINALS.get(words[0]) or (int(number[1]) if number else 0)
    kind = words[1] if len(words) == 2 else "day"
    if kind == "day":
        weekdays = ALL_DAYS
    elif kind in ("workday", "weekday"):
        weekdays = WORKDAYS
    elif kind in WEEKDAYS:
        weekdays = frozenset({WEEKDAYS[kind]})
    else:
        return None
    if not ordinal or ordinal > (31 if weekdays == ALL_DAYS else 5):
        return None
    return (ordinal, weekdays, month)


def parse_schedule(text: str) -> Schedule | None:
    """Read the common English forms of Todoist's repeat text, e.g.
    "every day at 8am", "every! 3 days", "every other monday",
    "every mon, wed, fri", "every 2 weeks on tue", "every 15th",
    "every 2nd monday", "every last sun feb, last sun aug", "every jan 1".
    The simple Russian forms work too: "каждый день", "каждые 2 недели во вт".
    """
    s = text.lower().strip()
    if re.search(r"[а-яё]", s):
        for phrase, english in RUSSIAN_PHRASES.items():
            s = re.sub(rf"\b{phrase}", english, s)
        s = re.sub(r"[а-яё]+", lambda m: RUSSIAN_WORDS.get(m[0], m[0]), s)
    s = re.sub(r"^ev(?:ery)?!?(?=\s)", "every", s)
    s = re.split(r"\s+(?:at|starting|from|until|ending|for)\s", s)[0]
    s = re.sub(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Hourly repeats ("every 12 hours") are left unread: several occurrences a
    # day don't fit one result per day.
    if re.fullmatch(r"daily|everyday|every (?:day|morning|afternoon|evening|night)", s):
        return Schedule("day")
    if s in ("every weekday", "every workday"):
        return Schedule("week", weekdays=WORKDAYS)
    if s == "every weekend":
        return Schedule("week", weekdays=frozenset({5, 6}))
    adverbs = {"weekly": "week", "monthly": "month", "yearly": "year", "annually": "year"}
    if s in adverbs:
        return Schedule(adverbs[s])
    m = re.fullmatch(r"every (.+)", s)
    if not m:
        return None
    rules = [parse_rule(part) for part in re.split(r"\s*,\s*|\s*&\s*|\s+and\s+", m[1])]
    if all(rules):
        return Schedule("nth", rules=tuple(rules))

    m = re.fullmatch(r"every (other |\d+ )?(.+)", s)
    every = 2 if m[1] == "other " else int(m[1]) if m[1] else 1
    rest = m[2]
    unit = re.fullmatch(r"(day|week|month|year)s?(?: on (.+))?", rest)
    if unit:
        weekdays = parse_weekdays(unit[2] or "") if unit[1] == "week" else None
        return Schedule(unit[1], every, weekdays or frozenset())
    weekdays = parse_weekdays(rest)
    if weekdays:
        return Schedule("week", every, weekdays)
    return None


@dataclass
class Ledger:
    since: date  # first tracked day
    pending: date | None  # day the oldest open occurrence was scheduled for
    checked: datetime  # completions up to this moment have been counted
    log: str  # one result per day from `since`; may run into the future
    last_done: date | None = None  # day the last counted completion happened

    @classmethod
    def parse(cls, text: str, tz: tzinfo) -> Ledger | None:
        if m := DATA_RE.search(text):
            pending = date.fromisoformat(m[2]) if m[2] != "-" else None
            last_done = date.fromisoformat(m[4]) if m[4] and m[4] != "-" else None
            checked = datetime.fromtimestamp(int(m[3]), timezone.utc)
            return cls(date.fromisoformat(m[1]), pending, checked, m[5], last_done)
        if m := V1_RE.search(text):
            updated = UPDATED_RE.search(text)
            if updated:
                checked = datetime.strptime(updated[1], "%d %b %Y %H:%M").replace(tzinfo=tz)
            else:
                checked = local_midnight(date.fromisoformat(m[2]), tz)
            return cls(date.fromisoformat(m[1]), None, checked, m[3])
        return None

    def serialize(self) -> str:
        checked = int(self.checked.timestamp())
        last_done = self.last_done or "-"
        return f"habit-tracker:v4:{self.since}:{self.pending or '-'}:{checked}:{last_done}:{self.log}"

    def extend_to(self, day: date):
        self.log = self.log.ljust((day - self.since).days + 1, EMPTY)

    def result(self, day: date) -> str:
        i = (day - self.since).days
        return self.log[i] if 0 <= i < len(self.log) else EMPTY

    def is_due_now(self, today: date) -> bool:
        """The open occurrence's day has arrived and it has no result yet."""
        if self.pending is None or self.pending > today:
            return False
        i = (self.pending - self.since).days
        return i < 0 or self.log[i : i + 1] in ("", EMPTY)

    def mark(self, day: date, result: str):
        """Record a result for a day unless it already has one."""
        i = (day - self.since).days
        if i < 0:
            return
        self.extend_to(day)
        if self.log[i] == EMPTY:
            self.log = self.log[:i] + result + self.log[i + 1 :]

    def fail_missed(self, schedule: Schedule | None, through: date):
        """The open occurrence fails once the next one's day has arrived."""
        while self.pending is not None:
            nxt = schedule.after(self.pending) if schedule else None
            if (nxt or self.pending + timedelta(days=1)) > through:
                return
            self.mark(self.pending, FAILED)
            self.pending = nxt  # unknown schedule: wait for Todoist's next due date

    def complete(
        self, schedule: Schedule | None, done_on: date, next_due: date | None, occurrence: date | None = None
    ):
        """Settle the open occurrence: done on or before its day is completed,
        after it is late, and once the next one's day arrived it has failed.
        `occurrence` is the day Todoist had the task on."""
        self.fail_missed(schedule, done_on)
        if occurrence and self.pending and done_on < self.pending and self.result(occurrence) in (DONE, LATE):
            # Ticked again before the next occurrence is due: Todoist brings a
            # task moved ahead back to its scheduled day once it's done there.
            return
        if self.pending is not None:
            self.mark(self.pending, DONE if done_on <= self.pending else LATE)
            self.last_done = done_on
            if schedule and schedule.on_calendar:
                # Todoist's next date skips an occurrence when the task was
                # postponed past it before being done; the calendar doesn't.
                next_due = schedule.after(self.pending) or next_due
            else:
                next_due = next_due or (schedule.after(self.pending) if schedule else None)
        self.pending = next_due

    def take_back(self, moved_to: date | None = None):
        """Undoing the last completion in Todoist (`moved_to` None), or moving
        the task back to the day of that completion or earlier, takes it back:
        its occurrence is open again. Moving a later occurrence earlier, to a
        day after the last completion, doesn't."""
        if self.pending is None or self.last_done is None:
            return
        if moved_to is not None and (moved_to > self.last_done or moved_to >= self.pending):
            return
        results = self.log.rstrip(EMPTY)
        if results and results[-1] in (DONE, LATE):  # nothing settled since
            day = self.since + timedelta(days=len(results) - 1)
            self.log = results[:-1] + EMPTY + self.log[len(results) :]
            self.pending, self.last_done = day, None


@dataclass
class Stats:
    completed: int
    late: int
    failed: int
    streak: int
    streak_kind: str | None  # DONE (on time or late), FAILED, or None without results
    best_streak: int

    @property
    def rate(self) -> int | None:
        total = self.completed + self.late + self.failed
        return round(100 * (self.completed + self.late) / total) if total else None


def compute_stats(ledger: Ledger) -> Stats:
    results = ledger.log.replace(EMPTY, "")
    done_or_failed = results.replace(LATE, DONE)
    streak_kind = done_or_failed[-1] if done_or_failed else None
    streak = len(done_or_failed) - len(done_or_failed.rstrip(streak_kind)) if streak_kind else 0
    best = max((len(run) for run in re.findall(r"\++", done_or_failed)), default=0)
    return Stats(
        completed=results.count(DONE),
        late=results.count(LATE),
        failed=results.count(FAILED),
        streak=streak,
        streak_kind=streak_kind,
        best_streak=best,
    )


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def render(ledger: Ledger, now: datetime, detailed: bool = False) -> str:
    s = compute_stats(ledger)
    if s.rate is None:
        rate = "— (no results yet)"
    elif detailed:
        rate = f"**{s.rate}%** ({s.completed} on time / {s.late} late / {s.failed} failed)"
    else:
        rate = f"**{s.rate}%** ({s.completed}/{s.late}/{s.failed})"
    if s.streak_kind == DONE:
        streak = f"**{plural(s.streak, 'completion')}** 🔥"
    elif s.streak_kind == FAILED:
        streak = f"**{plural(s.streak, 'fail')}** ❌"
    else:
        streak = "—"
    count = DETAILED_RECENT_RESULTS if detailed else RECENT_RESULTS
    results = ledger.log.replace(EMPTY, "")
    recent = "".join(SQUARES[c] for c in results[-count:])
    if ledger.is_due_now(now.date()):
        recent += OPEN_SQUARE
    if detailed:
        title = "📊 **Habit stats**"
        footer = [f"_Updated {now:%d %b %Y %H:%M}_ `{ledger.serialize()}`"]
    else:
        title = f"[📊]({ABOUT_URL}#{ledger.serialize()}) **Habit stats**"
        footer = []
    return "\n".join(
        [
            title,
            f"Tracking since: **{ledger.since:%d %b %Y}**",
            f"Completion rate: {rate}",
            f"Current streak: {streak}",
            f"Best streak: {plural(s.best_streak, 'completion')}",
            f"Last {count} results: {recent or '—'}",
            *footer,
        ]
    )


def replay(ledger: Ledger, schedule: Schedule | None, events: list[Event], tz: tzinfo):
    """Apply completions, undos and due date changes in the order they
    happened, failing missed occurrences as each day arrives."""
    for e in events:
        day = e.at.astimezone(tz).date()
        ledger.fail_missed(schedule, day)
        due_on = e.due_at.astimezone(tz).date() if e.due_at else None
        if e.kind == "completed":
            ledger.complete(schedule, day, due_on, e.occurrence)
        elif e.kind == "uncompleted":
            ledger.take_back()
        else:
            ledger.take_back(moved_to=due_on)


def history_start(
    events: list[Event], schedule: Schedule | None, tz: tzinfo, created: date | None = None
) -> Ledger | None:
    """A fresh ledger for rebuilding history from the task's first completion."""
    first = next((e for e in events if e.kind == "completed"), None)
    if first is None:
        return None
    done_on = first.at.astimezone(tz).date()
    # It counts for the occurrence Todoist had open, unless that date is
    # further out than the next one (like a mistyped year while setting up).
    latest = (schedule.after(done_on) if schedule else None) or done_on
    occurrence = first.occurrence if first.occurrence and first.occurrence <= latest else done_on
    next_due = first.due_at.astimezone(tz).date() if first.due_at else None
    pending = scheduled_occurrence(schedule, occurrence, next_due, earliest=created)
    # History starts with that occurrence, even when it was done ahead.
    checked = local_midnight(min(done_on, pending), tz)
    return Ledger(since=pending, pending=pending, checked=checked, log="")


def update_task(api, task: dict, now: datetime, dry_run: bool, rebuild: bool = False) -> str:
    """Bring the task's stats comment up to date. With `rebuild`, start over
    from the task's whole activity history instead of the last check."""
    tz, today = now.tzinfo, now.date()
    until = now - SETTLE_TIME
    ours = [c for c in api.comments(task["id"]) if MARKER_RE.search(c.get("content", ""))]
    comment = ours[-1] if ours else None
    due = due_day(task["due"], tz)
    schedule = parse_schedule(task["due"].get("string") or "")
    created = parse_utc(task["added_at"]).astimezone(tz).date() if task.get("added_at") else None
    ledger, history = None, None
    if rebuild:
        history = sorted(api.events(task["id"], created or today - timedelta(days=3650)), key=lambda e: e.at)
        ledger = history_start(history, schedule, tz, created)
    elif comment:
        ledger = Ledger.parse(comment["content"], tz)
    if ledger is None:
        since = min(today, due)
        ledger = Ledger(since=since, pending=None, checked=local_midnight(since, tz), log="")

    # Replay what happened since the last check, in order: a completion can
    # be taken back and the task moved again before the next run.
    if history is None:
        history = api.events(task["id"], ledger.checked.astimezone(timezone.utc).date() - timedelta(days=1))
    events = sorted((e for e in history if ledger.checked < e.at <= until), key=lambda e: e.at)
    if ledger.pending is None:
        # New tracking, an older comment, or a schedule the script can't
        # read: start from the occurrence Todoist has open.
        first = next((e for e in events if e.kind == "completed"), None)
        next_due = first.due_at.astimezone(tz).date() if first and first.due_at else None
        ledger.pending = scheduled_occurrence(schedule, (first and first.occurrence) or due, next_due, created)
    replay(ledger, schedule, events, tz)
    ledger.fail_missed(schedule, today)
    ledger.extend_to(today)
    ledger.checked = max(ledger.checked, until)

    content = render(ledger, now, detailed=DETAILED_NAME in task["content"].lower())
    if dry_run:
        print(f"--- {task['content']} ({task['id']})\n{content}\n")
    elif comment:
        api.update_comment(comment["id"], content)
    else:
        api.add_comment(task["id"], content)
    return content


def is_tracked(task: dict, label: str) -> bool:
    labels = {name.lower() for name in task.get("labels") or []}
    return label.lower() in labels and bool((task.get("due") or {}).get("is_recurring"))


def created_by(task: dict, user_id: str) -> bool:
    """Tasks in shared projects show up for every member. Only the account that
    created a task tracks it, so each stats comment has one writer."""
    return task.get("added_by_uid") in (None, user_id)


def task_id_from(value: str) -> str:
    """A task ID, or the ID at the end of a task link (…/app/task/some-title-ID)."""
    path = urllib.parse.urlparse(value.strip()).path
    return path.rstrip("/").rsplit("/", 1)[-1].rsplit("-", 1)[-1]


def run(
    api, label: str, now: datetime, dry_run: bool = False, task_id: str | None = None, rebuild: bool = False
) -> int:
    user_id = api.user_id()
    others = 0
    if task_id:
        task = api.task(task_id)
        if not is_tracked(task, label):
            print(f"error: {task['content']} ({task_id}) is not a recurring @{label} task", file=sys.stderr)
            return 1
        if not created_by(task, user_id):
            print(f"error: {task['content']} ({task_id}) is tracked by the account that created it", file=sys.stderr)
            return 1
        tasks = [task]
    else:
        tracked = [t for t in api.tasks_with_label(label) if is_tracked(t, label)]
        tasks = [t for t in tracked if created_by(t, user_id)]
        others = len(tracked) - len(tasks)
    errors = 0
    for task in tasks:
        try:
            update_task(api, task, now, dry_run, rebuild)
            if not dry_run:
                print(f"updated: {task['content']}")
        except Exception as e:  # keep going so one bad task doesn't block the rest
            errors += 1
            print(f"error: {task['content']} ({task['id']}): {e}", file=sys.stderr)
    summary = f"{len(tasks) - errors}/{len(tasks)} recurring @{label} tasks processed"
    if others:
        summary += f", {others} left to the accounts that created them"
    print(summary)
    return 1 if errors else 0


def load_dotenv(path: Path):
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def resolve_timezone(api: Todoist) -> tzinfo:
    name = os.environ.get("HABIT_TZ") or api.user_timezone()
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo


def main() -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--label", default=os.environ.get("HABIT_LABEL", "track"))
    parser.add_argument(
        "--account", metavar="NAME", help="use the token in TODOIST_API_TOKEN_NAME instead of TODOIST_API_TOKEN"
    )
    parser.add_argument("--dry-run", action="store_true", help="print comments instead of writing them")
    parser.add_argument("--task", metavar="ID_OR_LINK", help="update only this task (its ID or Todoist link)")
    parser.add_argument(
        "--rebuild", action="store_true", help="recalculate the task's whole history (needs --task)"
    )
    args = parser.parse_args()
    if args.rebuild and not args.task:
        parser.error("--rebuild replaces a task's history, so it needs --task to name the task")

    token_var = f"TODOIST_API_TOKEN_{args.account.upper()}" if args.account else "TODOIST_API_TOKEN"
    token = os.environ.get(token_var)
    if not token:
        print(f"{token_var} is not set (env var or .env next to this script)", file=sys.stderr)
        return 2
    api = Todoist(token)
    try:
        now = datetime.now(resolve_timezone(api))
        task_id = task_id_from(args.task) if args.task else None
        return run(api, args.label, now, args.dry_run, task_id, args.rebuild)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

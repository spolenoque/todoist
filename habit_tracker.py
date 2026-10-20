#!/usr/bin/env python3
"""Todoist habit tracker.

For every recurring task carrying the tracking label (default: "track"), keep a
single comment on the task with habit stats: how long it has been tracked, the
completion rate, and the current streak.

Every calendar day since tracking started gets at most one outcome:
  failed    - a run of this script found the task overdue: every scheduled
              occurrence from the overdue due date up to yesterday is failed
              (today too, if a due time today has already passed). The
              schedule is read from the task's repeat text ("every monday");
              if it can't be read, only the due date fails. Failures are final.
  completed - the task was completed that day and the day isn't failed.
Tracking starts on the first run, or at the overdue due date if the task is
already overdue then.

Failures cannot be reconstructed from Todoist later, so the per-day log is
stored in the stats comment itself (the last line). The script is stateless
otherwise and can run anywhere; run it at least once a day.

Usage:
  TODOIST_API_TOKEN=... python3 habit_tracker.py [--dry-run] [--label track]
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
from zoneinfo import ZoneInfo

API_URL = "https://api.todoist.com/api/v1"

DONE, FAILED, EMPTY = "+", "-", "."
DATA_RE = re.compile(
    r"`habit-tracker:v1:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2}):([+.\-]*)`"
)
RECENT_RESULTS = 10
SQUARES = {DONE: "🟩", FAILED: "🟥"}

WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|"
    "march|april|june|july|august|september|october|november|december"
)
ORDINAL = r"(?:\d{1,2}(?:st|nd|rd|th)?|first|second|third|fourth|fifth|last)"


class Todoist:
    def __init__(self, token: str):
        self.token = token

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

    def user_timezone(self) -> str | None:
        return (self._request("GET", "user").get("tz_info") or {}).get("timezone")

    def tasks_with_label(self, label: str) -> list[dict]:
        return list(self._paginate("tasks", {"label": label, "limit": 200}))

    def completion_times(self, task_id: str, since: date) -> list[datetime]:
        # Recurring completions are not returned by /tasks/completed/*; they
        # only show up in the activity log as item:completed events.
        params = {
            "object_id": task_id,
            "object_event_types": json.dumps(["item:completed"]),
            "date_from": since.isoformat(),
            "limit": 100,
        }
        # The API doesn't reliably apply object_id, so filter here as well.
        return [
            parse_utc(e["event_date"])
            for e in self._paginate("activities", params)
            if e.get("object_id") == task_id and e.get("event_type") == "completed"
        ]

    def comments(self, task_id: str) -> list[dict]:
        return list(self._paginate("comments", {"task_id": task_id, "limit": 200}))

    def add_comment(self, task_id: str, content: str):
        self._request("POST", "comments", body={"task_id": task_id, "content": content})

    def update_comment(self, comment_id: str, content: str):
        self._request("POST", f"comments/{comment_id}", body={"content": content})


def parse_utc(value: str) -> datetime:
    # Python 3.9's fromisoformat() does not accept a trailing "Z".
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def overdue_since(due: dict, now: datetime) -> date | None:
    """Local due day if the task is overdue at `now`, else None.

    `due["date"]` is either a full-day date ("2026-10-16"), a floating local
    datetime ("2026-10-16T09:00:00") or a fixed UTC datetime ending in "Z".
    """
    raw = due["date"]
    if len(raw) == 10:
        due_day = date.fromisoformat(raw)
        return due_day if due_day < now.date() else None
    if raw.endswith("Z"):
        due_at = parse_utc(raw).astimezone(now.tzinfo)
    else:
        due_at = datetime.fromisoformat(raw).replace(tzinfo=now.tzinfo)
    return due_at.date() if due_at < now else None


@dataclass(frozen=True)
class Schedule:
    unit: str  # "day", "week", "month" or "year"
    every: int = 1
    weekdays: frozenset = frozenset()  # with unit "week": only these days


def parse_weekdays(text: str) -> frozenset | None:
    words = [w for w in re.findall(r"\w+", text) if w != "and"]
    if words and all(w in WEEKDAYS for w in words):
        return frozenset(WEEKDAYS[w] for w in words)
    return None


def parse_schedule(text: str) -> Schedule | None:
    """Read the common English forms of Todoist's repeat text, e.g.
    "every day at 8am", "every! 3 days", "every other monday",
    "every mon, wed, fri", "every 2 weeks on tue", "every 15th", "every jan 1".
    """
    s = text.lower().strip()
    s = re.sub(r"^ev(?:ery)?!?(?=\s)", "every", s)
    s = re.split(r"\s+(?:at|starting|from|until|ending|for)\s", s)[0]
    s = re.sub(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()

    if re.fullmatch(r"daily|everyday|hourly|every (?:day|morning|afternoon|evening|night|hour|\d+ hours?)", s):
        return Schedule("day")
    if s in ("every weekday", "every workday"):
        return Schedule("week", weekdays=frozenset(range(5)))
    if s == "every weekend":
        return Schedule("week", weekdays=frozenset({5, 6}))
    adverbs = {"weekly": "week", "monthly": "month", "yearly": "year", "annually": "year"}
    if s in adverbs:
        return Schedule(adverbs[s])
    if re.fullmatch(rf"every (?:(?:{MONTHS}) {ORDINAL}|{ORDINAL} (?:{MONTHS}))", s):
        return Schedule("year")

    m = re.fullmatch(r"every (other |\d+ )?(.+)", s)
    if not m:
        return None
    every = 2 if m[1] == "other " else int(m[1]) if m[1] else 1
    rest = m[2]
    unit = re.fullmatch(r"(day|week|month|year)s?(?: on (.+))?", rest)
    if unit:
        weekdays = parse_weekdays(unit[2] or "") if unit[1] == "week" else None
        return Schedule(unit[1], every, weekdays or frozenset())
    weekdays = parse_weekdays(rest)
    if weekdays:
        return Schedule("week", every, weekdays)
    weekday_names = "|".join(WEEKDAYS)
    if every == 1 and re.fullmatch(rf"(?:the )?{ORDINAL}(?: (?:day|workday|weekday|{weekday_names}))?", rest):
        return Schedule("month")
    return None


def add_months(day: date, months: int) -> date:
    year, month = divmod(day.month - 1 + months, 12)
    year, month = day.year + year, month + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def scheduled_days(schedule: Schedule, start: date, end: date) -> list[date]:
    """Occurrences from `start` (itself an occurrence) through `end`."""
    if schedule.weekdays:
        first_monday = start - timedelta(days=start.weekday())
        days, day = [start], start + timedelta(days=1)
        while day <= end:
            week = (day - first_monday).days // 7
            if day.weekday() in schedule.weekdays and week % schedule.every == 0:
                days.append(day)
            day += timedelta(days=1)
        return days
    step = {
        "day": lambda k: start + timedelta(days=k),
        "week": lambda k: start + timedelta(weeks=k),
        "month": lambda k: add_months(start, k),
        "year": lambda k: add_months(start, 12 * k),
    }[schedule.unit]
    days, k = [], 0
    while (day := step(k * schedule.every)) <= end:
        days.append(day)
        k += 1
    return days


def missed_days(due: dict, due_day: date, last: date) -> list[date]:
    schedule = parse_schedule(due.get("string") or "")
    return scheduled_days(schedule, due_day, last) if schedule else [due_day]


@dataclass
class Ledger:
    since: date  # first tracked day
    synced: date  # day of the last run
    log: str  # one outcome char per day, starting at `since`

    @classmethod
    def parse(cls, text: str) -> Ledger | None:
        m = DATA_RE.search(text)
        if not m:
            return None
        return cls(date.fromisoformat(m[1]), date.fromisoformat(m[2]), m[3])

    def serialize(self) -> str:
        return f"`habit-tracker:v1:{self.since}:{self.synced}:{self.log}`"

    def extend_to(self, today: date):
        days = (today - self.since).days + 1
        self.log = self.log[:days].ljust(days, EMPTY)

    def mark(self, day: date, outcome: str):
        i = (day - self.since).days
        if not 0 <= i < len(self.log):
            return
        if outcome == DONE and self.log[i] == FAILED:
            return
        self.log = self.log[:i] + outcome + self.log[i + 1 :]


@dataclass
class Stats:
    completed: int
    failed: int
    streak: int
    streak_kind: str | None  # DONE, FAILED or None when there are no outcomes
    best_streak: int

    @property
    def rate(self) -> int | None:
        total = self.completed + self.failed
        return round(100 * self.completed / total) if total else None


def compute_stats(ledger: Ledger) -> Stats:
    outcomes = ledger.log.replace(EMPTY, "")
    streak_kind = outcomes[-1] if outcomes else None
    streak = len(outcomes) - len(outcomes.rstrip(streak_kind)) if streak_kind else 0
    best = max((len(run) for run in re.findall(r"\++", outcomes)), default=0)
    return Stats(
        completed=outcomes.count(DONE),
        failed=outcomes.count(FAILED),
        streak=streak,
        streak_kind=streak_kind,
        best_streak=best,
    )


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def render(ledger: Ledger, now: datetime) -> str:
    s = compute_stats(ledger)
    if s.rate is None:
        rate = "— (no results yet)"
    else:
        rate = f"**{s.rate}%** ({s.completed} completed / {s.failed} failed)"
    if s.streak_kind == DONE:
        streak = f"**{plural(s.streak, 'completion')}** 🔥"
    elif s.streak_kind == FAILED:
        streak = f"**{plural(s.streak, 'fail')}** ❌"
    else:
        streak = "—"
    outcomes = ledger.log.replace(EMPTY, "")
    recent = "".join(SQUARES[c] for c in outcomes[-RECENT_RESULTS:]) or "—"
    return "\n".join(
        [
            "📊 **Habit stats**",
            f"Tracking since: **{ledger.since:%d %b %Y}**",
            f"Completion rate: {rate}",
            f"Current streak: {streak}",
            f"Best streak: {plural(s.best_streak, 'completion')}",
            f"Last {RECENT_RESULTS} results: {recent}",
            f"_Updated {now:%d %b %Y %H:%M}_ {ledger.serialize()}",
        ]
    )


def update_task(api, task: dict, now: datetime, dry_run: bool) -> str:
    today = now.date()
    ours = [c for c in api.comments(task["id"]) if DATA_RE.search(c.get("content", ""))]
    comment = ours[-1] if ours else None
    ledger = Ledger.parse(comment["content"]) if comment else None
    due_day = overdue_since(task["due"], now)
    if ledger is None or ledger.since > today:
        since = min(today, due_day or today)
        ledger = Ledger(since=since, synced=since, log="")
    ledger.extend_to(today)

    if due_day is not None:
        # Each occurrence the task stayed overdue through fails: every day
        # for a daily task, once a week for a weekly one. Today only counts
        # once its due time has passed.
        for day in missed_days(task["due"], due_day, max(due_day, today - timedelta(days=1))):
            ledger.mark(day, FAILED)

    # Re-read from the day before the last run: covers timezone skew between
    # the API's date filter and local days, and re-marking a day is harmless.
    for completed_at in api.completion_times(task["id"], ledger.synced - timedelta(days=1)):
        ledger.mark(completed_at.astimezone(now.tzinfo).date(), DONE)

    ledger.synced = today
    content = render(ledger, now)
    if dry_run:
        print(f"--- {task['content']} ({task['id']})\n{content}\n")
    elif comment:
        api.update_comment(comment["id"], content)
    else:
        api.add_comment(task["id"], content)
    return content


def run(api, label: str, now: datetime, dry_run: bool = False) -> int:
    tasks = [t for t in api.tasks_with_label(label) if (t.get("due") or {}).get("is_recurring")]
    errors = 0
    for task in tasks:
        try:
            update_task(api, task, now, dry_run)
            if not dry_run:
                print(f"updated: {task['content']}")
        except Exception as e:  # keep going so one bad task doesn't block the rest
            errors += 1
            print(f"error: {task['content']} ({task['id']}): {e}", file=sys.stderr)
    print(f"{len(tasks) - errors}/{len(tasks)} recurring @{label} tasks processed")
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
    parser.add_argument("--dry-run", action="store_true", help="print comments instead of writing them")
    args = parser.parse_args()

    token = os.environ.get("TODOIST_API_TOKEN")
    if not token:
        print("TODOIST_API_TOKEN is not set (env var or .env next to this script)", file=sys.stderr)
        return 2
    api = Todoist(token)
    try:
        now = datetime.now(resolve_timezone(api))
        return run(api, args.label, now, args.dry_run)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

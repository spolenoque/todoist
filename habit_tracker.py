#!/usr/bin/env python3
"""Todoist habit tracker.

For every recurring task carrying the tracking label (default: "track"), keep a
single comment on the task with habit stats: when tracking started, the
completion rate, the current and best streak, and the last 10 results.

Every occurrence of the task gets one result, recorded on the day it was
scheduled for:
  completed - done on that day, or earlier.
  late      - done after that day, but before the next occurrence's day.
  failed    - the next occurrence's day arrived and it still wasn't done.
Postponing a task doesn't change the day it was scheduled for. Occurrence days
come from the task's repeat text ("every monday"); if the script can't read
it, an occurrence fails the day after it was due and there is no late.

Todoist doesn't record which occurrence is still open, so the per-day results,
the open occurrence and the time of the last check are stored in the stats
comment itself (the last line). Run the script at least once a day.

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

DONE, LATE, FAILED, EMPTY = "+", "~", "-", "."
DATE = r"\d{4}-\d{2}-\d{2}"
MARKER_RE = re.compile(r"`habit-tracker:v\d:")
DATA_RE = re.compile(rf"`habit-tracker:v3:({DATE}):({DATE}|-):(\d+):([+~.\-]*)`")
# Written by the first version, before late results existed.
V1_RE = re.compile(rf"`habit-tracker:v1:({DATE}):({DATE}):([+.\-]*)`")
UPDATED_RE = re.compile(r"_Updated (\d{1,2} \w{3} \d{4} \d{2}:\d{2})_")
# Completions this recent wait for the next run, in case the activity log
# hasn't caught up yet.
SETTLE_TIME = timedelta(minutes=5)
RECENT_RESULTS = 10
SQUARES = {DONE: "🟩", LATE: "🟨", FAILED: "🟥"}

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
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1}
ALL_DAYS = frozenset(range(7))
WORKDAYS = frozenset(range(5))


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

    def completions(self, task_id: str, since: date) -> list[tuple[datetime, date | None, datetime | None]]:
        """(completed at, local day the completed occurrence was due, next due
        time) for each completion of the task on or after `since`."""
        # Recurring completions are not returned by /tasks/completed/*; they
        # only show up in the activity log as item:completed events.
        params = {
            "object_id": task_id,
            "object_event_types": json.dumps(["item:completed"]),
            "date_from": since.isoformat(),
            "limit": 100,
        }
        result = []
        # The API doesn't reliably apply object_id, so filter here as well.
        for e in self._paginate("activities", params):
            if e.get("object_id") != task_id or e.get("event_type") != "completed":
                continue
            extra = e.get("extra_data") or {}
            occurrence = extra.get("completed_due_date_local")
            next_due = extra.get("due_date")
            result.append(
                (
                    parse_utc(e["event_date"]),
                    date.fromisoformat(occurrence[:10]) if occurrence else None,
                    parse_utc(next_due) if next_due else None,
                )
            )
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
    """
    s = text.lower().strip()
    s = re.sub(r"^ev(?:ery)?!?(?=\s)", "every", s)
    s = re.split(r"\s+(?:at|starting|from|until|ending|for)\s", s)[0]
    s = re.sub(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b", "", s)
    s = re.sub(r"\s+", " ", s).strip()

    if re.fullmatch(r"daily|everyday|hourly|every (?:day|morning|afternoon|evening|night|hour|\d+ hours?)", s):
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

    @classmethod
    def parse(cls, text: str, tz: tzinfo) -> Ledger | None:
        if m := DATA_RE.search(text):
            pending = date.fromisoformat(m[2]) if m[2] != "-" else None
            return cls(date.fromisoformat(m[1]), pending, datetime.fromtimestamp(int(m[3]), timezone.utc), m[4])
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
        return f"`habit-tracker:v3:{self.since}:{self.pending or '-'}:{checked}:{self.log}`"

    def extend_to(self, day: date):
        self.log = self.log.ljust((day - self.since).days + 1, EMPTY)

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

    def complete(self, schedule: Schedule | None, done_on: date, next_due: date | None):
        """Settle the open occurrence: done on or before its day is completed,
        after it is late, and once the next one's day arrived it has failed."""
        self.fail_missed(schedule, done_on)
        if self.pending is not None:
            self.mark(self.pending, DONE if done_on <= self.pending else LATE)
            next_due = next_due or (schedule.after(self.pending) if schedule else None)
        self.pending = next_due


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


def render(ledger: Ledger, now: datetime) -> str:
    s = compute_stats(ledger)
    if s.rate is None:
        rate = "— (no results yet)"
    else:
        rate = f"**{s.rate}%** ({s.completed} on time / {s.late} late / {s.failed} failed)"
    if s.streak_kind == DONE:
        streak = f"**{plural(s.streak, 'completion')}** 🔥"
    elif s.streak_kind == FAILED:
        streak = f"**{plural(s.streak, 'fail')}** ❌"
    else:
        streak = "—"
    results = ledger.log.replace(EMPTY, "")
    recent = "".join(SQUARES[c] for c in results[-RECENT_RESULTS:]) or "—"
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
    tz, today = now.tzinfo, now.date()
    ours = [c for c in api.comments(task["id"]) if MARKER_RE.search(c.get("content", ""))]
    comment = ours[-1] if ours else None
    ledger = Ledger.parse(comment["content"], tz) if comment else None
    due = due_day(task["due"], tz)
    schedule = parse_schedule(task["due"].get("string") or "")
    if ledger is None:
        since = min(today, due)
        ledger = Ledger(since=since, pending=None, checked=local_midnight(since, tz), log="")
    ledger.extend_to(today)

    until = now - SETTLE_TIME
    fetch_from = ledger.checked.astimezone(timezone.utc).date() - timedelta(days=1)
    new = sorted(
        (c for c in api.completions(task["id"], fetch_from) if ledger.checked < c[0] <= until),
        key=lambda c: c[0],
    )
    if ledger.pending is None:
        # New tracking, an older comment, or a schedule the script can't
        # read: start from the occurrence Todoist has open.
        ledger.pending = (new and new[0][1]) or due
    for completed_at, _, next_due_at in new:
        next_due = next_due_at.astimezone(tz).date() if next_due_at else None
        ledger.complete(schedule, completed_at.astimezone(tz).date(), next_due)
    ledger.fail_missed(schedule, today)
    ledger.checked = max(ledger.checked, until)

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

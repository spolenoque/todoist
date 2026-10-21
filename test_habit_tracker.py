import contextlib
import io
import random
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from habit_tracker import (
    ALL_DAYS,
    DATA_RE,
    WORKDAYS,
    Event,
    Ledger,
    Schedule,
    Todoist,
    compute_stats,
    due_day,
    parse_schedule,
    render,
    run,
    task_id_from,
    update_task,
)

TZ = ZoneInfo("Europe/Warsaw")
# 2026-10-19 is a Monday, 2026-10-21 a Wednesday and 2026-10-25 a Sunday.


def at(day: str, hour: int = 7, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}").replace(tzinfo=TZ)


def d(iso: str) -> date:
    return date.fromisoformat(iso)


def due_time(value: str) -> datetime:
    """UTC time of a due date ("2026-10-21") or local due time ("2026-10-21T20:00:00")."""
    local = datetime.fromisoformat(value).replace(tzinfo=TZ) if "T" in value else at(value, 12)
    return local.astimezone(timezone.utc)


class FakeTodoist:
    def __init__(self, tasks):
        self.tasks = tasks
        self.history = {}  # task_id -> [Event]
        self.comment_store = {}  # task_id -> [comment]
        self.writes = 0

    def tasks_with_label(self, label):
        return [t for t in self.tasks if label in t.get("labels", [])]

    def task(self, task_id):
        for t in self.tasks:
            if t["id"] == task_id:
                return t
        raise RuntimeError(f"GET tasks/{task_id} -> HTTP 404")

    def events(self, task_id, since):
        return [e for e in self.history.get(task_id, []) if e.at.date() >= since]

    def comments(self, task_id):
        return self.comment_store.get(task_id, [])

    def add_comment(self, task_id, content):
        self.writes += 1
        comments = self.comment_store.setdefault(task_id, [])
        comments.append({"id": f"c{len(comments)}", "content": content})

    def update_comment(self, comment_id, content):
        self.writes += 1
        for comments in self.comment_store.values():
            for c in comments:
                if c["id"] == comment_id:
                    c["content"] = content

    def complete(self, task, local_dt: datetime, next_due: str):
        """Complete the open occurrence; like Todoist, move the task to `next_due`."""
        occurrence = d(task["due"]["date"][:10])
        self.log_event(task, Event(local_dt.astimezone(timezone.utc), "completed", occurrence, due_time(next_due)))
        task["due"]["date"] = next_due

    def move(self, task, local_dt: datetime, new_due: str):
        self.log_event(task, Event(local_dt.astimezone(timezone.utc), "moved", due_at=due_time(new_due)))
        task["due"]["date"] = new_due

    def undo(self, task, local_dt: datetime, restored_due: str):
        """Todoist's undo of a completion, which puts the old due date back."""
        self.log_event(task, Event(local_dt.astimezone(timezone.utc), "uncompleted"))
        task["due"]["date"] = restored_due

    def log_event(self, task, event):
        self.history.setdefault(task["id"], []).append(event)

    def ledger(self, task_id) -> Ledger:
        return Ledger.parse(self.comment_store[task_id][-1]["content"], TZ)

    def log(self, task_id) -> str:
        return self.ledger(task_id).log


def habit(due: str, task_id="1", labels=("track",), recurring=True, every="every day"):
    return {
        "id": task_id,
        "content": f"habit {task_id}",
        "labels": list(labels),
        "added_at": "2026-01-01T00:00:00Z",
        "due": {"date": due, "is_recurring": recurring, "string": every},
    }


class DueDayTest(unittest.TestCase):
    def test_full_day_and_floating(self):
        self.assertEqual(due_day({"date": "2026-10-16"}, TZ), d("2026-10-16"))
        self.assertEqual(due_day({"date": "2026-10-16T09:00:00"}, TZ), d("2026-10-16"))

    def test_utc_datetime_uses_local_day(self):
        # 23:30 UTC on the 15th is 01:30 on the 16th in Warsaw.
        self.assertEqual(due_day({"date": "2026-10-15T23:30:00.000000Z"}, TZ), d("2026-10-16"))


class ScheduleTest(unittest.TestCase):
    def test_parse(self):
        sunday = frozenset({6})
        cases = {
            "every day at 8pm": Schedule("day"),
            "every! 3 days": Schedule("day", 3),
            "every other day": Schedule("day", 2),
            "every week": Schedule("week"),
            "every monday": Schedule("week", weekdays=frozenset({0})),
            "every mon, wed, fri 7am": Schedule("week", weekdays=frozenset({0, 2, 4})),
            "every other monday": Schedule("week", 2, frozenset({0})),
            "every 2 weeks on tue and thu": Schedule("week", 2, frozenset({1, 3})),
            "every weekday": Schedule("week", weekdays=WORKDAYS),
            "every 3 months": Schedule("month", 3),
            "every 15th": Schedule("nth", rules=((15, ALL_DAYS, 0),)),
            "every 2nd monday": Schedule("nth", rules=((2, frozenset({0}), 0),)),
            "every last workday": Schedule("nth", rules=((-1, WORKDAYS, 0),)),
            "every jan 1": Schedule("nth", rules=((1, ALL_DAYS, 1),)),
            "every last Sun Feb, last Sun Apr": Schedule("nth", rules=((-1, sunday, 2), (-1, sunday, 4))),
            "every day starting oct 1": Schedule("day"),
            "every 12 hours": None,
            "hourly": None,
            "every 2nd tuesday of the quarter": None,
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(parse_schedule(text), expected)

    def test_next_occurrence(self):
        retro = "every last Sun Feb, last Sun Apr, last Sun Jun, last Sun Aug, last Sun Oct, last sun Dec"
        cases = [
            ("every day", "2026-10-21", "2026-10-22"),
            ("every monday", "2026-10-21", "2026-10-26"),
            ("every other monday", "2026-10-19", "2026-11-02"),
            ("every weekday", "2026-10-23", "2026-10-26"),
            ("every mon, thu", "2026-10-19", "2026-10-22"),
            ("every 2 days", "2026-10-21", "2026-10-23"),
            ("every 31st", "2026-01-31", "2026-02-28"),
            ("every 31st", "2026-02-28", "2026-03-31"),
            ("every 2nd monday", "2026-10-12", "2026-11-09"),
            (retro, "2026-10-25", "2026-12-27"),
            ("every 1st Sun Jan, 1st Sun May, 1st Sun Sep", "2027-01-03", "2027-05-02"),
        ]
        for text, day, expected in cases:
            with self.subTest(text=text, day=day):
                self.assertEqual(parse_schedule(text).after(d(day)), d(expected))


class StatsTest(unittest.TestCase):
    def ledger(self, log):
        return Ledger(d("2026-10-01"), None, at("2026-10-01"), log)

    def test_late_counts_as_done(self):
        s = compute_stats(self.ledger("+~.-++.~"))
        self.assertEqual((s.completed, s.late, s.failed, s.rate), (3, 2, 1, 83))
        self.assertEqual((s.streak, s.streak_kind, s.best_streak), (3, "+", 3))

    def test_failed_streak(self):
        s = compute_stats(self.ledger("+~--."))
        self.assertEqual((s.streak, s.streak_kind, s.best_streak), (2, "-", 2))

    def test_render(self):
        content = render(self.ledger("+~.-++.~"), at("2026-10-08"))
        self.assertIn("Completion rate: **83%** (3 on time / 2 late / 1 failed)", content)
        self.assertIn("Current streak: **3 completions** 🔥", content)
        self.assertIn("Last 10 results: 🟩🟨🟥🟩🟩🟨\n", content)

    def test_render_adds_a_grey_square_for_the_occurrence_due_now(self):
        ledger = Ledger(d("2026-10-01"), d("2026-10-09"), at("2026-10-09"), "+~.-++.~.")
        self.assertIn("Last 10 results: 🟩🟨🟥🟩🟩🟨⬜\n", render(ledger, at("2026-10-09", 14)))
        self.assertIn("Last 10 results: 🟩🟨🟥🟩🟩🟨\n", render(ledger, at("2026-10-08", 14)))  # not due yet
        fresh = Ledger(d("2026-10-08"), d("2026-10-08"), at("2026-10-08"), ".")
        self.assertIn("Last 10 results: ⬜\n", render(fresh, at("2026-10-08", 14)))

    def test_postponed_task_shows_grey_until_it_fails(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.move(task, at("2026-10-21", 12), "2026-10-22")
        run(api, "track", at("2026-10-21", 14))
        self.assertIn("Last 10 results: ⬜\n", api.comment_store["1"][0]["content"])
        run(api, "track", at("2026-10-22", 1))
        self.assertIn("Last 10 results: 🟥⬜\n", api.comment_store["1"][0]["content"])  # the 22nd is due now
        api.complete(task, at("2026-10-22", 9), "2026-10-23")
        run(api, "track", at("2026-10-22", 10))
        self.assertIn("Last 10 results: 🟥🟩\n", api.comment_store["1"][0]["content"])

    def test_no_results(self):
        s = compute_stats(self.ledger("."))
        self.assertIsNone(s.rate)
        self.assertIsNone(s.streak_kind)


class RebuildTest(unittest.TestCase):
    def test_rebuilds_from_the_first_completion(self):
        task = habit("2026-10-12")
        api = FakeTodoist([task])
        api.complete(task, at("2026-10-12", 9), "2026-10-13")
        api.complete(task, at("2026-10-13", 9), "2026-10-14")
        # nothing on the 14th and 15th
        api.complete(task, at("2026-10-16", 9), "2026-10-17")  # the 14th's occurrence
        run(api, "track", at("2026-10-17", 1))  # tracking started only now
        self.assertEqual(api.ledger("1").since, d("2026-10-17"))
        run(api, "track", at("2026-10-17", 2), rebuild=True)
        ledger = api.ledger("1")
        self.assertEqual((ledger.since, ledger.log, ledger.pending), (d("2026-10-12"), "++--+.", d("2026-10-17")))
        self.assertEqual(len(api.comment_store["1"]), 1)  # same comment, rewritten

    def test_a_mistyped_first_due_date_is_ignored(self):
        # Set up on 2 Jul with the year typed wrong, then fixed and redone.
        task = habit("2027-07-01")
        api = FakeTodoist([task])
        api.complete(task, at("2026-07-02", 11, 19), "2027-07-02")
        api.move(task, at("2026-07-02", 11, 20), "2026-07-02")
        api.complete(task, at("2026-07-02", 11, 21), "2026-07-03")
        api.complete(task, at("2026-07-03", 9), "2026-07-04")
        run(api, "track", at("2026-07-04", 1), rebuild=True)
        ledger = api.ledger("1")
        self.assertEqual((ledger.since, ledger.log, ledger.pending), (d("2026-07-02"), "++.", d("2026-07-04")))

    def test_a_late_first_completion_starts_at_its_due_day(self):
        task = habit("2026-10-12")
        api = FakeTodoist([task])
        api.complete(task, at("2026-10-15", 9), "2026-10-16")  # open since the 12th
        run(api, "track", at("2026-10-16", 1), rebuild=True)
        self.assertEqual(api.log("1"), "---+.")

    def test_first_completion_moved_ahead_counts_for_its_scheduled_day(self):
        # 1st Sunday of Jan/May/Sep, moved to Saturday 5 Sep and done there.
        task = habit("2026-09-05", every="every 1st Sun Jan, 1st Sun May, 1st Sun Sep")
        api = FakeTodoist([task])
        api.complete(task, at("2026-09-05", 18), "2027-01-03")
        run(api, "track", at("2026-10-21", 1), rebuild=True)
        ledger = api.ledger("1")
        self.assertEqual((ledger.since, ledger.log[:1], ledger.pending), (d("2026-09-06"), "+", d("2027-01-03")))
        self.assertNotIn("⬜", api.comment_store["1"][0]["content"])

    def test_moved_ahead_and_ticked_twice_counts_once(self):
        # The real 'Planning' history: created 13 Aug, Sunday 6 Sep moved to
        # Saturday the 5th and done; Todoist put it back on the 6th, so it
        # was ticked again a minute later.
        task = habit("2026-09-06", every="every 1st Sun Jan, 1st Sun May, 1st Sun Sep")
        task["added_at"] = "2026-08-13T12:02:00Z"
        api = FakeTodoist([task])
        api.move(task, at("2026-09-05", 10, 55), "2026-09-05")
        api.complete(task, at("2026-09-05", 15, 40), "2026-09-06")
        api.complete(task, at("2026-09-05", 15, 41), "2027-01-03")
        run(api, "track", at("2026-10-21", 1), rebuild=True)
        ledger = api.ledger("1")
        self.assertEqual((ledger.since, ledger.log.rstrip("."), ledger.pending), (d("2026-09-06"), "+", d("2027-01-03")))

    def test_weekly_done_ahead_then_ticked_again_on_its_day_counts_once(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.move(task, at("2026-10-24", 9), "2026-10-24")
        api.complete(task, at("2026-10-24", 10), "2026-10-25")  # Todoist brings it back to Sunday
        api.complete(task, at("2026-10-25", 10), "2026-11-01")
        run(api, "track", at("2026-10-26", 1))
        ledger = api.ledger("1")
        self.assertEqual((ledger.log.rstrip("."), ledger.pending), ("....+", d("2026-11-01")))

    def test_ticking_an_old_occurrence_on_the_next_ones_day_counts_for_that_one(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.move(task, at("2026-10-24", 9), "2026-10-24")
        api.complete(task, at("2026-10-24", 10), "2026-10-25")  # Todoist brings it back to Sunday the 25th
        # Not ticked again on the 25th; the following Sunday, Todoist still has the 25th open.
        api.complete(task, at("2026-11-01", 10), "2026-11-08")
        run(api, "track", at("2026-11-02", 1))
        ledger = api.ledger("1")
        self.assertEqual((ledger.result(d("2026-10-25")), ledger.result(d("2026-11-01")), ledger.pending), ("+", "+", d("2026-11-08")))

    def test_first_completion_postponed_counts_late_for_its_scheduled_day(self):
        # Sunday 4 Oct postponed to Monday and done there; Todoist then moves it to the 11th.
        task = habit("2026-10-05", every="every sunday")
        api = FakeTodoist([task])
        api.complete(task, at("2026-10-05", 9), "2026-10-11")
        run(api, "track", at("2026-10-06", 1), rebuild=True)
        ledger = api.ledger("1")
        self.assertEqual((ledger.since, ledger.log[0], ledger.pending), (d("2026-10-04"), "~", d("2026-10-11")))

    def test_without_completions_it_starts_like_new_tracking(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1), rebuild=True)
        ledger = api.ledger("1")
        self.assertEqual((ledger.since, ledger.pending), (d("2026-10-21"), d("2026-10-25")))


class OccurrenceTest(unittest.TestCase):
    def test_done_on_its_day(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.complete(task, at("2026-10-21", 20), "2026-10-22")
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.log("1"), "+.")

    def test_done_early_counts_for_its_day(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.complete(task, at("2026-10-21", 18), "2026-11-01")  # Sunday's, done Wednesday
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.log("1"), "....+")  # recorded on Sunday the 25th

    def test_done_after_its_day_but_before_the_next_is_late(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-25", 1))
        run(api, "track", at("2026-10-26", 1))
        api.complete(task, at("2026-10-27", 10), "2026-11-01")
        run(api, "track", at("2026-10-28", 1))
        self.assertEqual(api.log("1"), "~...")

    def test_not_done_when_the_next_one_arrives_fails(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-25", 1))
        run(api, "track", at("2026-10-31", 1))
        self.assertEqual(api.log("1")[0], ".")  # still in time to be late
        run(api, "track", at("2026-11-01", 1))
        self.assertEqual(api.log("1")[0], "-")
        self.assertEqual(api.ledger("1").pending, d("2026-11-01"))

    def test_daily_missed_day_fails_and_doing_it_today_counts_today(self):
        task = habit("2026-10-20")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-20", 1))
        run(api, "track", at("2026-10-21", 1))
        self.assertEqual(api.log("1"), "-.")
        api.complete(task, at("2026-10-21", 10), "2026-10-22")  # Todoist still had the 20th open
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.log("1"), "-+.")

    def test_postponed_to_tomorrow_fails_the_day(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.move(task, at("2026-10-21", 12), "2026-10-22")
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.log("1"), "-.")
        api.complete(task, at("2026-10-22", 20), "2026-10-23")
        run(api, "track", at("2026-10-23", 1))
        self.assertEqual(api.log("1"), "-+.")

    def test_postponed_to_later_the_same_day_and_done(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.move(task, at("2026-10-21", 12), "2026-10-21T20:00:00")
        api.complete(task, at("2026-10-21", 21), "2026-10-22")
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.log("1"), "+.")

    def test_weekly_postponed_and_done_before_the_next_is_late(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-25", 1))
        api.move(task, at("2026-10-25", 12), "2026-10-27")
        api.complete(task, at("2026-10-27", 10), "2026-11-01")
        run(api, "track", at("2026-10-28", 1))
        self.assertEqual(api.log("1"), "~...")

    def test_every_two_days_postponed_by_one_is_late(self):
        task = habit("2026-10-21", every="every 2 days")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.move(task, at("2026-10-21", 12), "2026-10-22")
        api.complete(task, at("2026-10-22", 10), "2026-10-24")
        run(api, "track", at("2026-10-23", 1))
        self.assertEqual(api.log("1"), "~..")
        self.assertEqual(api.ledger("1").pending, d("2026-10-24"))

    def test_postponed_several_days_fails_each_day(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.move(task, at("2026-10-21", 12), "2026-10-24")
        for night in ("2026-10-22", "2026-10-23", "2026-10-24"):
            run(api, "track", at(night, 1))
        self.assertEqual(api.log("1"), "---.")

    def test_moved_ahead_and_done_early_is_completed(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.move(task, at("2026-10-22", 12), "2026-10-24")  # moved to Saturday ahead of time
        api.complete(task, at("2026-10-24", 10), "2026-11-01")
        run(api, "track", at("2026-10-26", 1))
        self.assertEqual(api.log("1"), "....+.")

    def test_unknown_schedule_fails_the_next_day_without_late(self):
        task = habit("2026-10-16", every="every 2nd tuesday of the quarter")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-16", 1))
        api.complete(task, at("2026-10-17", 10), "2027-01-12")
        run(api, "track", at("2026-10-18", 1))
        self.assertEqual(api.log("1"), "-..")
        self.assertEqual(api.ledger("1").pending, d("2027-01-12"))


class RunTest(unittest.TestCase):
    def test_skips_untracked_and_non_recurring(self):
        api = FakeTodoist([habit("2026-10-16", "1", labels=()), habit("2026-10-16", "2", recurring=False)])
        run(api, "track", at("2026-10-16"))
        self.assertEqual(api.comment_store, {})

    def test_first_run_starts_tracking_today(self):
        task = habit("2026-10-10")
        api = FakeTodoist([task])
        api.complete(task, at("2026-10-10", 20), "2026-10-16")  # before tracking: ignored
        run(api, "track", at("2026-10-16"))
        ledger = api.ledger("1")
        self.assertEqual((ledger.since, ledger.log, ledger.pending), (d("2026-10-16"), ".", d("2026-10-16")))

    def test_first_run_on_an_overdue_task_fails_each_missed_occurrence(self):
        api = FakeTodoist([habit("2026-10-12")])
        run(api, "track", at("2026-10-16"))
        ledger = api.ledger("1")
        self.assertEqual((ledger.since, ledger.log), (d("2026-10-12"), "----."))

    def test_completions_count_once(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.complete(task, at("2026-10-21", 20), "2026-10-22")
        run(api, "track", at("2026-10-22", 1))
        run(api, "track", at("2026-10-22", 12))  # same completion seen again
        self.assertEqual(api.log("1"), "+.")
        api.complete(task, at("2026-10-22", 20), "2026-10-23")
        run(api, "track", at("2026-10-23", 1))
        self.assertEqual(api.log("1"), "++.")

    def test_very_recent_completion_waits_for_the_next_run(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.complete(task, at("2026-10-21", 23, 58), "2026-10-22")
        run(api, "track", at("2026-10-21", 23, 59))
        self.assertEqual(api.log("1"), ".")
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.log("1"), "+.")

    def test_carries_over_a_first_version_comment(self):
        task = habit("2026-10-20", every="every weekday")
        api = FakeTodoist([task])
        api.comment_store["1"] = [
            {"id": "user", "content": "my own note"},
            {"id": "stats", "content": "📊 old\n_Updated 21 Oct 2026 03:46_ `habit-tracker:v1:2026-10-20:2026-10-21:-.`"},
        ]
        api.complete(task, at("2026-10-21", 11), "2026-10-22")  # the 20th's, done on the 21st
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.comment_store["1"][0]["content"], "my own note")
        self.assertEqual(api.log("1"), "-+.")
        self.assertIn("`habit-tracker:v4:", api.comment_store["1"][1]["content"])

    def test_single_task_run_updates_only_that_task(self):
        api = FakeTodoist([habit("2026-10-16", "1"), habit("2026-10-16", "2")])
        self.assertEqual(run(api, "track", at("2026-10-16"), task_id="2"), 0)
        self.assertEqual(list(api.comment_store), ["2"])

    def test_single_task_run_refuses_untracked_tasks(self):
        api = FakeTodoist([habit("2026-10-16", "1", labels=()), habit("2026-10-16", "2", recurring=False)])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run(api, "track", at("2026-10-16"), task_id="1"), 1)
            self.assertEqual(run(api, "track", at("2026-10-16"), task_id="2"), 1)
        self.assertEqual(api.comment_store, {})

    def test_task_id_from_id_or_link(self):
        for value in (
            "6h3wcWJ666fWX7rg",
            "https://app.todoist.com/app/task/vchera-bez-sladkogo-6h3wcWJ666fWX7rg",
            "https://app.todoist.com/app/task/vchera-bez-sladkogo-6h3wcWJ666fWX7rg/",
            "https://app.todoist.com/app/task/6h3wcWJ666fWX7rg?view=comments",
        ):
            with self.subTest(value):
                self.assertEqual(task_id_from(value), "6h3wcWJ666fWX7rg")

    def test_dry_run_writes_nothing(self):
        api = FakeTodoist([habit("2026-10-16")])
        run(api, "track", at("2026-10-16"), dry_run=True)
        self.assertEqual(api.writes, 0)

    def test_reads_activity_events(self):
        api = Todoist("token")
        utc = lambda *args: datetime(*args, tzinfo=timezone.utc)
        completed = {"completed_due_date_local": "2026-10-21T23:59:59", "due_date": "2026-10-22T21:59:59.000000Z"}
        activity = [
            {"object_id": "1", "event_type": "completed", "event_date": "2026-10-21T11:50:47Z", "extra_data": completed},
            {"object_id": "2", "event_type": "completed", "event_date": "2026-10-21T11:51:00Z", "extra_data": completed},
            {"object_id": "1", "event_type": "updated", "event_date": "2026-10-21T11:59:56Z",
             "extra_data": {"content": "x", "due_date": "2026-10-21T21:59:59.000000Z", "last_due_date": None}},
            {"object_id": "1", "event_type": "updated", "event_date": "2026-10-21T12:00:00Z",
             "extra_data": {"content": "x", "last_description": "old"}},
            {"object_id": "1", "event_type": "uncompleted", "event_date": "2026-10-21T12:01:00Z", "extra_data": {}},
        ]
        api._paginate = lambda path, params: iter(activity)
        self.assertEqual(
            api.events("1", d("2026-10-20")),
            [
                Event(utc(2026, 10, 21, 11, 50, 47), "completed", d("2026-10-21"), utc(2026, 10, 22, 21, 59, 59)),
                Event(utc(2026, 10, 21, 11, 59, 56), "moved", due_at=utc(2026, 10, 21, 21, 59, 59)),
                Event(utc(2026, 10, 21, 12, 1), "uncompleted"),
            ],
        )

    def test_data_line_round_trip(self):
        checked = datetime(2026, 1, 5, 6, 30, tzinfo=timezone.utc)
        for pending, last_done in ((None, None), (d("2026-01-06"), d("2026-01-05"))):
            ledger = Ledger(d("2026-01-02"), pending, checked, "+-.~+", last_done)
            self.assertTrue(DATA_RE.search(ledger.serialize()))
            self.assertEqual(Ledger.parse("text " + ledger.serialize(), TZ), ledger)

    def test_reads_v3_data_line(self):
        ledger = Ledger.parse("`habit-tracker:v3:2026-10-21:2026-10-22:1792580960:-+`", TZ)
        checked = datetime.fromtimestamp(1792580960, timezone.utc)
        self.assertEqual(ledger, Ledger(d("2026-10-21"), d("2026-10-22"), checked, "-+", None))


class TakeBackTest(unittest.TestCase):
    def completed_then_moved_back(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 12))
        api.complete(task, at("2026-10-21", 13, 50), "2026-10-22")
        api.move(task, at("2026-10-21", 13, 52), "2026-10-21")  # moved back to today
        run(api, "track", at("2026-10-21", 14))
        return task, api

    def test_moving_back_to_today_reopens_the_day(self):
        task, api = self.completed_then_moved_back()
        self.assertEqual((api.log("1"), api.ledger("1").pending), (".", d("2026-10-21")))
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.log("1"), "-.")

    def test_doing_it_again_counts_for_that_day_only(self):
        task, api = self.completed_then_moved_back()
        api.complete(task, at("2026-10-21", 20), "2026-10-22")
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.log("1"), "+.")
        run(api, "track", at("2026-10-23", 1))  # nothing done on the 22nd
        self.assertEqual(api.log("1"), "+-.")

    def test_taken_back_after_a_run_already_counted_it(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 12))
        api.complete(task, at("2026-10-21", 13), "2026-10-22")
        run(api, "track", at("2026-10-21", 14))
        self.assertEqual(api.log("1"), "+")
        api.move(task, at("2026-10-21", 14, 30), "2026-10-21")
        run(api, "track", at("2026-10-21", 15))
        self.assertEqual((api.log("1"), api.ledger("1").pending), (".", d("2026-10-21")))

    def test_late_completion_taken_back(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-25", 1))
        api.complete(task, at("2026-10-27", 10), "2026-11-01")
        api.move(task, at("2026-10-27", 10, 30), "2026-10-27")  # back to that Tuesday
        run(api, "track", at("2026-10-27", 11))
        self.assertEqual((api.log("1"), api.ledger("1").pending), ("...", d("2026-10-25")))
        run(api, "track", at("2026-11-01", 1))
        self.assertEqual(api.log("1")[0], "-")

    def test_early_completion_taken_back(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.complete(task, at("2026-10-21", 10), "2026-11-01")  # Sunday's, done Wednesday
        api.move(task, at("2026-10-21", 10, 30), "2026-10-21")
        run(api, "track", at("2026-10-21", 11))
        self.assertEqual(api.ledger("1").pending, d("2026-10-25"))
        self.assertNotIn("+", api.log("1"))

    def test_moving_the_next_weekly_occurrence_to_today_is_not_taking_back(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-25", 1))
        api.complete(task, at("2026-10-25", 10), "2026-11-01")
        api.move(task, at("2026-10-28", 8), "2026-10-28")  # next week's, brought forward on Wednesday
        run(api, "track", at("2026-10-28", 9))
        self.assertEqual((api.log("1"), api.ledger("1").pending), ("+...", d("2026-11-01")))
        api.complete(task, at("2026-10-28", 18), "2026-11-01")
        run(api, "track", at("2026-10-29", 1))
        self.assertEqual(api.log("1"), "+......+")  # counts for Sunday the 1st, done early

    def test_moving_the_next_interval_occurrence_to_today_is_not_taking_back(self):
        task = habit("2026-10-21", every="every 2 days")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.complete(task, at("2026-10-21", 10), "2026-10-23")
        api.move(task, at("2026-10-22", 8), "2026-10-22")
        run(api, "track", at("2026-10-22", 9))
        self.assertEqual((api.log("1"), api.ledger("1").pending), ("+.", d("2026-10-23")))

    def test_moved_back_then_postponed_still_takes_it_back(self):
        # The exact sequence from 21 Oct: postpone, move back, complete,
        # move back again, postpone again, all before one run.
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 12))
        api.move(task, at("2026-10-21", 13, 43), "2026-10-22")
        api.move(task, at("2026-10-21", 13, 50), "2026-10-21")
        api.complete(task, at("2026-10-21", 13, 51), "2026-10-22")
        api.move(task, at("2026-10-21", 13, 59), "2026-10-21")
        api.move(task, at("2026-10-21", 14, 14), "2026-10-22")
        run(api, "track", at("2026-10-21", 14, 30))
        self.assertEqual((api.log("1"), api.ledger("1").pending), (".", d("2026-10-21")))
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.log("1"), "-.")

    def test_taken_back_and_done_again_before_a_run_counts_once(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 12))
        api.complete(task, at("2026-10-21", 13), "2026-10-22")
        api.move(task, at("2026-10-21", 13, 5), "2026-10-21")
        api.complete(task, at("2026-10-21", 13, 10), "2026-10-22")
        run(api, "track", at("2026-10-21", 14))
        self.assertEqual((api.log("1"), api.ledger("1").pending), ("+", d("2026-10-22")))

    def test_undo_in_todoist_takes_it_back(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 12))
        api.complete(task, at("2026-10-21", 13), "2026-10-22")
        run(api, "track", at("2026-10-21", 14))
        api.undo(task, at("2026-10-21", 14, 10), "2026-10-21")
        run(api, "track", at("2026-10-21", 15))
        self.assertEqual((api.log("1"), api.ledger("1").pending), (".", d("2026-10-21")))

    def test_postponed_then_done_today_leaves_tomorrow_open(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        api.move(task, at("2026-10-21", 12), "2026-10-22")
        api.complete(task, at("2026-10-21", 20), "2026-10-23")  # Todoist skips the 22nd
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual((api.log("1"), api.ledger("1").pending), ("+.", d("2026-10-22")))
        run(api, "track", at("2026-10-23", 1))
        self.assertEqual(api.log("1"), "+-.")


class RandomHistoryTest(unittest.TestCase):
    """Weeks of random completing, postponing and moving back, checking after
    every run that results stay consistent."""

    SCHEDULES = ("every day", "every sunday", "every weekday", "every mon, thu", "every 2nd monday", "every 2 days")

    def test_invariants(self):
        rng = random.Random(20261021)
        for every in self.SCHEDULES:
            for trial in range(15):
                with self.subTest(every=every, trial=trial):
                    self.simulate(rng, every)

    def simulate(self, rng, every):
        schedule = parse_schedule(every)
        start = d("2026-10-05")
        task = habit(schedule.after(start - timedelta(days=1)).isoformat(), every=every)
        api = FakeTodoist([task])
        for offset in range(45):
            day = start + timedelta(days=offset)
            self.run_and_check(api, schedule, at(day.isoformat(), 1))
            if offset == 15:
                with contextlib.redirect_stdout(io.StringIO()):
                    run(api, "track", at(day.isoformat(), 1, 30), rebuild=True)
            elif offset > 15 and offset % 6 == 0:
                self.check_rebuild_agrees(api, task, at(day.isoformat(), 1))
            actions = sorted(
                (rng.randint(8, 22), rng.randint(0, 59), rng.choice(["complete", "complete", "postpone", "back", "undo", "run"]))
                for _ in range(rng.randint(0, 3))
            )
            for hour, minute, action in actions:
                when = at(day.isoformat(), hour, minute)
                due = d(task["due"]["date"][:10])
                if action == "complete":
                    # Like Todoist: the next occurrence after the due date, and after today.
                    nxt = schedule.after(due)
                    while nxt <= day:
                        nxt = schedule.after(nxt)
                    api.complete(task, when, nxt.isoformat())
                elif action == "postpone":
                    api.move(task, when, (max(due, day) + timedelta(days=rng.randint(1, 3))).isoformat())
                elif action == "back":
                    api.move(task, when, day.isoformat())
                elif action == "undo":
                    last = (api.history.get("1") or [None])[-1]
                    if last and last.kind == "completed":
                        api.undo(task, when, last.occurrence.isoformat())
                else:
                    self.run_and_check(api, schedule, when)

    def check_rebuild_agrees(self, api, task, now):
        """After a rebuild, the night-by-night updates and rebuilding again
        from the whole history give exactly the same results."""
        nightly = api.ledger("1")
        with contextlib.redirect_stdout(io.StringIO()):
            rebuilt = Ledger.parse(update_task(api, task, now, dry_run=True, rebuild=True), TZ)
        self.assertEqual(
            (nightly.since, nightly.pending, nightly.log.rstrip(".")),
            (rebuilt.since, rebuilt.pending, rebuilt.log.rstrip(".")),
        )

    def run_and_check(self, api, schedule, now):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run(api, "track", now), 0)
            ledger = api.ledger("1")
            run(api, "track", now)
        self.assertEqual(api.ledger("1"), ledger, "a second run at the same moment changed something")
        self.assertRegex(ledger.log, r"^[+~.\-]*$")
        if not schedule.on_calendar:
            return
        scheduled = lambda day: schedule.after(day - timedelta(days=1)) == day
        for i, result in enumerate(ledger.log):
            if result != ".":
                self.assertTrue(scheduled(ledger.since + timedelta(days=i)), f"result off schedule: {ledger}")
        occurrence = ledger.since if scheduled(ledger.since) else schedule.after(ledger.since)
        while schedule.after(occurrence) <= now.date():
            i = (occurrence - ledger.since).days
            self.assertNotEqual(ledger.log[i : i + 1] or ".", ".", f"{occurrence} has no result: {ledger}")
            occurrence = schedule.after(occurrence)


if __name__ == "__main__":
    unittest.main()

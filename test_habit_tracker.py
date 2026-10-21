import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from habit_tracker import (
    ALL_DAYS,
    DATA_RE,
    WORKDAYS,
    Ledger,
    Schedule,
    Todoist,
    compute_stats,
    due_day,
    parse_schedule,
    render,
    run,
)

TZ = ZoneInfo("Europe/Warsaw")
# 2026-10-19 is a Monday, 2026-10-21 a Wednesday and 2026-10-25 a Sunday.


def at(day: str, hour: int = 7, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}").replace(tzinfo=TZ)


def d(iso: str) -> date:
    return date.fromisoformat(iso)


class FakeTodoist:
    def __init__(self, tasks):
        self.tasks = tasks
        self.done = {}  # task_id -> [(utc time, occurrence day, next due utc time)]
        self.comment_store = {}  # task_id -> [comment]
        self.writes = 0

    def tasks_with_label(self, label):
        return [t for t in self.tasks if label in t.get("labels", [])]

    def completions(self, task_id, since):
        return [c for c in self.done.get(task_id, []) if c[0].date() >= since]

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
        next_at = at(next_due, 12).astimezone(timezone.utc)
        self.done.setdefault(task["id"], []).append((local_dt.astimezone(timezone.utc), occurrence, next_at))
        task["due"]["date"] = next_due

    def ledger(self, task_id) -> Ledger:
        return Ledger.parse(self.comment_store[task_id][-1]["content"], TZ)

    def log(self, task_id) -> str:
        return self.ledger(task_id).log


def habit(due: str, task_id="1", labels=("track",), recurring=True, every="every day"):
    return {
        "id": task_id,
        "content": f"habit {task_id}",
        "labels": list(labels),
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

    def test_no_results(self):
        s = compute_stats(self.ledger("."))
        self.assertIsNone(s.rate)
        self.assertIsNone(s.streak_kind)


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
        task["due"]["date"] = "2026-10-22"
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.log("1"), "-.")
        api.complete(task, at("2026-10-22", 20), "2026-10-23")
        run(api, "track", at("2026-10-23", 1))
        self.assertEqual(api.log("1"), "-+.")

    def test_postponed_to_later_the_same_day_and_done(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        task["due"]["date"] = "2026-10-21T20:00:00"
        api.complete(task, at("2026-10-21", 21), "2026-10-22")
        run(api, "track", at("2026-10-22", 1))
        self.assertEqual(api.log("1"), "+.")

    def test_weekly_postponed_and_done_before_the_next_is_late(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-25", 1))
        task["due"]["date"] = "2026-10-27"
        api.complete(task, at("2026-10-27", 10), "2026-11-01")
        run(api, "track", at("2026-10-28", 1))
        self.assertEqual(api.log("1"), "~...")

    def test_every_two_days_postponed_by_one_is_late(self):
        task = habit("2026-10-21", every="every 2 days")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        task["due"]["date"] = "2026-10-22"
        api.complete(task, at("2026-10-22", 10), "2026-10-24")
        run(api, "track", at("2026-10-23", 1))
        self.assertEqual(api.log("1"), "~..")
        self.assertEqual(api.ledger("1").pending, d("2026-10-24"))

    def test_postponed_several_days_fails_each_day(self):
        task = habit("2026-10-21")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        task["due"]["date"] = "2026-10-24"
        for night in ("2026-10-22", "2026-10-23", "2026-10-24"):
            run(api, "track", at(night, 1))
        self.assertEqual(api.log("1"), "---.")

    def test_moved_ahead_and_done_early_is_completed(self):
        task = habit("2026-10-25", every="every sunday")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-21", 1))
        task["due"]["date"] = "2026-10-24"  # moved to Saturday ahead of time
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
        self.assertIn("`habit-tracker:v3:", api.comment_store["1"][1]["content"])

    def test_dry_run_writes_nothing(self):
        api = FakeTodoist([habit("2026-10-16")])
        run(api, "track", at("2026-10-16"), dry_run=True)
        self.assertEqual(api.writes, 0)

    def test_completions_of_other_tasks_are_ignored(self):
        api = Todoist("token")
        extra = {"completed_due_date_local": "2026-10-16T23:59:59", "due_date": "2026-10-17T21:59:59.000000Z"}
        events = [
            {"object_id": "1", "event_type": "completed", "event_date": "2026-10-16T15:05:26.325096Z", "extra_data": extra},
            {"object_id": "2", "event_type": "completed", "event_date": "2026-10-16T11:30:52.755048Z", "extra_data": extra},
        ]
        api._paginate = lambda path, params: iter(events)
        self.assertEqual(
            api.completions("1", d("2026-10-15")),
            [
                (
                    datetime(2026, 10, 16, 15, 5, 26, 325096, tzinfo=timezone.utc),
                    d("2026-10-16"),
                    datetime(2026, 10, 17, 21, 59, 59, tzinfo=timezone.utc),
                )
            ],
        )

    def test_data_line_round_trip(self):
        for pending in (None, d("2026-01-06")):
            ledger = Ledger(d("2026-01-02"), pending, datetime(2026, 1, 5, 6, 30, tzinfo=timezone.utc), "+-.~+")
            self.assertTrue(DATA_RE.search(ledger.serialize()))
            self.assertEqual(Ledger.parse("text " + ledger.serialize(), TZ), ledger)


if __name__ == "__main__":
    unittest.main()

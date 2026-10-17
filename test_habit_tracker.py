import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from habit_tracker import (
    DATA_RE,
    Ledger,
    Schedule,
    Todoist,
    compute_stats,
    overdue_since,
    parse_schedule,
    run,
    scheduled_days,
)

TZ = ZoneInfo("Europe/Warsaw")


def at(day: str, hour: int = 7) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00").replace(tzinfo=TZ)


class FakeTodoist:
    def __init__(self, tasks):
        self.tasks = tasks
        self.completions = {}  # task_id -> [utc datetime]
        self.comment_store = {}  # task_id -> [comment]
        self.writes = 0

    def tasks_with_label(self, label):
        return [t for t in self.tasks if label in t.get("labels", [])]

    def completion_times(self, task_id, since):
        return [c for c in self.completions.get(task_id, []) if c.date() >= since]

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

    def complete(self, task_id, local_dt: datetime):
        self.completions.setdefault(task_id, []).append(local_dt.astimezone(timezone.utc))

    def ledger(self, task_id) -> Ledger:
        return Ledger.parse(self.comment_store[task_id][-1]["content"])


def habit(due: str, task_id="1", labels=("track",), recurring=True, every="every day"):
    return {
        "id": task_id,
        "content": f"habit {task_id}",
        "labels": list(labels),
        "due": {"date": due, "is_recurring": recurring, "string": every},
    }


def days(*isos):
    return [date.fromisoformat(d) for d in isos]


class OverdueTest(unittest.TestCase):
    def test_full_day_date(self):
        now = at("2026-10-16")
        self.assertIsNone(overdue_since({"date": "2026-10-16"}, now))
        self.assertEqual(overdue_since({"date": "2026-10-14"}, now), date(2026, 10, 14))

    def test_floating_datetime(self):
        now = at("2026-10-16", 10)
        self.assertEqual(overdue_since({"date": "2026-10-16T09:00:00"}, now), date(2026, 10, 16))
        self.assertIsNone(overdue_since({"date": "2026-10-16T11:00:00"}, now))

    def test_utc_datetime_uses_local_day(self):
        # 23:30 UTC on the 15th is 01:30 on the 16th in Warsaw.
        now = at("2026-10-16", 10)
        due = {"date": "2026-10-15T23:30:00.000000Z"}
        self.assertEqual(overdue_since(due, now), date(2026, 10, 16))


class ScheduleTest(unittest.TestCase):
    def test_parse(self):
        cases = {
            "every day at 8pm": Schedule("day"),
            "every! 3 days": Schedule("day", 3),
            "every other day": Schedule("day", 2),
            "every week": Schedule("week"),
            "every monday": Schedule("week", 1, frozenset({0})),
            "every mon, wed, fri 7am": Schedule("week", 1, frozenset({0, 2, 4})),
            "every other monday": Schedule("week", 2, frozenset({0})),
            "every 2 weeks on tue and thu": Schedule("week", 2, frozenset({1, 3})),
            "every weekday": Schedule("week", 1, frozenset(range(5))),
            "every 15th": Schedule("month"),
            "every 2nd monday": Schedule("month"),
            "every 3 months": Schedule("month", 3),
            "every jan 1": Schedule("year"),
            "every day starting oct 1": Schedule("day"),
            "every 2nd tuesday of the quarter": None,
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(parse_schedule(text), expected)

    def test_weekly_overdue_for_a_few_days_is_one_occurrence(self):
        weekly = parse_schedule("every monday")
        self.assertEqual(scheduled_days(weekly, *days("2026-10-12", "2026-10-15")), days("2026-10-12"))
        self.assertEqual(
            scheduled_days(weekly, *days("2026-10-12", "2026-10-19")), days("2026-10-12", "2026-10-19")
        )

    def test_weekday_sets_and_intervals(self):
        self.assertEqual(
            scheduled_days(parse_schedule("every mon, thu"), *days("2026-10-12", "2026-10-18")),
            days("2026-10-12", "2026-10-15"),
        )
        self.assertEqual(
            scheduled_days(parse_schedule("every other monday"), *days("2026-10-12", "2026-11-02")),
            days("2026-10-12", "2026-10-26"),
        )
        self.assertEqual(
            scheduled_days(parse_schedule("every weekday"), *days("2026-10-16", "2026-10-20")),
            days("2026-10-16", "2026-10-19", "2026-10-20"),
        )

    def test_monthly_clamps_to_month_end(self):
        self.assertEqual(
            scheduled_days(parse_schedule("every 31st"), *days("2026-01-31", "2026-03-31")),
            days("2026-01-31", "2026-02-28", "2026-03-31"),
        )


class StatsTest(unittest.TestCase):
    def test_streaks_ignore_empty_days(self):
        s = compute_stats(Ledger(date(2026, 10, 1), date(2026, 10, 9), "++-+.+.+."))
        self.assertEqual((s.days_tracked, s.completed, s.failed), (9, 5, 1))
        self.assertEqual((s.streak, s.streak_kind, s.best_streak), (3, "+", 3))
        self.assertEqual(s.rate, 83)

    def test_failed_streak(self):
        s = compute_stats(Ledger(date(2026, 10, 1), date(2026, 10, 4), "+--."))
        self.assertEqual((s.streak, s.streak_kind, s.best_streak), (2, "-", 1))

    def test_no_outcomes(self):
        s = compute_stats(Ledger(date(2026, 10, 1), date(2026, 10, 1), "."))
        self.assertIsNone(s.rate)
        self.assertIsNone(s.streak_kind)


class RunTest(unittest.TestCase):
    def test_skips_untracked_and_non_recurring(self):
        api = FakeTodoist([habit("2026-10-16", "1", labels=()), habit("2026-10-16", "2", recurring=False)])
        run(api, "track", at("2026-10-16"))
        self.assertEqual(api.comment_store, {})

    def test_first_run_starts_tracking_today(self):
        api = FakeTodoist([habit("2026-10-16")])
        api.complete("1", at("2026-10-10", 20))  # completion before tracking: ignored
        run(api, "track", at("2026-10-16"))
        ledger = api.ledger("1")
        self.assertEqual((ledger.since, ledger.log), (date(2026, 10, 16), "."))

    def test_first_run_counts_existing_overdue_days(self):
        api = FakeTodoist([habit("2026-10-12")])
        run(api, "track", at("2026-10-16"))
        ledger = api.ledger("1")
        self.assertEqual((ledger.since, ledger.log), (date(2026, 10, 12), "----."))

    def test_daily_habit_over_several_runs(self):
        task = habit("2026-10-16")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-16"))  # due today: no outcome yet

        api.complete("1", at("2026-10-16", 21))
        task["due"]["date"] = "2026-10-17"
        run(api, "track", at("2026-10-17"))

        # Skipped the 17th: the run on the 18th sees it overdue since the 17th.
        run(api, "track", at("2026-10-18"))
        self.assertEqual(api.ledger("1").log, "+-.")

        # Completed late on the 18th: that day flips to completed.
        api.complete("1", at("2026-10-18", 22))
        task["due"]["date"] = "2026-10-19"
        run(api, "track", at("2026-10-19"))

        self.assertEqual(api.ledger("1").log, "+-+.")
        self.assertEqual(len(api.comment_store["1"]), 1)  # updated in place, not re-added
        content = api.comment_store["1"][0]["content"]
        self.assertIn("Tracking for: **4 days**", content)
        self.assertIn("**67%** (2 completed / 1 failed)", content)
        self.assertIn("Current streak: **1 day completed**", content)

    def test_missed_runs_leave_gaps_and_overdue_fills_them(self):
        task = habit("2026-10-16")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-16"))
        # No runs for three days, task never done: next run backfills failures.
        run(api, "track", at("2026-10-20"))
        self.assertEqual(api.ledger("1").log, "----.")

    def test_weekly_habit_missed_then_done_late(self):
        task = habit("2026-10-12", every="every monday")
        api = FakeTodoist([task])
        for night in ("2026-10-13", "2026-10-14", "2026-10-15"):
            run(api, "track", at(night, 1))
        api.complete("1", at("2026-10-15", 18))  # done on Thursday
        task["due"]["date"] = "2026-10-19"
        run(api, "track", at("2026-10-16", 1))
        self.assertEqual(api.ledger("1").log, "-..+.")

    def test_unreadable_schedule_fails_only_the_due_date(self):
        api = FakeTodoist([habit("2026-10-12", every="every 2nd tuesday of the quarter")])
        run(api, "track", at("2026-10-16"))
        self.assertEqual(api.ledger("1").log, "-....")

    def test_failure_is_final(self):
        task = habit("2026-10-16T08:00:00")
        api = FakeTodoist([task])
        run(api, "track", at("2026-10-16", 10))  # due time passed: today fails
        api.complete("1", at("2026-10-16", 11))
        task["due"]["date"] = "2026-10-17T08:00:00"
        run(api, "track", at("2026-10-16", 12))
        self.assertEqual(api.ledger("1").log, "-")

    def test_test_task_overdue_with_completions_today(self):
        # Completed several times today, then moved back to yesterday.
        api = FakeTodoist([habit("2026-10-15")])
        for minute in (5, 9, 10, 15):
            api.complete("1", at("2026-10-16", 17).replace(minute=minute))
        run(api, "track", at("2026-10-16", 17))
        self.assertEqual(api.ledger("1").log, "-+")

    def test_preserves_other_comments_and_recovers_ledger(self):
        api = FakeTodoist([habit("2026-10-20")])
        old = Ledger(date(2026, 10, 10), date(2026, 10, 15), "++-+++")
        api.comment_store["1"] = [
            {"id": "user", "content": "my own note"},
            {"id": "stats", "content": "📊 old\n" + old.serialize()},
        ]
        api.complete("1", at("2026-10-16", 9))
        run(api, "track", at("2026-10-17"))
        self.assertEqual(api.comment_store["1"][0]["content"], "my own note")
        self.assertEqual(api.ledger("1").log, "++-++++.")

    def test_dry_run_writes_nothing(self):
        api = FakeTodoist([habit("2026-10-16")])
        run(api, "track", at("2026-10-16"), dry_run=True)
        self.assertEqual(api.writes, 0)

    def test_completions_of_other_tasks_are_ignored(self):
        api = Todoist("token")
        events = [
            {"object_id": "1", "event_type": "completed", "event_date": "2026-10-16T15:05:26.325096Z"},
            {"object_id": "2", "event_type": "completed", "event_date": "2026-10-16T11:30:52.755048Z"},
        ]
        api._paginate = lambda path, params: iter(events)
        times = api.completion_times("1", date(2026, 10, 15))
        self.assertEqual(times, [datetime(2026, 10, 16, 15, 5, 26, 325096, tzinfo=timezone.utc)])

    def test_data_line_round_trip(self):
        ledger = Ledger(date(2026, 1, 2), date(2026, 1, 5), "+-.+")
        self.assertTrue(DATA_RE.search(ledger.serialize()))
        self.assertEqual(Ledger.parse("text " + ledger.serialize()), ledger)


if __name__ == "__main__":
    unittest.main()

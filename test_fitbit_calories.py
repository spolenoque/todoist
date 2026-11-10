import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import fitbit_calories
from fitbit_calories import TITLE, GoogleHealth, render, save_to_env, save_to_github, update
from habit_tracker import Event

TZ = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 10, 23, 5, tzinfo=TZ)  # a Tuesday, an hour before the day ends
TODAY = date(2026, 11, 10)
GOAL = 3500


def calories(*full_days, today=None):
    """Calories for the full days up to yesterday, oldest first, and today so
    far; None for no data."""
    result = {TODAY - timedelta(days=len(full_days) - i): v for i, v in enumerate(full_days) if v is not None}
    if today is not None:
        result[TODAY] = today
    return result


def utc(local: datetime) -> datetime:
    return local.astimezone(timezone.utc)


class FakeHealth:
    def __init__(self, calories):
        self.calories = calories
        self.asked = []

    def daily_calories(self, first, last):
        self.asked.append((first, last))
        return {d: v for d, v in self.calories.items() if first <= d <= last}


class FakeTodoist:
    def __init__(self, comments=(), due="2026-11-10"):
        self.comments_list = list(comments)
        self.due = due
        self.history = []  # [Event]
        self.closed = 0
        self.writes = 0

    def task(self, task_id):
        return {
            "id": task_id,
            "content": "Физически поактивничать",
            "due": {"date": self.due, "string": "every day", "is_recurring": True},
        }

    def events(self, task_id, since):
        return [e for e in self.history if e.at.date() >= since]

    def close_task(self, task_id):
        """Like Todoist: the completion is logged and the task moves on a day."""
        self.closed += 1
        due = date.fromisoformat(self.due)
        self.history.append(Event(utc(NOW), "completed", due))
        self.due = (due + timedelta(days=1)).isoformat()

    def comments(self, task_id):
        return self.comments_list

    def add_comment(self, task_id, content):
        self.writes += 1
        self.comments_list.append({"id": f"c{len(self.comments_list)}", "content": content})

    def update_comment(self, comment_id, content):
        self.writes += 1
        for c in self.comments_list:
            if c["id"] == comment_id:
                c["content"] = content


def quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


class RenderTest(unittest.TestCase):
    def test_render(self):
        data = calories(2180.4, 2301, 2455, 1998, 2612, 2250, 2411.6, today=3612.2)
        self.assertEqual(
            render(data, NOW, GOAL),
            "\n".join(
                [
                    TITLE,
                    "Tue 10 Nov, by 23:05: **3,612 kcal**",
                    "Goal: over 3,500 kcal ✅",
                    "7-day average: 2,315 kcal",
                    "Last 7 days: 22 23 25 20 26 23 24",
                ]
            ),
        )

    def test_last_days_in_hundreds_with_the_days_over_the_goal_in_bold(self):
        data = calories(2657, 4915, 2739, 2776, 3051, 3990, 2298)
        self.assertIn("Last 7 days: 27 **49** 27 28 31 **40** 23", render(data, NOW, GOAL))
        self.assertIn("Last 7 days: 27 49 27 28 31 40 23", render(data, NOW))  # no goal, no bold

    def test_exactly_the_goal_is_not_a_hit_and_halves_round_up(self):
        self.assertIn("Last 7 days: — — — — 35 26 —", render(calories(3500, 2550, None), NOW, GOAL))

    def test_goal_not_reached_yet(self):
        self.assertIn("Goal: over 3,500 kcal, 288 to go", render(calories(2000, today=3212.4), NOW, GOAL))
        self.assertIn("Goal: over 3,500 kcal, 0 to go", render(calories(2000, today=3500), NOW, GOAL))

    def test_without_a_goal_there_is_no_goal_line(self):
        self.assertNotIn("Goal", render(calories(2000, today=3000), NOW))

    def test_has_no_links_for_todoist_to_attach_a_preview_card_to(self):
        self.assertNotIn("://", render(calories(2000, today=3000), NOW, GOAL))

    def test_days_without_data_are_left_out_of_the_average(self):
        content = render(calories(2000, None, 3000, None, None, None, None), NOW, GOAL)
        self.assertIn("Tue 10 Nov, by 23:05: — (no data yet)", content)
        self.assertIn("Goal: over 3,500 kcal, 3,500 to go", content)
        self.assertIn("7-day average: 2,500 kcal", content)
        self.assertIn("Last 7 days: 20 — 30 — — — —", content)

    def test_no_data_at_all(self):
        self.assertIn("7-day average: —", render({}, NOW))


class UpdateTest(unittest.TestCase):
    def test_reads_today_and_the_seven_full_days_before(self):
        health = FakeHealth({})
        quiet(update, FakeTodoist(), health, "t1", NOW)
        self.assertEqual(health.asked, [(date(2026, 11, 3), TODAY)])

    def test_adds_the_comment_once_then_keeps_it_up_to_date(self):
        api = FakeTodoist()
        quiet(update, api, FakeHealth(calories(today=1000)), "t1", NOW - timedelta(hours=3))
        quiet(update, api, FakeHealth(calories(today=2500)), "t1", NOW)
        self.assertEqual(len(api.comments_list), 1)
        self.assertIn("Tue 10 Nov, by 23:05: **2,500 kcal**", api.comments_list[0]["content"])

    def test_leaves_the_habit_stats_comment_alone(self):
        stats = {"id": "s", "content": "[📊](habit://habit-tracker:v4:…) **Habit stats**"}
        api = FakeTodoist([dict(stats)])
        quiet(update, api, FakeHealth(calories(today=2000)), "t1", NOW)
        self.assertEqual(api.comments_list[0], stats)
        self.assertTrue(api.comments_list[1]["content"].startswith(TITLE))

    def test_a_real_run_keeps_the_numbers_out_of_the_public_log(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            update(FakeTodoist(), FakeHealth(calories(2000, today=3600)), "t1", NOW, goal=GOAL)
        self.assertEqual(out.getvalue(), "updated: Физически поактивничать\n")

    def test_dry_run_writes_nothing_and_completes_nothing(self):
        api = FakeTodoist()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            update(api, FakeHealth(calories(today=3600)), "t1", NOW, dry_run=True, goal=GOAL)
        self.assertEqual((api.writes, api.closed), (0, 0))
        self.assertIn("It would be marked done for today.", out.getvalue())


class CompleteTest(unittest.TestCase):
    def run_at(self, api, today_kcal, goal=GOAL, now=NOW):
        quiet(update, api, FakeHealth(calories(today=today_kcal)), "t1", now, goal=goal)

    def test_over_the_goal_marks_it_done_once(self):
        api = FakeTodoist()
        self.run_at(api, 3501)
        self.run_at(api, 3700, now=NOW + timedelta(minutes=30))
        self.assertEqual(api.closed, 1)
        self.assertEqual(api.due, "2026-11-11")
        self.assertIn("Goal: over 3,500 kcal ✅", api.comments_list[0]["content"])

    def test_at_or_under_the_goal_leaves_it_open(self):
        api = FakeTodoist()
        self.run_at(api, 3500)
        self.run_at(api, 1200)
        self.assertEqual(api.closed, 0)

    def test_without_a_goal_it_never_marks_it_done(self):
        api = FakeTodoist()
        self.run_at(api, 5000, goal=None)
        self.assertEqual(api.closed, 0)

    def test_already_done_today_is_left_alone(self):
        api = FakeTodoist(due="2026-11-11")
        api.history.append(Event(utc(NOW - timedelta(hours=5)), "completed", TODAY))
        self.run_at(api, 3600)
        self.assertEqual(api.closed, 0)

    def test_a_completion_you_took_back_today_is_not_redone(self):
        api = FakeTodoist(due="2026-11-10")  # undone, so it's back on today
        api.history += [
            Event(utc(NOW - timedelta(hours=2)), "completed", TODAY),
            Event(utc(NOW - timedelta(hours=1)), "uncompleted"),
        ]
        self.run_at(api, 3600)
        self.assertEqual(api.closed, 0)

    def test_a_completion_yesterday_doesnt_count_for_today(self):
        api = FakeTodoist(due="2026-11-10")
        api.history.append(Event(utc(NOW - timedelta(days=1)), "completed", TODAY - timedelta(days=1)))
        self.run_at(api, 3600)
        self.assertEqual(api.closed, 1)

    def test_an_overdue_task_is_marked_done_today(self):
        api = FakeTodoist(due="2026-11-09")
        self.run_at(api, 3600)
        self.assertEqual(api.closed, 1)

    def test_a_task_done_ahead_isnt_done_again(self):
        api = FakeTodoist(due="2026-11-12")
        self.run_at(api, 3600)
        self.assertEqual(api.closed, 0)


class AuthorizeTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.env = Path(folder.name, ".env")

    def test_replaces_the_token_and_keeps_the_other_lines(self):
        self.env.write_text("TODOIST_API_TOKEN=t\nGOOGLE_REFRESH_TOKEN=old\n# note\n")
        save_to_env(self.env, "GOOGLE_REFRESH_TOKEN", "new")
        self.assertEqual(self.env.read_text(), "TODOIST_API_TOKEN=t\nGOOGLE_REFRESH_TOKEN=new\n# note\n")

    def test_adds_the_token_when_there_is_none(self):
        self.env.write_text("TODOIST_API_TOKEN=t")
        save_to_env(self.env, "GOOGLE_REFRESH_TOKEN", "new")
        self.assertEqual(self.env.read_text(), "TODOIST_API_TOKEN=t\nGOOGLE_REFRESH_TOKEN=new\n")

    def test_sets_the_github_secret_through_stdin(self):
        with mock.patch.object(subprocess, "run") as run:
            self.assertTrue(save_to_github("GOOGLE_REFRESH_TOKEN", "new"))
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["gh", "secret", "set", "GOOGLE_REFRESH_TOKEN"])
        self.assertEqual(kwargs["input"], "new")

    def test_without_the_github_cli_it_says_so(self):
        for error in (FileNotFoundError("gh"), subprocess.CalledProcessError(1, "gh")):
            with mock.patch.object(subprocess, "run", side_effect=error):
                self.assertFalse(save_to_github("GOOGLE_REFRESH_TOKEN", "new"))

    def test_authorize_saves_the_token_without_showing_it(self):
        self.env.write_text("GOOGLE_CLIENT_ID=id\nGOOGLE_CLIENT_SECRET=secret\nGOOGLE_REFRESH_TOKEN=old\n")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(fitbit_calories, "ENV_FILE", self.env), \
                mock.patch.object(fitbit_calories, "authorize", return_value=("1//new-token", 604799)), \
                mock.patch.object(fitbit_calories, "save_to_github", return_value=True) as github, \
                mock.patch.object(GoogleHealth, "daily_calories", return_value={}), \
                mock.patch("sys.argv", ["fitbit_calories.py", "--authorize"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(fitbit_calories.main(), 0)
        self.assertIn("GOOGLE_REFRESH_TOKEN=1//new-token\n", self.env.read_text())
        github.assert_called_once_with("GOOGLE_REFRESH_TOKEN", "1//new-token")
        self.assertNotIn("new-token", out.getvalue())
        self.assertIn("Google signs it out on", out.getvalue())


class GoogleHealthTest(unittest.TestCase):
    def fake_http(self, pages):
        calls = []

        def http_json(method, url, headers, data=None):
            body = data.decode()
            calls.append((url, headers, body))
            if url == fitbit_calories.TOKEN_URL:
                return {"access_token": "access", "expires_in": 3599}
            return pages.pop(0)

        return calls, mock.patch.object(fitbit_calories, "http_json", http_json)

    def test_reads_daily_totals(self):
        def point(day, kcal=None):
            p = {"civilStartTime": {"date": {"year": 2026, "month": 11, "day": day}}}
            if kcal is not None:
                p["totalCalories"] = {"kcalSum": kcal}
            return p

        pages = [
            {"rollupDataPoints": [point(1, 2250.5), point(2, 0)], "nextPageToken": "next"},
            {"rollupDataPoints": [point(3)]},
        ]
        calls, patch = self.fake_http(pages)
        with patch:
            result = GoogleHealth("id", "secret", "refresh").daily_calories(date(2026, 11, 1), date(2026, 11, 3))
        self.assertEqual(result, {date(2026, 11, 1): 2250.5})  # 0 kcal and missing mean no data

        token_url, _, token_body = calls[0]
        self.assertEqual(token_url, fitbit_calories.TOKEN_URL)
        self.assertIn("grant_type=refresh_token", token_body)
        self.assertIn("refresh_token=refresh", token_body)

        url, headers, body = calls[1]
        self.assertEqual(
            url, "https://health.googleapis.com/v4/users/me/dataTypes/total-calories/dataPoints:dailyRollUp"
        )
        self.assertEqual(headers["Authorization"], "Bearer access")
        self.assertEqual(
            json.loads(body),
            {
                "range": {
                    "start": {"date": {"year": 2026, "month": 11, "day": 1}},
                    "end": {"date": {"year": 2026, "month": 11, "day": 4}},  # the end is exclusive
                },
                "windowSizeDays": 1,
                "pageSize": 14,
            },
        )
        self.assertEqual(json.loads(calls[2][2])["pageToken"], "next")
        self.assertEqual(len(calls), 3)  # the access token is fetched once

    def test_an_expired_refresh_token_says_what_to_do(self):
        def http_json(*args, **kwargs):
            raise RuntimeError('POST /token -> HTTP 400: {"error": "invalid_grant"}')

        with mock.patch.object(fitbit_calories, "http_json", http_json):
            with self.assertRaisesRegex(RuntimeError, "--authorize"):
                GoogleHealth("id", "secret", "old").access_token()


if __name__ == "__main__":
    unittest.main()

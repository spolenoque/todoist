import contextlib
import io
import json
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import fitbit_calories
from fitbit_calories import TITLE, GoogleHealth, render, update

TZ = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 3, 1, 15, tzinfo=TZ)  # a Tuesday, just after midnight
YESTERDAY = date(2026, 11, 2)


def days_back(*values):
    """Calories for the days up to yesterday, oldest first; None for no data."""
    return {
        YESTERDAY - timedelta(days=len(values) - 1 - i): v for i, v in enumerate(values) if v is not None
    }


class FakeHealth:
    def __init__(self, calories):
        self.calories = calories
        self.asked = []

    def daily_calories(self, first, last):
        self.asked.append((first, last))
        return {d: v for d, v in self.calories.items() if first <= d <= last}


class FakeTodoist:
    def __init__(self, comments=()):
        self.comments_list = list(comments)
        self.writes = 0

    def task(self, task_id):
        return {"id": task_id, "content": "Физически поактивничать"}

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
        calories = days_back(2180.4, 2301, 2455, 1998, 2612, 2250, 2411.6)
        self.assertEqual(
            render(calories, YESTERDAY),
            "\n".join(
                [
                    TITLE,
                    "Mon 2 Nov: **2,412 kcal**",
                    "7-day average: 2,315 kcal",
                    "Last 7 days: 2,180 · 2,301 · 2,455 · 1,998 · 2,612 · 2,250 · 2,412",
                ]
            ),
        )

    def test_has_no_links_for_todoist_to_attach_a_preview_card_to(self):
        self.assertNotIn("://", render(days_back(2000), YESTERDAY))

    def test_days_without_data_are_left_out_of_the_average(self):
        content = render(days_back(2000, None, 3000, None, None, None, None), YESTERDAY)
        self.assertIn("Mon 2 Nov: — (no data yet)", content)
        self.assertIn("7-day average: 2,500 kcal", content)
        self.assertIn("Last 7 days: 2,000 · — · 3,000 · — · — · — · —", content)

    def test_no_data_at_all(self):
        self.assertIn("7-day average: —", render({}, YESTERDAY))


class UpdateTest(unittest.TestCase):
    def test_reads_the_last_seven_full_days(self):
        health = FakeHealth({})
        quiet(update, FakeTodoist(), health, "t1", NOW)
        self.assertEqual(health.asked, [(date(2026, 10, 27), YESTERDAY)])

    def test_adds_the_comment_once_then_keeps_it_up_to_date(self):
        api = FakeTodoist()
        quiet(update, api, FakeHealth(days_back(2000)), "t1", NOW)
        quiet(update, api, FakeHealth({YESTERDAY: 2000, date(2026, 11, 3): 2500}), "t1", NOW + timedelta(days=1))
        self.assertEqual(len(api.comments_list), 1)
        self.assertIn("Tue 3 Nov: **2,500 kcal**", api.comments_list[0]["content"])

    def test_leaves_the_habit_stats_comment_alone(self):
        stats = {"id": "s", "content": "[📊](https://github.com/spolenoque/todoist#habit-tracker:v4:…) **Habit stats**"}
        api = FakeTodoist([dict(stats)])
        quiet(update, api, FakeHealth(days_back(2000)), "t1", NOW)
        self.assertEqual(api.comments_list[0], stats)
        self.assertTrue(api.comments_list[1]["content"].startswith(TITLE))

    def test_a_real_run_keeps_the_numbers_out_of_the_public_log(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            update(FakeTodoist(), FakeHealth(days_back(2000)), "t1", NOW)
        self.assertEqual(out.getvalue(), "updated: Физически поактивничать\n")

    def test_dry_run_writes_nothing(self):
        api = FakeTodoist()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            update(api, FakeHealth(days_back(2000)), "t1", NOW, dry_run=True)
        self.assertEqual(api.writes, 0)
        self.assertIn("Mon 2 Nov: **2,000 kcal**", out.getvalue())


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

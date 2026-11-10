#!/usr/bin/env python3
"""Calories burned, from a Fitbit through the Google Health API.

Keeps one comment on a Todoist task up to date with the total calories burned
(basal plus active, as Google Health shows them) today so far and on each of
the last 7 full days:

  🔥 Calories burned
  Tue 10 Nov, by 23:05: 3,612 kcal
  Goal: over 3,500 kcal ✅
  7-day average: 2,315 kcal
  Last 7 days: 2,180 · 2,301 · 2,455 · 1,998 · 2,612 · 2,250 · 2,412

With --complete-above, it also marks the task done for today once today's
calories are over that many, unless someone completed it today already (even
if they took that back). Every run reads all the days again, so a day that
hadn't finished syncing from the phone yet is corrected on the next run.

Usage:
  python3 fitbit_calories.py --authorize             # once: sign in to Google
  python3 fitbit_calories.py --task ID_OR_LINK [--complete-above KCAL] [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from habit_tracker import Todoist, due_day, http_json, load_dotenv, resolve_timezone, task_id_from

HEALTH_URL = "https://health.googleapis.com/v4"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly"
DAYS = 7  # total-calories rollups cover at most 14 days per request
ENV_FILE = Path(__file__).with_name(".env")
# Also how the script finds its comment. No links: Todoist attaches a preview
# card to them.
TITLE = "🔥 **Calories burned**"


class GoogleHealth:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token = None

    def access_token(self) -> str:
        if self._access_token is None:
            form = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }
            try:
                self._access_token = post_form(TOKEN_URL, form)["access_token"]
            except RuntimeError as e:
                if "invalid_grant" in str(e):
                    raise RuntimeError(
                        "Google no longer accepts GOOGLE_REFRESH_TOKEN: it was revoked, or it expired because "
                        "the Google Cloud app is still in Testing (tokens last 7 days there). Publish the app, "
                        "then run `python3 fitbit_calories.py --authorize` again."
                    ) from e
                raise
        return self._access_token

    def daily_calories(self, first: date, last: date) -> dict[date, float]:
        """Total kcal burned on each day from `first` to `last`, in the
        user's local time. Days without data are left out."""
        url = f"{HEALTH_URL}/users/me/dataTypes/total-calories/dataPoints:dailyRollUp"
        headers = {"Authorization": f"Bearer {self.access_token()}", "Content-Type": "application/json"}
        body = {
            "range": {"start": civil(first), "end": civil(last + timedelta(days=1))},
            "windowSizeDays": 1,
            "pageSize": 14,
        }
        result = {}
        while True:
            page = http_json("POST", url, headers, json.dumps(body).encode()) or {}
            for point in page.get("rollupDataPoints", []):
                d = point["civilStartTime"]["date"]
                value = (point.get("totalCalories") or {}).get("kcalSum")
                if value:  # a day can't burn 0 kcal: that's a day without data
                    result[date(d["year"], d["month"], d["day"])] = value
            if not page.get("nextPageToken"):
                return result
            body["pageToken"] = page["nextPageToken"]


def civil(day: date) -> dict:
    """Midnight starting `day`, as a CivilDateTime."""
    return {"date": {"year": day.year, "month": day.month, "day": day.day}}


def post_form(url: str, form: dict) -> dict:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    return http_json("POST", url, headers, urllib.parse.urlencode(form).encode())


def kcal(value: float | None) -> str:
    return f"{round(value):,}" if value else "—"


def render(calories: dict[date, float], now: datetime, goal: int | None = None) -> str:
    today = now.date()
    days = [today - timedelta(days=k) for k in range(DAYS, 0, -1)]  # the full days before today
    values = [calories.get(day) for day in days]
    known = [v for v in values if v]
    average = f"{kcal(sum(known) / len(known))} kcal" if known else "—"
    so_far = calories.get(today) or 0
    lines = [
        TITLE,
        f"{today:%a} {today.day} {today:%b}, by {now:%H:%M}: "
        + (f"**{kcal(so_far)} kcal**" if so_far else "— (no data yet)"),
    ]
    if goal:
        progress = " ✅" if so_far > goal else f", {round(goal - so_far):,} to go"
        lines.append(f"Goal: over {goal:,} kcal{progress}")
    lines += [
        f"{DAYS}-day average: {average}",
        f"Last {DAYS} days: " + " · ".join(kcal(v) for v in values),
    ]
    return "\n".join(lines)


def open_today(api, task: dict, now: datetime) -> bool:
    """The task has an occurrence due today or earlier, and nobody completed
    it today: not an earlier run, and not you, even if you took it back."""
    tz, today = now.tzinfo, now.date()
    if not task.get("due") or due_day(task["due"], tz) > today:
        return False
    events = api.events(task["id"], today - timedelta(days=1))
    return not any(e.kind == "completed" and e.at.astimezone(tz).date() == today for e in events)


def update(api, health, task_id: str, now: datetime, dry_run: bool = False, goal: int | None = None) -> str:
    """Write today's calories so far and those of the last full days into the
    task's comment. With a `goal`, mark the task done for today once today's
    calories are over it."""
    task = api.task(task_id)
    today = now.date()
    calories = health.daily_calories(today - timedelta(days=DAYS), today)
    content = render(calories, now, goal)
    complete = bool(goal) and (calories.get(today) or 0) > goal and open_today(api, task, now)
    ours = [c for c in api.comments(task_id) if c.get("content", "").startswith(TITLE)]
    if dry_run:
        print(f"--- {task['content']} ({task_id})\n{content}\n")
        if complete:
            print("It would be marked done for today.\n")
        return content
    if complete:
        api.close_task(task_id)
    if ours:
        api.update_comment(ours[-1]["id"], content)
    else:
        api.add_comment(task_id, content)
    # Just the name: the workflow logs of this repository are public.
    print(f"updated: {task['content']}")
    return content


def save_to_env(path: Path, name: str, value: str):
    """Set `name` in the .env file, keeping its other lines."""
    lines = path.read_text().splitlines() if path.is_file() else []
    line = f"{name}={value}"
    for i, existing in enumerate(lines):
        if existing.partition("=")[0].strip() == name:
            lines[i] = line
            break
    else:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n")


def save_to_github(name: str, value: str) -> bool:
    """Set the repository secret with the GitHub CLI, if it's installed and
    signed in. The value goes in through stdin, so it's never shown."""
    try:
        subprocess.run(
            ["gh", "secret", "set", name], input=value, text=True,
            capture_output=True, check=True, cwd=Path(__file__).parent,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def authorize(client_id: str, client_secret: str) -> tuple[str, int | None]:
    """Sign in to Google in the browser. Returns a refresh token, and the
    seconds until Google signs it out if it does (the app is in Testing)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    replies = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" in query or "error" in query:
                replies.append(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Done, you can close this tab.".encode())

        def log_message(self, *args):
            pass

    # Desktop app clients accept any port on the loopback address.
    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",  # and a new one every time, even if access was granted before
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print(f"Sign in with the Google account you use in the Google Health app:\n\n  {url}\n")
    webbrowser.open(url)
    deadline = time.monotonic() + 300
    with server:
        # The browser may ask for other things too, like the favicon.
        while not replies and time.monotonic() < deadline:
            server.timeout = max(1, deadline - time.monotonic())
            server.handle_request()
    if not replies:
        raise RuntimeError("no answer from Google within 5 minutes")
    reply = replies[0]
    if reply.get("state") != state:
        raise RuntimeError("the sign-in answer doesn't belong to this request; run --authorize again")
    if "error" in reply:
        raise RuntimeError(f"Google sign-in failed: {reply['error']}")
    tokens = post_form(
        TOKEN_URL,
        {
            "code": reply["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
    )
    if "refresh_token" not in tokens:
        raise RuntimeError("Google didn't return a refresh token; run --authorize again")
    granted = tokens.get("scope", "").split()
    if SCOPE not in granted:
        raise RuntimeError("the activity and fitness permission wasn't granted; tick it on Google's consent screen")
    return tokens["refresh_token"], tokens.get("refresh_token_expires_in")


def main() -> int:
    load_dotenv(ENV_FILE)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--task", metavar="ID_OR_LINK", default=os.environ.get("CALORIES_TASK"),
        help="the task to keep the comment on (its ID or Todoist link; default: CALORIES_TASK)",
    )
    parser.add_argument(
        "--complete-above", metavar="KCAL", type=int,
        help="mark the task done for today once today's calories are over this many",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the comment instead of writing it")
    parser.add_argument(
        "--authorize", action="store_true",
        help="sign in to Google, and save the token in .env and the GitHub secret",
    )
    args = parser.parse_args()

    missing = [v for v in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET") if not os.environ.get(v)]
    if not args.authorize:
        missing += [v for v in ("GOOGLE_REFRESH_TOKEN", "TODOIST_API_TOKEN") if not os.environ.get(v)]
        if not args.task:
            parser.error("--task (or CALORIES_TASK) is needed to know which task gets the calories")
    if missing:
        print(f"{', '.join(missing)} not set (env vars or .env next to this script)", file=sys.stderr)
        return 2
    client_id, client_secret = os.environ["GOOGLE_CLIENT_ID"], os.environ["GOOGLE_CLIENT_SECRET"]

    try:
        if args.authorize:
            refresh_token, expires_in = authorize(client_id, client_secret)
            # Saved before anything else can fail, so the sign-in isn't wasted.
            save_to_env(ENV_FILE, "GOOGLE_REFRESH_TOKEN", refresh_token)
            if save_to_github("GOOGLE_REFRESH_TOKEN", refresh_token):
                print("Signed in. The new token is saved in .env and in the GitHub secret.")
            else:
                print(
                    "Signed in. The new token is saved in .env, but not on GitHub: the GitHub CLI isn't\n"
                    "installed or signed in. Copy the GOOGLE_REFRESH_TOKEN value from .env into the secret."
                )
            if expires_in:
                expiry = datetime.now() + timedelta(seconds=expires_in)
                print(f"Google signs it out on {expiry:%a %d %b at %H:%M}, as the app is still in Testing.")
            yesterday = date.today() - timedelta(days=1)
            calories = GoogleHealth(client_id, client_secret, refresh_token).daily_calories(yesterday, yesterday)
            print(f"Calories burned yesterday: {kcal(calories.get(yesterday))} kcal")
            return 0
        api = Todoist(os.environ["TODOIST_API_TOKEN"])
        health = GoogleHealth(client_id, client_secret, os.environ["GOOGLE_REFRESH_TOKEN"])
        now = datetime.now(resolve_timezone(api))
        update(api, health, task_id_from(args.task), now, args.dry_run, args.complete_above)
        return 0
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

# todoist

Habit tracker for Todoist. For every **recurring** task with the `@track` label,
`habit_tracker.py` keeps one comment on the task up to date:

```
📊 Habit stats
Tracking since: 02 Jul 2026
Completion rate: 72% (81/0/31)
Current streak: 8 completions 🔥
Best streak: 12 completions
Last 7 results: 🟥🟩🟩🟩🟩🟩🟩
```

The completion rate's numbers are on time / late / failed. Tasks with "test"
in their name get a detailed comment for checking the script's work: the rate
spelled out, the last 10 results, and the time of the update with the stored
data (see below).

Python 3.9+, no dependencies.

## Setup

1. Get your API token: Todoist → Settings → Integrations → Developer.
2. Put it in `.env` next to the script (git-ignored):
   ```
   TODOIST_API_TOKEN=your-token
   ```
3. Preview without writing anything:
   ```
   python3 habit_tracker.py --dry-run
   ```
4. Run it for real: `python3 habit_tracker.py`

Options: `--label NAME` (or `HABIT_LABEL`) to track a different label.
`--task ID_OR_LINK` updates just one task; it takes the task's ID or its
Todoist link, and the task still has to be a recurring tracked one.
`--rebuild --task ID_OR_LINK` recalculates that task's history from Todoist's
activity log, starting at its first completion, instead of continuing from the
last run. It only works together with `--task`. It
gives the same result every time, so it's safe to repeat. The activity log
keeps the full history on paid plans and only the last 7 days on the free one.
`HABIT_TZ=Europe/Warsaw` overrides the timezone; by default it comes from your
Todoist account settings.

## More accounts

Each account has its own token. `--account NAME` reads it from
`TODOIST_API_TOKEN_NAME` instead of `TODOIST_API_TOKEN`, e.g.
`TODOIST_API_TOKEN_LU` for `--account lu`; put it in `.env` for local runs.
Each run uses that account's timezone and `@track` label.

Tasks in shared projects show up for every member, so an account only tracks
the tasks it created; the others are left to their creators' runs. A task
someone else completes still counts for it.

## How it counts

Every occurrence of the task gets one result, recorded on the day it was
scheduled for:

- 🟩 **completed**: done on that day, or earlier. Ticking a Sunday task on
  Saturday counts for Sunday.
- 🟨 **late**: done after that day, but before the next occurrence's day.
- 🟥 **failed**: the next occurrence's day arrived and it still wasn't done.

A ⬜ after the last results marks the occurrence that's due now and not done
yet; it turns into one of the above.

Postponing a task doesn't change the day it was scheduled for. A Sunday task
moved to Tuesday and done there is late. A daily task postponed to tomorrow
fails when tomorrow arrives, and doing it tomorrow counts for tomorrow.

To take a completion back, move the task back to the day you completed it (or
earlier), or use Todoist's undo, which does the same. That occurrence is open
again, even if you move the task somewhere else afterwards: do it again to
count it, or it fails when the next one arrives. Moving
a later occurrence earlier, to a day after your last completion, is just
planning ahead and takes nothing back. When you do a task ahead of its day,
Todoist may bring it back to that day; ticking it again there doesn't count
twice.

The completion rate counts late as done and shows the split, e.g.
`80% (6/2/2)` for 6 on time, 2 late and 2 failed. A late result keeps a streak going;
only a failure breaks it. Days with no occurrence don't count.

The occurrence days come from the task's repeat text. The script understands
the common English forms: `every day`, `every other day`, `every 3 days`,
`every monday`, `every mon, wed, fri`, `every other monday`,
`every 2 weeks on tue`, `every weekday`, `every 3 months`, `every 15th`,
`every last day`, `every 2nd monday`, `every last workday`, `every jan 1`,
`every last sun feb, last sun apr, …`, and the simple Russian ones:
`каждый день`, `каждые 3 дня`, `через день`, `каждую среду`,
`каждый пн, ср и пт`, `каждые 2 недели во вт`, `каждый будний день`,
`каждые выходные`, `каждый месяц`. Times and `starting`/`until` parts are
ignored. Hourly repeats (`every 12 hours`) aren't supported. If it can't read
the repeat text, it can't know when the next
occurrence is: an occurrence then fails the day after it was due, and there is
no late.

Tracking starts on the first run that sees the task, or at its due date if
it's already overdue by then. Completions from before tracking started are
ignored.

Todoist doesn't record which occurrence is still open, so the per-day results,
the open occurrence, the time of the last check and the day of the last
completion are stored in the stats comment itself: in the link behind the 📊
(it leads nowhere, because Todoist adds a preview card to web links), or in the
backtick line at the end of a detailed comment. Don't edit them. If you delete
the comment, tracking starts over.

## Scheduling

Run the script once a day, shortly after midnight, so it sees yesterday's
misses as overdue before you complete or reschedule them.

It runs nightly on GitHub Actions (`.github/workflows/habit-tracker.yml`) at
00:15 UTC, which is 01:15/02:15 in Madrid. To set it up, add your token as a
repository secret named `TODOIST_API_TOKEN` (Settings → Secrets and variables
→ Actions). Each extra account gets its own secret and its own step in the
workflow, like the one for `lu`. To run it by hand, open Actions → Habit tracker → Run workflow, and
tick "dry run" to only print the comments.

If your timezone is far from UTC+1/+2, change the cron line so the run lands
just after your local midnight.

Recurring completions are read from Todoist's activity log. On the free plan
that log only goes back 7 days, so completions made during a gap of more than
a week between runs won't be counted.

## Calories from Fitbit

`fitbit_calories.py` keeps one more comment on a task, with the calories your
Fitbit counted today so far and on each of the last 7 full days. With
`--complete-above 3500`, it also marks the task done for today once today is
over 3,500 kcal:

```
🔥 Calories burned
Tue 10 Nov, by 23:05: 3,612 kcal
Goal: over 3,500 kcal ✅
7-day average: 3,204 kcal
Last 7 days: 27 49 27 28 31 40 23
```

The last 7 days are in hundreds of kcal, and the days over the goal are in
bold (49 and 40 here).

These are total calories, resting plus active, the number the Google Health
app shows. They come from the Google Health API, which replaced the Fitbit Web
API in 2026. Every run reads all the days again, so a day that hadn't finished
syncing from the phone yet is corrected on the next run. A `—` is a day
without data; it doesn't count towards the average.

It only marks the task done while today's occurrence is open. It leaves the
task alone when you've done it ahead, or when it was completed today already,
by you or an earlier run. That includes a completion you took back, so undoing
it in Todoist sticks. An overdue task counts for today, as if you'd ticked it
yourself.

### Setup

You need a Google Cloud app of your own. It only has to work for you, so it
doesn't need Google's verification.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project and enable the
   [Google Health API](https://console.cloud.google.com/apis/library/health.googleapis.com).
2. Open Google Auth Platform and set it up as an **External** app. Under
   **Data Access**, add the scope
   `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly`.
   Under **Audience**, add your Google account as a test user, then click
   **Publish app**. While the app is in Testing, Google signs it out every 7
   days.
3. Under **Clients**, create a client of type **Desktop app**. Put its ID and
   secret in `.env`:
   ```
   GOOGLE_CLIENT_ID=….apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=…
   ```
4. Sign in with the Google account you use in the Google Health app:
   ```
   python3 fitbit_calories.py --authorize
   ```
   Google warns that the app isn't verified. It's yours, so click
   **Advanced → Go to …** and allow access. The script saves the token in
   `.env`, and in the `GOOGLE_REFRESH_TOKEN` repository secret if the GitHub
   CLI (`gh`) is signed in. Then it prints yesterday's calories to show it
   works.
5. Preview, then write the comment:
   ```
   python3 fitbit_calories.py --task ID_OR_LINK --dry-run
   python3 fitbit_calories.py --task ID_OR_LINK
   ```
   `CALORIES_TASK` in `.env` saves typing `--task`.

It runs on GitHub Actions from `.github/workflows/calories.yml`, every hour
from 08:23 to 00:23 UTC (09:23 to 01:23 in Madrid in winter), at 23 minutes
past because GitHub is busiest on the hour. Each run marks the task done once
today is over the goal, so it's ticked within about an hour of getting there.
GitHub starts scheduled runs late, lately by up to 7 hours, but with a run
every hour one usually still lands in the last hours of the day. A run that
starts after midnight checks the new day, and only refreshes the comment.

Add `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `GOOGLE_REFRESH_TOKEN` as
repository secrets. If the refresh token stops working (you revoked access, or
the app was still in Testing), the runs say so; run `--authorize` again,
which updates `.env` and the secret. While the app is in Testing, that's every
7 days; `--authorize` tells you when the new token runs out.

This repository's workflow logs are public, so a run prints neither the
calories nor whether it marked the task done. See [PRIVACY.md](PRIVACY.md).

## Tests

```
python3 -m unittest -v
```

# todoist

Habit tracker for Todoist. For every **recurring** task with the `@track` label,
`habit_tracker.py` keeps one comment on the task up to date:

```
📊 Habit stats
Tracking for: 24 days (since 23 Sep 2026)
Completion rate: 87% (20 completed / 3 failed)
Current streak: 5 days completed 🔥
Best streak: 9 days completed
Last 14 days: 🟩🟩🟥🟩⬜🟩🟩🟩🟩🟩⬜🟩🟩🟩
Updated 16 Oct 2026 07:00  `habit-tracker:v1:2026-09-23:2026-10-16:++-+.+…`
```

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
`HABIT_TZ=Europe/Warsaw` overrides the timezone; by default it comes from your
Todoist account settings.

## How it counts

Each calendar day since tracking started gets at most one outcome:

- **failed**: a run of the script found the task overdue. Every scheduled
  occurrence from the overdue due date up to yesterday is failed: each day for
  a daily task, once a week for a weekly one. Today is failed too if the task
  had a due time today that has passed. Failures are final: completing the
  task late doesn't undo them.
- **completed**: the task was completed that day, and the day isn't failed.
- Days with neither (not due yet, or a weekly task's off days) don't count and
  don't break streaks.

So a task due yesterday that you complete this morning gives yesterday 🟥 if
the script ran before you completed it, and today 🟩. A task due every Monday
that you complete on Thursday gives Monday 🟥 and Thursday 🟩. It gets a
second 🟥 only if it is still overdue the following Monday.

The schedule comes from the task's repeat text. The script understands the
common English forms: `every day`, `every other day`, `every 3 days`,
`every monday`, `every mon, wed, fri`, `every other monday`,
`every 2 weeks on tue`, `every weekday`, `every 15th`, `every 2nd monday`,
`every 3 months`, `every jan 1`. Times and `starting`/`until` parts are
ignored. If it can't read the repeat text, only the overdue due date fails. Tracking starts on the
first run that sees the task, or at its due date if it's already overdue by
then. Completions from before tracking started are ignored.

Todoist keeps no record of a task having been overdue, so the per-day history
is stored in the backtick line at the end of the stats comment. Don't edit that
line. If you delete the comment, tracking starts over.

## Scheduling

Run the script once a day, shortly after midnight, so it sees yesterday's
misses as overdue before you complete or reschedule them.

It runs nightly on GitHub Actions (`.github/workflows/habit-tracker.yml`) at
23:15 UTC, which is 00:15/01:15 in Madrid. To set it up, add your token as a
repository secret named `TODOIST_API_TOKEN` (Settings → Secrets and variables
→ Actions). To run it by hand, open Actions → Habit tracker → Run workflow, and
tick "dry run" to only print the comments.

If your timezone is far from UTC+1/+2, change the cron line so the run lands
just after your local midnight.

Recurring completions are read from Todoist's activity log. On the free plan
that log only goes back 7 days, so completions made during a gap of more than
a week between runs won't be counted.

## Tests

```
python3 -m unittest -v
```

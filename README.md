# todoist

Habit tracker for Todoist. For every **recurring** task with the `@track` label,
`habit_tracker.py` keeps one comment on the task up to date:

```
📊 Habit stats
Tracking since: 23 Sep 2026
Completion rate: 87% (17 on time / 3 late / 3 failed)
Current streak: 5 completions 🔥
Best streak: 9 completions
Last 10 results: 🟩🟩🟥🟩🟨🟩🟩🟩🟩🟩
Updated 16 Oct 2026 07:00  `habit-tracker:v4:2026-09-23:2026-10-17:1792130400:2026-10-16:++-+.~…`
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
`--task ID_OR_LINK` updates just one task; it takes the task's ID or its
Todoist link, and the task still has to be a recurring tracked one.
`HABIT_TZ=Europe/Warsaw` overrides the timezone; by default it comes from your
Todoist account settings.

## How it counts

Every occurrence of the task gets one result, recorded on the day it was
scheduled for:

- 🟩 **completed**: done on that day, or earlier. Ticking a Sunday task on
  Saturday counts for Sunday.
- 🟨 **late**: done after that day, but before the next occurrence's day.
- 🟥 **failed**: the next occurrence's day arrived and it still wasn't done.

Postponing a task doesn't change the day it was scheduled for. A Sunday task
moved to Tuesday and done there is late. A daily task postponed to tomorrow
fails when tomorrow arrives, and doing it tomorrow counts for tomorrow.

To take a completion back, move the task back to the day you completed it (or
earlier), or use Todoist's undo, which does the same. That occurrence is open
again, even if you move the task somewhere else afterwards: do it again to
count it, or it fails when the next one arrives. Moving
a later occurrence earlier, to a day after your last completion, is just
planning ahead and takes nothing back.

The completion rate counts late as done and shows the split, e.g.
`80% (6 on time / 2 late / 2 failed)`. A late result keeps a streak going;
only a failure breaks it. Days with no occurrence don't count.

The occurrence days come from the task's repeat text. The script understands
the common English forms: `every day`, `every other day`, `every 3 days`,
`every monday`, `every mon, wed, fri`, `every other monday`,
`every 2 weeks on tue`, `every weekday`, `every 3 months`, `every 15th`,
`every last day`, `every 2nd monday`, `every last workday`, `every jan 1`,
`every last sun feb, last sun apr, …`. Times and `starting`/`until` parts are
ignored. Hourly repeats (`every 12 hours`) aren't supported. If it can't read
the repeat text, it can't know when the next
occurrence is: an occurrence then fails the day after it was due, and there is
no late.

Tracking starts on the first run that sees the task, or at its due date if
it's already overdue by then. Completions from before tracking started are
ignored.

Todoist doesn't record which occurrence is still open, so the per-day results,
the open occurrence, the time of the last check and the day of the last
completion are stored in the backtick line at the end of the stats comment. Don't edit that line. If you delete the
comment, tracking starts over.

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

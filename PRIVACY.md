# Privacy policy

This policy covers Daily Calories (`fitbit_calories.py` in this repository), a
personal script. Its owner is its only user, and it isn't offered to anyone
else.

## What it reads

With the owner's permission, it reads one thing from the Google Health API: the
total calories burned on each of the last 7 days. The permission it holds
(`googlehealth.activity_and_fitness.readonly`) covers other activity data too,
but the script doesn't request it.

## Where the data goes

The numbers are written into a comment on the owner's Todoist task, where
Todoist stores them. They aren't sent anywhere else, and they aren't sold or
shared. They're kept out of this repository's workflow logs, which are public.

The token that lets the script read the data is kept in a local `.env` file and
in this repository's GitHub secrets. It's never committed to the repository.

## Removing access

The owner can remove the script's access at any time at
<https://myaccount.google.com/permissions>, and remove the numbers by deleting
the comment.

## Contact

Open an issue in this repository.

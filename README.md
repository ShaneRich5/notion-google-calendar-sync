# Notion ↔ Google Calendar task sync

Tick **Sync to Calendar** on a task in your Notion `tasks` database and it shows up in
Google Calendar. Edits flow both ways: change a task in Notion and the event updates;
drag or rename the event in Google Calendar and the task updates.

## What it does

| You do this | The sync does this |
| --- | --- |
| Tick **Sync to Calendar** on a task that has a **Due** date | Creates an event and saves its ID in **GCal Event ID** |
| Change the name, Due, Status, Priority or Tags in Notion | Updates the event |
| Rename or move the event in Google Calendar | Updates the task's name and Due date |
| Edit the same task on both sides between runs | The most recent edit wins |
| Set Status to ✅ or ♲ | Prefixes the event title, e.g. `✅ Pay rent` |
| Untick the box, or clear Due | Deletes the event |
| Delete the task in Notion | Deletes the event |
| Delete the event in Google Calendar | Unticks the box (with `TWO_WAY_SYNC=false` it recreates the event instead) |
| Duplicate a synced task | Gives the copy its own event |

How fields map:

- **Due** with a date only → all-day event. A date range → a multi-day event.
- **Due** with a time → timed event. With no end time it lasts `DEFAULT_EVENT_MINUTES` (60).
- **Priority** → event colour: High = red, Medium = yellow, Low = green.
- The event description lists Status, Priority and Tags, and links back to the Notion page.

Only the title and time sync back from Google. Everything else is owned by Notion.

## Setup

### 1. Create a Notion integration

1. Go to <https://www.notion.so/profile/integrations> → **New integration**.
   Type **Internal**, with the **Read content** and **Update content** capabilities.
2. Copy the **Internal Integration Secret**.
3. Open the [tasks database](https://www.notion.so/780ea27050a44001b65a9f0b84f1d824)
   → **•••** → **Connections** → add your integration.

   Your `tasks` dashboard page only holds *linked views* of this database, so connect
   the integration to the database itself (or to a page that contains it), not the dashboard.

### 2. Create a Google service account

The sync signs in as a **service account** — a non-human Google identity authenticated
with a private key instead of a browser sign-in. There's no "unverified app" warning
and no expiring token, and the same key works whether the sync runs on this laptop or
in GitHub Actions. You share your calendar with it, the same way you'd share it with
a person.

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project.
2. **APIs & Services → Library**: enable **Google Calendar API**.
3. **IAM & Admin → Service Accounts → Create service account**. Any name (e.g.
   `notion-calendar-sync`) — it needs no roles or extra access, just skip those steps.
4. Open the new service account → **Keys → Add key → Create new key → JSON**. This
   downloads a `.json` file — save it in this folder as `service-account.json`. It's a
   secret: never share it or commit it (already covered by `.gitignore`).
5. Open the downloaded file and copy the `client_email` value, e.g.
   `notion-calendar-sync@your-project.iam.gserviceaccount.com`.
6. In [Google Calendar](https://calendar.google.com/), find the calendar you want to
   sync to → **Settings and sharing → Share with specific people** → add that email
   → permission **Make changes to events** → **Send**.

### 3. Install and configure

```powershell
cd C:\Users\shane\Projects\notion-google-calendar-sync
py -3.11 -m venv .venv                  # needs Python 3.10+
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env                   # then edit .env
```

In `.env`, fill in `NOTION_TOKEN` and `GOOGLE_CALENDAR_ID` — the calendar you shared
with the service account in step 2.6, usually just your Gmail address (**not**
`primary`: that means the service account's own empty calendar, not yours).
`NOTION_DATA_SOURCE_ID` is already set to your tasks database.

### 4. Run setup

```powershell
.venv\Scripts\python -m notion_gcal_sync --setup
```

This adds the **Sync to Calendar** (checkbox) and **GCal Event ID** (text) properties to
the database, and checks the service account can reach the calendar you shared.

### 5. Sync

```powershell
.venv\Scripts\python -m notion_gcal_sync            # sync once
.venv\Scripts\python -m notion_gcal_sync --watch    # keep syncing every 5 minutes
.venv\Scripts\python -m notion_gcal_sync -v         # also log unchanged/skipped tasks
```

Logs are also written to `sync.log`.

## Running it automatically

### Remotely, with GitHub Actions (recommended)

`.github/workflows/sync.yml` runs the sync roughly every 10 minutes on GitHub's
servers — no computer of yours needs to stay on. It needs your settings as
**repository secrets** (GitHub repo → **Settings → Secrets and variables → Actions →
New repository secret**):

| Secret | Value |
| --- | --- |
| `NOTION_TOKEN` | Your Notion integration secret |
| `NOTION_DATA_SOURCE_ID` | `493101e3-fc1d-403b-9957-0be55bf4baaf` |
| `GOOGLE_CALENDAR_ID` | The calendar you shared with the service account |
| `GCP_SERVICE_ACCOUNT_JSON` | The **entire contents** of `service-account.json`, pasted as one secret |

That's everything required. `TWO_WAY_SYNC`, `TIMEZONE`, `DEFAULT_EVENT_MINUTES` and
`COMPLETED_STATUSES` are optional secrets — add one only if you want a non-default
value, same as in `.env`.

Once the secrets are set, it starts running on its own schedule. To run it once by
hand: GitHub repo → **Actions → Sync Notion tasks to Google Calendar → Run workflow**.
If a run fails, its log is attached to that run under **Artifacts**.

### Locally, on Windows

Useful for testing changes before they reach GitHub Actions, or if you'd rather not
rely on it at all. To sync every 5 minutes in the background with no console window:

```powershell
$dir = "C:\Users\shane\Projects\notion-google-calendar-sync"
$action = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\pythonw.exe" -Argument "-m notion_gcal_sync" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "Notion Google Calendar Sync" -Action $action -Trigger $trigger
```

To remove it later: `Unregister-ScheduledTask -TaskName "Notion Google Calendar Sync"`.

Running both at once is fine — they use the same fingerprint-based change detection,
so there's no conflict, just faster syncing.

## FAQ

**Do I need to store the Google Calendar ID in the Notion table?**
No. The calendar ID is one setting (`GOOGLE_CALENDAR_ID` in `.env`) because every task
goes to the same calendar. What each task *does* need is the ID of **its event**, so
the sync can find it again to update or delete it. That's the **GCal Event ID** column,
which `--setup` creates and the sync fills in. Leave it alone: don't type in it.
Each event also stores its Notion page ID in a hidden field, so if the event ID ever
goes missing (say a run is interrupted), the sync relinks the event instead of making
a duplicate.

**How does it know which side changed?**
Each event carries hidden fingerprints of what was last synced. Comparing them with
the current Notion task and the current event shows which side changed, so there is
no local database. That also means you can run the sync from any machine.

**Can I switch to a different calendar later?**
Untick your synced tasks and run a sync to remove the old events. Then change
`GOOGLE_CALENDAR_ID` and tick the tasks again. If you skip this, two-way mode can't find
the old events on the new calendar, treats them as deleted, and unticks the tasks.

**Why is there a delay?**
The sync polls; it doesn't get pushed changes. Changes appear on the next run: within
5 minutes locally (`--watch` or the scheduled task), or roughly 10 minutes on GitHub
Actions — its schedule isn't exact and can run a few minutes late, especially right on
the hour.

**Can I use GitHub Actions and run it locally at the same time?**
Yes — see "Running it automatically" above. Useful while testing a change: run it
locally to see the result immediately, without waiting for GitHub Actions' schedule.

## Development

```powershell
.venv\Scripts\python -m pytest
```

The tests run the whole sync against in-memory fakes of the Notion and Google APIs
(`tests/fakes.py`), so they need no credentials.

| File | Purpose |
| --- | --- |
| `notion_gcal_sync/sync.py` | The sync engine: what to create, update, pull or delete |
| `notion_gcal_sync/model.py` | Task ↔ event conversion, date handling, change fingerprints |
| `notion_gcal_sync/notion_api.py` | Notion queries, page updates, schema setup |
| `notion_gcal_sync/gcal_api.py` | Google service account auth and event calls |
| `notion_gcal_sync/config.py` | Settings from `.env` |
| `notion_gcal_sync/__main__.py` | Command line |
| `.github/workflows/sync.yml` | Scheduled remote sync via GitHub Actions |

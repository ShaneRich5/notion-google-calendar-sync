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

### 2. Create Google OAuth credentials

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project.
2. **APIs & Services → Library**: enable **Google Calendar API**.
3. **Google Auth Platform → Audience**: choose **External** and add your Gmail address
   as a test user. Then click **Publish app**. While an app is in *Testing*, Google
   expires its sign-ins after 7 days, which would stop an unattended sync. Publishing
   a personal app is fine: when you sign in, Google warns that it's unverified; click
   **Advanced → Go to (app name)**.
4. **Clients → Create client → Desktop app**. Download the JSON and save it in this
   folder as `credentials.json`.

### 3. Install and configure

```powershell
cd C:\Users\shane\Projects\notion-google-calendar-sync
py -3.11 -m venv .venv                  # needs Python 3.10+
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env                   # then paste NOTION_TOKEN into .env
```

`NOTION_DATA_SOURCE_ID` in `.env.example` is already set to your tasks database.
Set `GOOGLE_CALENDAR_ID` if you don't want to use your main calendar. A dedicated
"Notion Tasks" calendar is easy to hide or colour separately. You'll find its ID in
Google Calendar under **Settings → (calendar) → Integrate calendar**.

### 4. Run setup

```powershell
.venv\Scripts\python -m notion_gcal_sync --setup
```

This adds the **Sync to Calendar** (checkbox) and **GCal Event ID** (text) properties to
the database, opens a browser to sign in to Google, and checks it can reach the calendar.

### 5. Sync

```powershell
.venv\Scripts\python -m notion_gcal_sync            # sync once
.venv\Scripts\python -m notion_gcal_sync --watch    # keep syncing every 5 minutes
.venv\Scripts\python -m notion_gcal_sync -v         # also log unchanged/skipped tasks
```

Logs are also written to `sync.log`.

## Running it automatically on Windows

To sync every 5 minutes in the background with no console window, run this in PowerShell:

```powershell
$dir = "C:\Users\shane\Projects\notion-google-calendar-sync"
$action = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\pythonw.exe" -Argument "-m notion_gcal_sync" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "Notion Google Calendar Sync" -Action $action -Trigger $trigger
```

To remove it later: `Unregister-ScheduledTask -TaskName "Notion Google Calendar Sync"`.

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
5 minutes with the scheduled task or `--watch`.

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
| `notion_gcal_sync/gcal_api.py` | Google sign-in and event calls |
| `notion_gcal_sync/config.py` | Settings from `.env` |
| `notion_gcal_sync/__main__.py` | Command line |

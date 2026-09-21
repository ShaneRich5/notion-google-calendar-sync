"""The Google Calendar side: OAuth sign-in and the event operations the sync needs."""

from __future__ import annotations

from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import Config, ConfigError
from .model import SOURCE_KEY

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
GONE = (404, 410)

Event = dict[str, Any]


def load_credentials(cfg: Config, interactive: bool) -> Credentials:
    """Load the saved Google token, refreshing it if needed.

    Only opens a browser sign-in when `interactive` is set (during --setup), so a
    scheduled run never hangs waiting for a login.
    """
    creds = None
    if cfg.google_token_file.exists():
        creds = Credentials.from_authorized_user_file(str(cfg.google_token_file), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise ConfigError("Google sign-in is missing or expired. Run: python -m notion_gcal_sync --setup")
        if not cfg.google_credentials_file.exists():
            raise ConfigError(
                f"Google OAuth client file not found at {cfg.google_credentials_file}. "
                "Download it from Google Cloud Console (see README)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(cfg.google_credentials_file), SCOPES)
        creds = flow.run_local_server(port=0)
    cfg.google_token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service(cfg: Config, interactive: bool = False) -> Any:
    return build("calendar", "v3", credentials=load_credentials(cfg, interactive), cache_discovery=False)


def _without_nones(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_nones(v) for k, v in value.items() if v is not None}
    return value


class Calendar:
    def __init__(self, service: Any, calendar_id: str) -> None:
        self.events = service.events()
        self.calendar_id = calendar_id

    def check_access(self) -> None:
        try:
            self.events.list(calendarId=self.calendar_id, maxResults=1).execute()
        except HttpError as exc:
            if exc.resp.status in GONE:
                raise ConfigError(f"Google calendar {self.calendar_id!r} not found. Check GOOGLE_CALENDAR_ID.") from exc
            raise

    def list_managed(self, source_id: str) -> list[Event]:
        """All live events this tool created for the given Notion data source."""
        events: list[Event] = []
        page_token = None
        while True:
            response = self.events.list(
                calendarId=self.calendar_id,
                privateExtendedProperty=f"{SOURCE_KEY}={source_id}",
                maxResults=2500,
                pageToken=page_token,
            ).execute()
            events.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return events

    def get(self, event_id: str) -> Event | None:
        try:
            return self.events.get(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            if exc.resp.status in GONE:
                return None
            raise

    def insert(self, body: Event) -> Event:
        return self.events.insert(calendarId=self.calendar_id, body=_without_nones(body)).execute()

    def patch(self, event_id: str, body: Event) -> Event:
        return self.events.patch(calendarId=self.calendar_id, eventId=event_id, body=body).execute()

    def delete(self, event_id: str) -> None:
        try:
            self.events.delete(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            if exc.resp.status not in GONE:
                raise

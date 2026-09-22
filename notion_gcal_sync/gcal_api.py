"""The Google Calendar side: service-account sign-in and the event operations the sync needs.

A service account is a non-human Google identity, authenticated with a private key
instead of a browser sign-in. It has no calendar of its own worth using: you share
your real calendar with its email address (like sharing with a coworker), and it
then reads/writes that calendar using its key. No browser, no expiring token, no
"unverified app" limits — the same setup works unattended, on this machine or in CI.
"""

from __future__ import annotations

import json
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import Config, ConfigError
from .model import SOURCE_KEY

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
GONE = (404, 410)

Event = dict[str, Any]


class NotEventOwner(Exception):
    """Google refused to change an event because this service account didn't create it.

    Google restricts some changes to an event's original creator, even for someone
    with edit access to the calendar. This happens for events created before this
    project switched from a personal OAuth sign-in to a service account — anything
    the service account creates itself won't hit this again.
    """


def _is_forbidden_for_non_creator(exc: HttpError) -> bool:
    return exc.resp.status == 403 and b"forbiddenForNonCreator" in (exc.content or b"")


def _service_account_email(cfg: Config) -> str:
    try:
        return json.loads(cfg.google_service_account_file.read_text(encoding="utf-8"))["client_email"]
    except Exception:
        return "(its client_email, see the key file)"


def load_credentials(cfg: Config) -> service_account.Credentials:
    if not cfg.google_service_account_file.exists():
        raise ConfigError(
            f"Google service account key not found at {cfg.google_service_account_file}. "
            "Create one in Google Cloud Console and download its JSON key (see README)."
        )
    try:
        return service_account.Credentials.from_service_account_file(str(cfg.google_service_account_file), scopes=SCOPES)
    except (ValueError, KeyError) as exc:
        raise ConfigError(f"{cfg.google_service_account_file} doesn't look like a service account key: {exc}") from exc


def build_service(cfg: Config) -> Any:
    return build("calendar", "v3", credentials=load_credentials(cfg), cache_discovery=False)


def _without_nones(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_nones(v) for k, v in value.items() if v is not None}
    return value


class Calendar:
    def __init__(self, service: Any, calendar_id: str) -> None:
        self.events = service.events()
        self.calendar_id = calendar_id

    def check_access(self, cfg: Config) -> None:
        try:
            self.events.list(calendarId=self.calendar_id, maxResults=1).execute()
        except HttpError as exc:
            if exc.resp.status in GONE:
                raise ConfigError(
                    f"Google calendar {self.calendar_id!r} not found, or not shared with the service account "
                    f"{_service_account_email(cfg)}. In Google Calendar, share this calendar with that address "
                    "and give it 'Make changes to events' permission (see README)."
                ) from exc
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
        try:
            return self.events.patch(calendarId=self.calendar_id, eventId=event_id, body=body).execute()
        except HttpError as exc:
            if _is_forbidden_for_non_creator(exc):
                raise NotEventOwner(event_id) from exc
            raise

    def delete(self, event_id: str) -> None:
        try:
            self.events.delete(calendarId=self.calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            if exc.resp.status in GONE:
                return
            if _is_forbidden_for_non_creator(exc):
                raise NotEventOwner(event_id) from exc
            raise

    def replace(self, event_id: str, body: Event) -> Event:
        """Recreate an event under this service account, since it can't edit the original in place."""
        self.delete(event_id)
        return self.insert(body)

"""Settings, read from environment variables or the project's .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Google Calendar colour IDs: 11 = Tomato, 5 = Banana, 2 = Sage.
PRIORITY_COLORS = {"High": "11", "Medium": "5", "Low": "2"}


class ConfigError(Exception):
    """A setup problem the user has to fix; shown without a traceback."""


@dataclass(frozen=True)
class Config:
    notion_token: str
    data_source_id: str
    calendar_id: str = ""
    google_service_account_file: Path = PROJECT_ROOT / "service-account.json"
    two_way: bool = True
    default_duration_minutes: int = 60
    sync_interval_seconds: int = 300
    timezone_name: str = ""
    prop_title: str = "Task name"
    prop_date: str = "Due"
    prop_checkbox: str = "Sync to Calendar"
    prop_event_id: str = "GCal Event ID"
    prop_status: str = "Status"
    prop_priority: str = "Priority"
    prop_tags: str = "Tags"
    completed_statuses: tuple[str, ...] = ("✅", "♲")

    @property
    def default_duration(self) -> timedelta:
        return timedelta(minutes=self.default_duration_minutes)

    @property
    def tz(self) -> tzinfo:
        """Zone for Notion times that carry no UTC offset; defaults to this machine's zone."""
        if self.timezone_name:
            return ZoneInfo(self.timezone_name)
        return datetime.now().astimezone().tzinfo

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv(PROJECT_ROOT / ".env")

        def env(name: str, default: str = "") -> str:
            # A variable set to "" is treated the same as unset. Both mean "use the
            # default" here — e.g. GitHub Actions sets an unconfigured secret to "",
            # not absent, so this keeps optional settings optional in CI too.
            return os.environ.get(name, "").strip() or default

        token = env("NOTION_TOKEN")
        if not token:
            raise ConfigError("NOTION_TOKEN is not set. Copy .env.example to .env and fill it in.")
        data_source_id = env("NOTION_DATA_SOURCE_ID").removeprefix("collection://").replace("-", "").lower()
        if not data_source_id:
            raise ConfigError("NOTION_DATA_SOURCE_ID is not set (see .env.example).")

        calendar_id = env("GOOGLE_CALENDAR_ID")
        if not calendar_id:
            raise ConfigError(
                "GOOGLE_CALENDAR_ID is not set. A service account has no calendar of its own to default to: "
                "set this to the calendar you shared with it, e.g. your Gmail address (see README)."
            )
        if calendar_id.lower() == "primary":
            raise ConfigError(
                "GOOGLE_CALENDAR_ID=primary won't work with a service account: 'primary' means the service "
                "account's own (empty) calendar, not yours. Set it to the calendar you shared, e.g. your Gmail address."
            )

        timezone_name = env("TIMEZONE")
        if timezone_name:
            try:
                ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, ValueError):
                raise ConfigError(f"TIMEZONE={timezone_name!r} is not a valid IANA zone, e.g. America/New_York.")

        try:
            duration = int(env("DEFAULT_EVENT_MINUTES", "60"))
            interval = int(env("SYNC_INTERVAL_SECONDS", "300"))
        except ValueError as exc:
            raise ConfigError(f"DEFAULT_EVENT_MINUTES and SYNC_INTERVAL_SECONDS must be whole numbers ({exc}).")

        defaults = cls(notion_token="", data_source_id="")
        return cls(
            notion_token=token,
            data_source_id=data_source_id,
            calendar_id=calendar_id,
            google_service_account_file=PROJECT_ROOT / env("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json"),
            two_way=env("TWO_WAY_SYNC", "true").lower() in {"1", "true", "yes", "on"},
            default_duration_minutes=duration,
            sync_interval_seconds=interval,
            timezone_name=timezone_name,
            prop_title=env("NOTION_PROP_TITLE", defaults.prop_title),
            prop_date=env("NOTION_PROP_DATE", defaults.prop_date),
            prop_checkbox=env("NOTION_PROP_CHECKBOX", defaults.prop_checkbox),
            prop_event_id=env("NOTION_PROP_EVENT_ID", defaults.prop_event_id),
            prop_status=env("NOTION_PROP_STATUS", defaults.prop_status),
            prop_priority=env("NOTION_PROP_PRIORITY", defaults.prop_priority),
            prop_tags=env("NOTION_PROP_TAGS", defaults.prop_tags),
            completed_statuses=tuple(
                s.strip() for s in env("COMPLETED_STATUSES", ",".join(defaults.completed_statuses)).split(",") if s.strip()
            ),
        )

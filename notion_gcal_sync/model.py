"""Translation between Notion task pages and Google Calendar events.

The title and date are two-way: they can be edited on either side. The rest of the
event (description, colour) is derived from Notion and only flows Notion -> Google.

Every synced event carries two fingerprints in its private extended properties:

    notionHash  hash of the whole event as last built from Notion
    eventHash   hash of the two-way fields as last written to Google

Comparing them with the current state of each side tells the sync which side changed
since the last run, so no local database is needed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import PRIORITY_COLORS, Config

PAGE_KEY = "notionPageId"
SOURCE_KEY = "notionSource"
NOTION_HASH_KEY = "notionHash"
EVENT_HASH_KEY = "eventHash"

ONE_DAY = timedelta(days=1)


def parse_timestamp(value: str, tz: tzinfo | None = None) -> datetime:
    """Parse an ISO 8601 datetime from either API. Naive values get `tz` (default UTC)."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz or timezone.utc)
    return parsed.replace(microsecond=0)


def _parse_notion_date(value: str, tz: tzinfo) -> date | datetime:
    return parse_timestamp(value, tz) if "T" in value else date.fromisoformat(value)


def _zone(name: str | None) -> tzinfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class When:
    """When an event happens. For all-day spans `end` is exclusive, as in Google Calendar."""

    all_day: bool
    start: date | datetime
    end: date | datetime

    def key(self) -> list[str]:
        """A representation that compares equal for the same span, whatever the UTC offset."""
        if self.all_day:
            return ["date", self.start.isoformat(), self.end.isoformat()]
        return ["datetime", _utc(self.start), _utc(self.end)]

    def to_gcal(self) -> dict[str, dict[str, str | None]]:
        # Both keys are always sent so that a patch switching between all-day and timed
        # clears the one no longer used. None values are dropped on insert.
        if self.all_day:
            return {
                "start": {"date": self.start.isoformat(), "dateTime": None},
                "end": {"date": self.end.isoformat(), "dateTime": None},
            }
        return {
            "start": {"date": None, "dateTime": self.start.isoformat()},
            "end": {"date": None, "dateTime": self.end.isoformat()},
        }

    def to_notion(self, default_duration: timedelta) -> dict[str, str | None]:
        if self.all_day:
            last_day = self.end - ONE_DAY
            return {"start": self.start.isoformat(), "end": last_day.isoformat() if last_day > self.start else None}
        end = None if self.end - self.start == default_duration else self.end.isoformat()
        return {"start": self.start.isoformat(), "end": end}

    @classmethod
    def from_notion(cls, value: dict[str, Any] | None, cfg: Config) -> When | None:
        if not value or not value.get("start"):
            return None
        tz = _zone(value.get("time_zone")) or cfg.tz
        start = _parse_notion_date(value["start"], tz)
        end = _parse_notion_date(value["end"], tz) if value.get("end") else None
        if isinstance(start, datetime):
            if not isinstance(end, datetime) or end < start:
                end = start + cfg.default_duration
            return cls(False, start, end)
        if end is None or isinstance(end, datetime) or end < start:
            end = start
        return cls(True, start, end + ONE_DAY)

    @classmethod
    def from_gcal(cls, start: dict[str, str], end: dict[str, str]) -> When:
        if start.get("date"):
            return cls(True, date.fromisoformat(start["date"]), date.fromisoformat(end["date"]))
        return cls(False, parse_timestamp(start["dateTime"]), parse_timestamp(end["dateTime"]))


def plain_text(rich_text: list[dict[str, Any]] | None) -> str:
    return "".join(part.get("plain_text", "") for part in rich_text or [])


@dataclass(frozen=True)
class Task:
    """The parts of a Notion task page the sync cares about."""

    page_id: str
    url: str
    title: str
    when: When | None
    checked: bool
    event_id: str
    status: str
    priority: str
    tags: tuple[str, ...]
    last_edited: datetime

    @classmethod
    def from_page(cls, page: dict[str, Any], cfg: Config) -> Task:
        props = page.get("properties", {})

        def prop(name: str) -> dict[str, Any]:
            return props.get(name) or {}

        status = prop(cfg.prop_status)
        return cls(
            page_id=page["id"],
            url=page.get("url", ""),
            title=plain_text(prop(cfg.prop_title).get("title")).strip(),
            when=When.from_notion(prop(cfg.prop_date).get("date"), cfg),
            checked=bool(prop(cfg.prop_checkbox).get("checkbox")),
            event_id=plain_text(prop(cfg.prop_event_id).get("rich_text")).strip(),
            status=(status.get("status") or status.get("select") or {}).get("name") or "",
            priority=(prop(cfg.prop_priority).get("select") or {}).get("name") or "",
            tags=tuple(tag["name"] for tag in prop(cfg.prop_tags).get("multi_select") or []),
            last_edited=parse_timestamp(page["last_edited_time"]),
        )


def summary_for(task: Task, cfg: Config) -> str:
    """Event title: the task name, prefixed with the status when the task is complete."""
    title = task.title or "Untitled"
    return f"{task.status} {title}" if task.status in cfg.completed_statuses else title


def title_from_summary(summary: str, cfg: Config) -> str:
    """Inverse of summary_for: the task name from an event title."""
    summary = summary.strip()
    for status in cfg.completed_statuses:
        if summary.startswith(status + " "):
            return summary[len(status) + 1 :].strip()
    return summary


def description_for(task: Task) -> str:
    details = [
        f"{label}: {value}"
        for label, value in (("Status", task.status), ("Priority", task.priority), ("Tags", ", ".join(task.tags)))
        if value
    ]
    link = f"Open in Notion: {task.url}"
    return "\n".join(details + ["", link]) if details else link


def fingerprint(*parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_event(task: Task, cfg: Config) -> dict[str, Any]:
    """The Google Calendar event body for a task. The task must have a date."""
    assert task.when is not None
    summary = summary_for(task, cfg)
    description = description_for(task)
    color = PRIORITY_COLORS.get(task.priority)
    event: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "colorId": color,
        **task.when.to_gcal(),
        "extendedProperties": {
            "private": {
                PAGE_KEY: task.page_id,
                SOURCE_KEY: cfg.data_source_id,
                NOTION_HASH_KEY: fingerprint(summary, task.when.key(), description, color),
                EVENT_HASH_KEY: fingerprint(summary, task.when.key()),
            }
        },
    }
    if task.url:
        event["source"] = {"title": "Notion", "url": task.url}
    return event


def private_props(event: dict[str, Any]) -> dict[str, str]:
    return (event.get("extendedProperties") or {}).get("private") or {}


def current_event_hash(event: dict[str, Any]) -> str:
    """Fingerprint of the two-way fields as they are in Google Calendar right now."""
    when = When.from_gcal(event["start"], event["end"])
    return fingerprint(event.get("summary", "").strip(), when.key())
